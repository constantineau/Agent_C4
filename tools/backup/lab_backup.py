#!/usr/bin/env python3
"""Nightly backup of the Lab's Docker volumes and the dev Postgres (#180).

What one run does:
  1. Each Lab volume -> `vps-lab_<vol>.tar.gz`. The tar is deterministic (sorted, gzip mtime 0),
     and every SQLite file in it is an online `.backup()` snapshot, not the live file, so a Lab
     write mid-run cannot tear it.
  2. sr33_dev -> `vps-sr33_dev.pgdump` (`pg_dump -Fc`, a read-only snapshot transaction; nothing
     is written to the live database). The dump is then read back with `pg_restore -f -` in a
     throwaway container (no network) to count the rows of every table and hash its content.
  3. Each artifact is uploaded to the destination and checked there: a local copy is re-hashed,
     a GCS object's MD5 is compared with ours (no Coldline retrieval fee).
  4. `MANIFEST-sha256.txt` (sha256sum format, the artifacts stored in this set) and then
     `INDEX.json` (everything a restore needs: hashes, per-file hashes, row counts). INDEX.json
     goes last: a set without it is an unfinished run.
  5. Prune (if enabled): keep the newest N daily / weekly / monthly sets, delete the rest.

Unchanged artifacts are NOT stored again. If a volume's content (or the dump's content) hashes
the same as in the previous set, the new INDEX.json points at the set that already holds the
bytes. The coastline, ENC and retro volumes barely change, so most nights store only the dump and
the small JSON volumes. Prune never deletes an object a kept set still points at.

Destination (`--dest` / C4_BACKUP_DEST): a local directory, or `gs://bucket/prefix`.

Usage:
  lab_backup.py backup [--dest D] [--no-prune]
  lab_backup.py prune  [--dest D] [--dry-run]
  lab_backup.py list   [--dest D]

Env (all optional except the destination):
  C4_BACKUP_DEST         where sets go (required)
  C4_BACKUP_WORK         staging directory on this host (default /mnt/data/c4-backup/work)
  C4_BACKUP_VOLUMES      space-separated volume names (default: the Lab's eight, below)
  C4_BACKUP_PG           "container:user:db" (default sr33-dev-timescaledb-1:sr33:sr33_dev);
                         empty = no database
  C4_BACKUP_PRUNE        1 = prune after a successful backup (default 1)
  C4_BACKUP_KEEP         "daily,weekly,monthly" sets to keep (default 7,4,12)
"""
import argparse
import base64
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

# The Lab's state. gribcache is deliberately left out: it is a download cache (2 GB, 34k files)
# that refills itself from NOAA. coastline and enc are slow to rebuild and almost never change,
# so with de-duplication they cost nothing after the first set.
DEFAULT_VOLUMES = ("sr33-dev_lab_ingested sr33-dev_lab_learning sr33-dev_lab_retro "
                   "sr33-dev_lab_playbooks sr33-dev_lab_shares sr33-dev_lab_plume "
                   "sr33-dev_lab_coastline sr33-dev_lab_enc")
DEFAULT_PG = "sr33-dev-timescaledb-1:sr33:sr33_dev"
SET_RE = re.compile(r"^backup-(\d{4}-\d{2}-\d{2})T(\d{6})Z$")
INDEX = "INDEX.json"
MANIFEST = "MANIFEST-sha256.txt"
SQLITE_MAGIC = b"SQLite format 3\x00"
CHUNK = 1 << 20


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} lab-backup: {msg}",
          flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def md5_b64_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return base64.b64encode(h.digest()).decode()


# ---- destinations ----------------------------------------------------------------------------

class LocalDest:
    """A directory: <root>/<set>/<object>. Writes go to a temp name and are renamed when whole."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.label = self.root

    def sets(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(n for n in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, n)))

    def objects(self, set_name):
        d = os.path.join(self.root, set_name)
        return sorted(n for n in os.listdir(d) if not n.startswith(".")) if os.path.isdir(d) else []

    def put(self, local, set_name, name):
        d = os.path.join(self.root, set_name)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{name}.part")
        shutil.copyfile(local, tmp)
        os.replace(tmp, os.path.join(d, name))

    def verify(self, local, set_name, name):
        """The stored copy, re-read from the destination, hashes the same as ours."""
        p = os.path.join(self.root, set_name, name)
        return os.path.isfile(p) and sha256_file(p) == sha256_file(local)

    def read_text(self, set_name, name):
        p = os.path.join(self.root, set_name, name)
        if not os.path.isfile(p):
            return None
        with open(p) as f:
            return f.read()

    def fetch(self, set_name, name, local):
        shutil.copyfile(os.path.join(self.root, set_name, name), local)

    def delete(self, set_name, name):
        os.remove(os.path.join(self.root, set_name, name))
        return True

    def drop_set_if_empty(self, set_name):
        d = os.path.join(self.root, set_name)
        for n in os.listdir(d):             # leftover .part files of a dead run go with it
            if n.startswith("."):
                os.remove(os.path.join(d, n))
        if not os.listdir(d):
            os.rmdir(d)


class GcsDest:
    """gs://bucket/prefix via gsutil. Never touches anything outside the prefix."""

    def __init__(self, url, gsutil="gsutil"):
        m = re.match(r"^gs://([^/]+)/(.+?)/*$", url)
        if not m:
            raise SystemExit(f"refusing destination {url!r}: use gs://bucket/prefix (a prefix, "
                             f"never the bucket root, so prune cannot reach other data)")
        self.prefix = f"gs://{m.group(1)}/{m.group(2)}"
        self.label = self.prefix
        self.gsutil = gsutil

    def _run(self, *args, check=True, **kw):
        return subprocess.run([self.gsutil, "-q", *args], check=check, capture_output=True,
                              text=True, **kw)

    def sets(self):
        r = self._run("ls", f"{self.prefix}/", check=False)
        out = []
        for line in r.stdout.splitlines():
            line = line.rstrip("/")
            if line.startswith(self.prefix + "/"):
                out.append(line[len(self.prefix) + 1:])
        return sorted(n for n in out if n and "/" not in n)

    def objects(self, set_name):
        r = self._run("ls", f"{self.prefix}/{set_name}/", check=False)
        base = f"{self.prefix}/{set_name}/"
        return sorted(line[len(base):] for line in r.stdout.splitlines()
                      if line.startswith(base) and line[len(base):])

    def put(self, local, set_name, name):
        self._run("cp", local, f"{self.prefix}/{set_name}/{name}")

    def verify(self, local, set_name, name):
        r = self._run("stat", f"{self.prefix}/{set_name}/{name}", check=False)
        m = re.search(r"Hash \(md5\):\s*(\S+)", r.stdout)
        return bool(m) and m.group(1) == md5_b64_file(local)

    def read_text(self, set_name, name):
        r = self._run("cat", f"{self.prefix}/{set_name}/{name}", check=False)
        return r.stdout if r.returncode == 0 else None

    def fetch(self, set_name, name, local):
        self._run("cp", f"{self.prefix}/{set_name}/{name}", local)

    def delete(self, set_name, name):
        # The archive bucket has a retention policy: deletes are refused until an object is a
        # year old. That is reported, not fatal.
        r = self._run("rm", f"{self.prefix}/{set_name}/{name}", check=False)
        if r.returncode != 0:
            log(f"  could not delete {set_name}/{name}: {r.stderr.strip()[:200]}")
        return r.returncode == 0

    def drop_set_if_empty(self, set_name):
        pass                                # GCS has no directories


def open_dest(spec):
    if not spec:
        raise SystemExit("no destination: pass --dest or set C4_BACKUP_DEST")
    return GcsDest(spec) if spec.startswith("gs://") else LocalDest(spec)


# ---- artifacts -------------------------------------------------------------------------------

def sqlite_counts(path):
    """{table: rows} for every ordinary table, read-only."""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        return {n: c.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        c.close()


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_snapshot(src, dst):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


class _HashingWriter(io.RawIOBase):
    """Hashes the uncompressed tar stream (the content key) on its way into gzip."""

    def __init__(self, inner):
        self.inner, self.h = inner, hashlib.sha256()

    def writable(self):
        return True

    def write(self, b):
        self.h.update(b)
        return self.inner.write(b)


def tar_volume(src_dir, out_path, scratch):
    """Deterministic tar.gz of `src_dir`. Returns (content_key, {relpath: sha256}, {db: counts}).

    SQLite databases are archived as `.backup()` snapshots under their own name, and their
    -wal/-shm/-journal companions are left out (the snapshot already holds what they hold)."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        for n in sorted(dirnames) + sorted(filenames):
            entries.append(os.path.join(dirpath, n))
    entries.sort(key=lambda p: os.path.relpath(p, src_dir))
    dbs = {p for p in entries if os.path.isfile(p) and not os.path.islink(p) and is_sqlite(p)}
    skip = {p + sfx for p in dbs for sfx in ("-wal", "-shm", "-journal")}
    files, counts = {}, {}
    with open(out_path, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
        hw = _HashingWriter(gz)
        with tarfile.open(fileobj=hw, mode="w|", format=tarfile.GNU_FORMAT) as tar:
            for p in entries:
                if p in skip:
                    continue
                rel = os.path.relpath(p, src_dir)
                ti = tar.gettarinfo(p, arcname=rel)
                ti.mtime = int(ti.mtime)
                if ti.isfile():
                    data = p
                    if p in dbs:
                        data = os.path.join(scratch, "snap.db")
                        if os.path.exists(data):
                            os.remove(data)
                        _sqlite_snapshot(p, data)
                        ti.size = os.path.getsize(data)
                        counts[rel] = sqlite_counts(data)
                    files[rel] = sha256_file(data)
                    with open(data, "rb") as f:
                        tar.addfile(ti, f)
                else:
                    tar.addfile(ti)
        hw.flush()
    return hw.h.hexdigest(), files, counts


def volume_dir(volume):
    r = subprocess.run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", volume],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"volume {volume} not found: {r.stderr.strip()}")
    return r.stdout.strip()


COPY_RE = re.compile(r"^COPY (\S+) \(")


def scan_plain_sql(stream):
    """(content_key, {table: rows}) from a `pg_restore -f -` text stream. The key hashes the
    plain SQL, which (unlike the -Fc file, which carries the dump time) is the same for the same
    data. COPY targets are counted row by row, so a restore can be checked against them."""
    h = hashlib.sha256()
    counts, table = {}, None
    for line in stream:
        h.update(line)
        if table is not None:
            if line == b"\\.\n":
                table = None
            else:
                counts[table] += 1
        elif line.startswith(b"COPY "):
            m = COPY_RE.match(line.decode("utf-8", "replace"))
            if m:
                table = m.group(1)
                counts.setdefault(table, 0)
    return h.hexdigest(), counts


def pg_image(container):
    r = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", container],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def dump_pg(spec, out_path):
    """pg_dump -Fc of the live DB (pg_dump reads in one read-only snapshot transaction), then a
    content scan of the dump in a throwaway, network-less container from the same image."""
    container, user, db = spec.split(":")
    with open(out_path, "wb") as f:
        subprocess.run(["docker", "exec", container, "pg_dump", "-U", user, "-Fc", db],
                       stdout=f, check=True)
    image = pg_image(container)
    with open(out_path, "rb") as f:
        p = subprocess.Popen(["docker", "run", "--rm", "-i", "--network", "none", image,
                              "pg_restore", "-f", "-"], stdin=f, stdout=subprocess.PIPE)
        key, counts = scan_plain_sql(p.stdout)
        if p.wait() != 0:
            raise RuntimeError("pg_restore could not read the dump back")
    ver = subprocess.run(["docker", "exec", container, "psql", "-U", user, "-d", db, "-Atc",
                          "SELECT extversion FROM pg_extension WHERE extname='timescaledb'"],
                         capture_output=True, text=True).stdout.strip()
    return key, counts, {"image": image, "timescaledb": ver, "db": db, "user": user}


# ---- the run ---------------------------------------------------------------------------------

def set_name_for(now):
    return now.strftime("backup-%Y-%m-%dT%H%M%SZ")


def complete_sets(dest):
    """[(name, index)] of finished sets, oldest first."""
    out = []
    for n in dest.sets():
        if not SET_RE.match(n):
            continue
        txt = dest.read_text(n, INDEX)
        if txt is None:
            continue
        try:
            out.append((n, json.loads(txt)))
        except ValueError:
            log(f"  {n}/{INDEX} is unreadable — treated as unfinished")
    return out


def run_backup(dest, work, volumes, pg_spec, now=None, producers=None):
    """One backup set. `producers` (tests) replaces the docker-backed artifact makers:
    [(name, kind, fn(out_path, scratch) -> (content_key, extra_dict))]."""
    now = now or datetime.now(timezone.utc)
    set_name = set_name_for(now)
    if set_name in dest.sets():
        raise SystemExit(f"{set_name} already exists at {dest.label}")
    prev = complete_sets(dest)
    prev_index = prev[-1][1] if prev else {"artifacts": []}
    prev_by_name = {a["name"]: a for a in prev_index.get("artifacts", [])}

    if producers is None:
        producers = []
        for v in volumes:
            short = v.split("_", 1)[1] if v.startswith("sr33-dev_") else v

            def mk_vol(out, scratch, v=v):
                key, files, counts = tar_volume(volume_dir(v), out, scratch)
                return key, {"volume": v, "files": files, "sqlite_counts": counts}
            producers.append((f"vps-{short}.tar.gz", "volume", mk_vol))
        if pg_spec:
            db = pg_spec.split(":")[2]

            def mk_pg(out, scratch, spec=pg_spec):
                key, counts, meta = dump_pg(spec, out)
                return key, {"tables": counts, **meta}
            producers.append((f"vps-{db}.pgdump", "postgres", mk_pg))

    os.makedirs(work, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="stage-", dir=work)
    artifacts, stored, failures = [], [], []
    try:
        for name, kind, fn in producers:
            out = os.path.join(stage, name)
            scratch = tempfile.mkdtemp(dir=stage)
            try:
                key, extra = fn(out, scratch)
            except Exception as exc:              # one bad volume must not cost the others
                log(f"!! {name}: FAILED — {exc}")
                failures.append(name)
                continue
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            size = os.path.getsize(out)
            old = prev_by_name.get(name)
            if old and old.get("content_key") == key:
                art = {**old, **extra, "name": name, "kind": kind}
                art["stored_in"] = old["stored_in"]   # same bytes as before: point, don't store
                log(f"  {name}: unchanged — kept in {art['stored_in']}")
            else:
                digest = sha256_file(out)
                dest.put(out, set_name, name)
                if not dest.verify(out, set_name, name):
                    log(f"!! {name}: the stored copy does not match — set left unfinished")
                    failures.append(name)
                    continue
                art = {"name": name, "kind": kind, "sha256": digest, "bytes": size,
                       "content_key": key, "stored_in": set_name, **extra}
                stored.append(art)
                log(f"  {name}: {size / 1048576:.1f} MiB stored and verified at the destination")
            artifacts.append(art)
            os.remove(out)

        if failures:
            raise RuntimeError(f"{len(failures)} artifact(s) failed: {', '.join(failures)} — "
                               f"{set_name} left without {INDEX}, so nothing will treat it "
                               f"as a backup; the next prune removes it")
        man = os.path.join(stage, MANIFEST)
        with open(man, "w") as f:
            for a in stored:
                f.write(f"{a['sha256']}  {a['name']}\n")
        dest.put(man, set_name, MANIFEST)
        index = {"set": set_name, "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "artifacts": artifacts}
        ip = os.path.join(stage, INDEX)
        with open(ip, "w") as f:
            json.dump(index, f, indent=1, sort_keys=True)
        dest.put(ip, set_name, INDEX)
        if not dest.verify(ip, set_name, INDEX):
            raise RuntimeError(f"{INDEX} did not verify at the destination")
        new_bytes = sum(a["bytes"] for a in stored)
        log(f"{set_name} complete: {len(artifacts)} artifacts, {len(stored)} stored "
            f"({new_bytes / 1048576:.1f} MiB new), {len(artifacts) - len(stored)} unchanged")
        return set_name, index
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ---- retention -------------------------------------------------------------------------------

def set_time(name):
    m = SET_RE.match(name)
    return datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S").replace(
        tzinfo=timezone.utc)


def choose_keep(names, daily, weekly, monthly):
    """Grandfather-father-son over finished sets: the newest set of each of the last `daily`
    days that have one, of the last `weekly` ISO weeks, of the last `monthly` months. The newest
    set is always kept."""
    names = sorted(names, key=set_time, reverse=True)
    keep = set(names[:1])
    for n_keep, bucket in ((daily, lambda t: t.date()),
                           (weekly, lambda t: tuple(t.isocalendar())[:2]),
                           (monthly, lambda t: (t.year, t.month))):
        seen = []
        for n in names:
            b = bucket(set_time(n))
            if b in seen:
                continue
            if len(seen) >= n_keep:
                break
            seen.append(b)
            keep.add(n)
    return keep


def run_prune(dest, keep_spec=(7, 4, 12), dry_run=False, min_unfinished_age_h=24, now=None):
    """Delete sets outside the keep rule, but never an object a kept set's INDEX points at.
    Unfinished sets (no INDEX) older than `min_unfinished_age_h` are cleared the same way.
    Returns (deleted, refused) object counts."""
    now = now or datetime.now(timezone.utc)
    done = dict(complete_sets(dest))
    if not done:
        log("prune: no finished set at the destination — nothing is deleted")
        return 0, 0
    keep = choose_keep(list(done), *keep_spec)
    needed = {}                                  # set -> objects a kept set still points at
    for n in keep:
        for a in done[n]["artifacts"]:
            needed.setdefault(a["stored_in"], set()).add(a["name"])
        needed.setdefault(n, set()).update({INDEX, MANIFEST})
    deleted = refused = 0
    for n in dest.sets():
        if not SET_RE.match(n) or n in keep:
            continue
        if n not in done and (now - set_time(n)).total_seconds() < min_unfinished_age_h * 3600:
            continue                             # may be a run in progress
        hold = needed.get(n, set())
        # INDEX first: a half-pruned set must read as unfinished, never as a complete backup
        objs = sorted(dest.objects(n), key=lambda o: (o != INDEX, o))
        for o in objs:
            if o in hold:
                continue
            if dry_run:
                log(f"  would delete {n}/{o}")
                deleted += 1
                continue
            if dest.delete(n, o):
                deleted += 1
            else:
                refused += 1
        if not dry_run:
            dest.drop_set_if_empty(n)
        if hold:
            log(f"  {n}: kept {len(hold)} object(s) that newer sets still use")
    log(f"prune: keeping {len(keep)} set(s); {deleted} object(s) "
        f"{'would be ' if dry_run else ''}deleted, {refused} refused")
    return deleted, refused


# ---- cli -------------------------------------------------------------------------------------

def _keep_spec(s):
    parts = [int(x) for x in s.split(",")]
    if len(parts) != 3 or min(parts) < 1:
        raise SystemExit(f"C4_BACKUP_KEEP must be 'daily,weekly,monthly', each >= 1 (got {s!r})")
    return tuple(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("backup", "prune", "list"))
    ap.add_argument("--dest", default=os.environ.get("C4_BACKUP_DEST"))
    ap.add_argument("--work", default=os.environ.get("C4_BACKUP_WORK", "/mnt/data/c4-backup/work"))
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="prune: list, delete nothing")
    a = ap.parse_args(argv)
    dest = open_dest(a.dest)
    keep = _keep_spec(os.environ.get("C4_BACKUP_KEEP", "7,4,12"))

    if a.cmd == "list":
        for n, idx in complete_sets(dest):
            here = [x for x in idx["artifacts"] if x["stored_in"] == n]
            print(f"{n}  {len(idx['artifacts'])} artifacts, {len(here)} stored here, "
                  f"{sum(x['bytes'] for x in here) / 1048576:.1f} MiB")
        return 0
    if a.cmd == "prune":
        run_prune(dest, keep, dry_run=a.dry_run)
        return 0

    os.makedirs(a.work, exist_ok=True)
    lock = open(os.path.join(a.work, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another backup is running — exiting")
        return 1
    volumes = os.environ.get("C4_BACKUP_VOLUMES", DEFAULT_VOLUMES).split()
    pg = os.environ.get("C4_BACKUP_PG", DEFAULT_PG)
    log(f"backup -> {dest.label}: {len(volumes)} volume(s)" + (f" + {pg}" if pg else ""))
    try:
        run_backup(dest, a.work, volumes, pg)
    except Exception as exc:
        log(f"!! BACKUP FAILED: {exc}")
        return 1
    if os.environ.get("C4_BACKUP_PRUNE", "1") == "1" and not a.no_prune:
        run_prune(dest, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
