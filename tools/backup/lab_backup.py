#!/usr/bin/env python3
"""Nightly backup of the Lab's Docker volumes and the dev Postgres (#180).

What one run does:
  1. Each Lab volume -> `vps-lab_<vol>.tar.gz`. The tar is deterministic (sorted, gzip mtime 0),
     and every SQLite file in it is an online `.backup()` snapshot, not the live file, so a Lab
     write mid-run cannot tear it.
  2. the dev database -> `vps-<db>.pgdump` (`pg_dump -Fc`, a read-only snapshot transaction; nothing
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
  C4_BACKUP_PROJECT      the dev stack's compose project; names the default volumes
                         (<project>_lab_*) and database container (default: C4_DEV_PROJECT,
                         as compose.dev.yml resolves it: the environment, else this checkout's
                         .env, else c4-dev; #314)
  C4_BACKUP_VOLUMES      space-separated volume names (default: the Lab's eight, below)
  C4_BACKUP_PG           "container:user:db" (default <project>-timescaledb-1:<user>:<db>, with
                         POSTGRES_USER / POSTGRES_DB resolved the same way, else c4 / c4_dev);
                         empty = no database
  C4_ENV_FILE            the .env to read those from (default: <this checkout>/.env)

It runs on the VPS from its checkout (c4-lab-backup.service), so the dev stack's pins in that
checkout's .env (C4_DEV_PROJECT=..., POSTGRES_USER/DB) name the live volumes and database.
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
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stack_pin(name, default, env=None, env_file=None):
    """`name` as compose.dev.yml would see it: exported, else this checkout's .env (last line
    wins, quotes stripped), else `default`."""
    env = os.environ if env is None else env
    if env.get(name):
        return env[name]
    value = None
    try:
        with open(env_file or env.get("C4_ENV_FILE") or os.path.join(ROOT, ".env")) as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", line)
                if m and m.group(1) == name:
                    v = m.group(2)
                    q = re.match(r"^(['\"])(.*)\1$", v)
                    # quoted: as is; unquoted: " #..." is a comment, as compose reads it
                    value = q.group(2) if q else re.sub(r"\s+#.*$", "", v)
    except OSError:
        pass
    return value or default


# Volume and container names are prefixed by the dev stack's compose project (#314).
_PROJECT = os.environ.get("C4_BACKUP_PROJECT") or stack_pin("C4_DEV_PROJECT", "c4-dev")
VOL_PREFIX = f"{_PROJECT}_lab_"
PG_CONTAINER = f"{_PROJECT}-timescaledb-1"
LAB_VOLUMES = ("ingested", "learning", "retro", "playbooks", "shares", "plume", "coastline", "enc")
DEFAULT_VOLUMES = " ".join(VOL_PREFIX + v for v in LAB_VOLUMES)
DEFAULT_PG = (f"{PG_CONTAINER}:{stack_pin('POSTGRES_USER', 'c4')}:"
              f"{stack_pin('POSTGRES_DB', 'c4_dev')}")
SET_RE = re.compile(r"^backup-(\d{4}-\d{2}-\d{2})T(\d{6})Z$")
INDEX = "INDEX.json"
MANIFEST = "MANIFEST-sha256.txt"
ATOMIC_TMP_RE = re.compile(r"^\..+\.tmp$")
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
        """Copy, fsync, rename, fsync the set dir (and the root when the set is new): an INDEX
        that survives a power cut must never name an artifact that did not."""
        d = os.path.join(self.root, set_name)
        new = not os.path.isdir(d)
        os.makedirs(d, exist_ok=True)
        if new:
            _fsync_dir(self.root)
        tmp = os.path.join(d, f".{name}.part")
        shutil.copyfile(local, tmp)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(d, name))
        _fsync_dir(d)

    def sync(self):
        """fsync the root and every set dir (prune calls this before it deletes anything)."""
        if os.path.isdir(self.root):
            _fsync_dir(self.root)
            for n in self.sets():
                _fsync_dir(os.path.join(self.root, n))

    def has(self, set_name, art):
        """The stored object exists and is the one INDEX describes (size + sha256)."""
        p = os.path.join(self.root, set_name, art["name"])
        return (os.path.isfile(p) and os.path.getsize(p) == art.get("bytes")
                and sha256_file(p) == art.get("sha256"))

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
        d = os.path.join(self.root, set_name)
        os.remove(os.path.join(d, name))
        _fsync_dir(d)            # a retired INDEX stays retired before any artifact goes
        return True

    def drop_set_if_empty(self, set_name):
        d = os.path.join(self.root, set_name)
        for n in os.listdir(d):             # leftover .part files of a dead run go with it
            if n.startswith("."):
                os.remove(os.path.join(d, n))
        if not os.listdir(d):
            os.rmdir(d)


class DestError(RuntimeError):
    """The destination could not be read. Never the same as "not there": prune stops on it."""


_GCS_MISSING = ("matched no objects", "No URLs matched", "NotFound", "404")


class GcsDest:
    """gs://bucket/prefix via gsutil. Never touches anything outside the prefix.

    Reads separate "not there" (gsutil's no-match / 404) from every other failure, which
    raises DestError: a 503 on a listing must never read as an empty or missing set."""

    def __init__(self, url, gsutil="gsutil"):
        m = re.match(r"^gs://([^/]+)/(.+?)/*$", url)
        if not m:
            raise SystemExit(f"refusing destination {url!r}: use gs://bucket/prefix (a prefix, "
                             f"never the bucket root, so prune cannot reach other data)")
        self.prefix = f"gs://{m.group(1)}/{m.group(2)}"
        self.label = self.prefix
        self.gsutil = gsutil

    def _run(self, *args, check=True, **kw):
        # -q hides progress lines, but on `stat` it hides the whole answer (size, md5): stat
        # runs without it, or every verify reads "no hash" and fails.
        quiet = [] if args[0] == "stat" else ["-q"]
        return subprocess.run([self.gsutil, *quiet, *args], check=check, capture_output=True,
                              text=True, **kw)

    def _read(self, *args):
        """stdout, or None when the object/prefix does not exist; DestError on anything else."""
        r = self._run(*args, check=False)
        if r.returncode == 0:
            return r.stdout
        if any(k in r.stderr for k in _GCS_MISSING):
            return None
        raise DestError(f"gsutil {' '.join(args)} failed: {r.stderr.strip()[:300]}")

    def sets(self):
        out = []
        for line in (self._read("ls", f"{self.prefix}/") or "").splitlines():
            line = line.rstrip("/")
            if line.startswith(self.prefix + "/"):
                out.append(line[len(self.prefix) + 1:])
        return sorted(n for n in out if n and "/" not in n)

    def objects(self, set_name):
        base = f"{self.prefix}/{set_name}/"
        return sorted(line[len(base):] for line in (self._read("ls", base) or "").splitlines()
                      if line.startswith(base) and line[len(base):])

    def put(self, local, set_name, name):
        self._run("cp", local, f"{self.prefix}/{set_name}/{name}")

    def verify(self, local, set_name, name):
        r = self._run("stat", f"{self.prefix}/{set_name}/{name}", check=False)
        m = re.search(r"Hash \(md5\):\s*(\S+)", r.stdout)
        return bool(m) and m.group(1) == md5_b64_file(local)

    def read_text(self, set_name, name):
        return self._read("cat", f"{self.prefix}/{set_name}/{name}")

    def sync(self):
        pass                                # an upload that returned is already durable

    def has(self, set_name, art):
        """The object exists with INDEX's size and MD5 (from `gsutil stat`, no download). An
        INDEX entry without an md5 (older sets) or any stat failure counts as "no"."""
        if not art.get("md5"):
            return False
        try:
            out = self._read("stat", f"{self.prefix}/{set_name}/{art['name']}")
        except DestError:
            return False
        if out is None:
            return False
        size = re.search(r"Content-Length:\s*(\d+)", out)
        md5 = re.search(r"Hash \(md5\):\s*(\S+)", out)
        return (bool(size and md5) and int(size.group(1)) == art.get("bytes")
                and md5.group(1) == art["md5"])

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


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    """True for a SQLite file. A file that is gone is not one (it is skipped later); any OTHER
    read error raises: guessing "plain file" would tar a live DB and its WAL as loose bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except FileNotFoundError:
        return False


def _sqlite_snapshot(src, dst):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


class _HashingReader:
    """Hashes exactly the bytes tar reads from one open file."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b


class _HashingWriter(io.RawIOBase):
    """Hashes the uncompressed tar stream (the content key) on its way into gzip."""

    def __init__(self, inner):
        self.inner, self.h = inner, hashlib.sha256()

    def writable(self):
        return True

    def write(self, b):
        self.h.update(b)
        return self.inner.write(b)


def _listing(src_dir):
    """Every path under src_dir, sorted; atomic-write temps left out; unreadable dirs raise."""
    def unreadable(exc):           # os.walk skips a directory it cannot read, silently
        raise RuntimeError(f"cannot read {exc.filename}: {exc.strerror} — this volume's backup "
                           f"would be missing it") from exc
    entries = []
    for dirpath, dirnames, filenames in os.walk(src_dir, onerror=unreadable):
        dirnames.sort()
        for n in sorted(dirnames) + sorted(filenames):
            # The Lab's atomic writes leave `.<name>.<random>.tmp` beside a file while saving
            # it; it is never content, and it may vanish at any moment.
            if not ATOMIC_TMP_RE.match(n):
                entries.append(os.path.join(dirpath, n))
    return sorted(entries, key=lambda p: os.path.relpath(p, src_dir))


def tar_volume(src_dir, out_path, scratch):
    """Deterministic tar.gz of `src_dir`. Returns (content_key, {relpath: sha256}, {db: counts}).

    SQLite databases are archived as `.backup()` snapshots under their own name, and their
    -wal/-shm/-journal companions are left out (the snapshot already holds what they hold)."""
    # SQLite FIRST, files after: a DB row may name a file (retro.db -> pinned GRIBs, which the
    # Lab copies in before it inserts the row). Snapshotting the DBs before listing the files
    # means every file a snapshot names is already on disk when the listing runs.
    snaps = {}

    def snapshot(p):
        out = os.path.join(scratch, f"snap-{len(snaps)}.db")
        try:
            _sqlite_snapshot(p, out)
        except sqlite3.Error:
            if not os.path.exists(p):
                return None                      # deleted meanwhile: not archived
            raise
        snaps[p] = out
        return out

    def unsnapped(listing):
        return [p for p in listing if p not in snaps and os.path.isfile(p)
                and not os.path.islink(p) and is_sqlite(p)]

    # A DB that appears between the snapshot pass and the file listing would otherwise be
    # snapshotted AFTER the listing, and could name a file the listing missed. Snapshot it and
    # list again, until a listing finds no new DB.
    entries = _listing(src_dir)
    for _ in range(3):
        new_dbs = unsnapped(entries)
        if not new_dbs:
            break
        for p in new_dbs:
            snapshot(p)
        entries = _listing(src_dir)
    else:
        if unsnapped(entries):
            raise RuntimeError("databases kept appearing during the backup; retry the volume")
    dbs = {p for p in entries if p in snaps}
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
                if os.path.isfile(p) and not os.path.islink(p):
                    data = p
                    if p in dbs:
                        data = snaps.get(p)
                        if data is None:             # snapshot found it deleted
                            log(f"  {rel}: gone before it could be read (deleted) — "
                                f"not archived")
                            continue
                        counts[rel] = sqlite_counts(data)
                    # ONE open file for the header, the bytes and the hash. The Lab replaces its
                    # JSON with tmp + rename: a stat, a hash and a read by NAME could each see a
                    # different file and publish a tar that does not match INDEX. An open fd
                    # keeps reading the file it opened, whatever the name now points at.
                    try:
                        f = open(data, "rb")
                    except FileNotFoundError:
                        log(f"  {rel}: gone before it could be read (deleted) — not archived")
                        continue
                    with f:
                        if data == p:                # a plain file: still not a DB, by this fd?
                            if f.read(16) == SQLITE_MAGIC:
                                raise RuntimeError(f"{rel} became a database during the "
                                                   f"backup; retry the volume")
                            f.seek(0)
                        ti = tar.gettarinfo(arcname=rel, fileobj=f)
                        ti.mtime = int(ti.mtime)
                        if data != p:                # the snapshot's bytes, the db's metadata
                            src = os.stat(p)
                            ti.mtime, ti.mode = int(src.st_mtime), src.st_mode & 0o7777
                        hr = _HashingReader(f)
                        tar.addfile(ti, hr)
                    files[rel] = hr.h.hexdigest()
                else:
                    try:
                        ti = tar.gettarinfo(p, arcname=rel)
                    except FileNotFoundError:   # deleted between the listing and now
                        log(f"  {rel}: gone before it could be read (deleted) — not archived")
                        continue
                    ti.mtime = int(ti.mtime)
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


def image_meta(image_id):
    """What a NEW machine needs to get this exact image: a pullable `repo@sha256:...` digest
    (from RepoDigests) and the tag, as well as the local id. The local id alone is useless on a
    replacement VPS: nothing can pull it."""
    r = subprocess.run(["docker", "image", "inspect", "-f",
                        "{{json .RepoDigests}}|{{json .RepoTags}}", image_id],
                       capture_output=True, text=True, check=True)
    digests, tags = (json.loads(x) for x in r.stdout.strip().split("|", 1))
    return {"image": image_id, "image_ref": (digests or [None])[0],
            "image_tag": (tags or [None])[0]}


def save_image(image_id, out_path):
    """docker save | gzip: the fallback when an image has no registry digest to pull."""
    with open(out_path, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
        p = subprocess.Popen(["docker", "save", image_id], stdout=subprocess.PIPE)
        while b := p.stdout.read(CHUNK):
            gz.write(b)
        if p.wait() != 0:
            raise RuntimeError(f"docker save {image_id} failed")


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
    return key, counts, {**image_meta(image), "timescaledb": ver, "db": db, "user": user}


# ---- the run ---------------------------------------------------------------------------------

def set_name_for(now):
    return now.strftime("backup-%Y-%m-%dT%H%M%SZ")


def read_indexes(dest, strict=True):
    """{set: INDEX dict, or None for a set with no INDEX (unfinished)} for every set named like
    ours. FAILS CLOSED: a listing or read error (DestError/OSError) or an INDEX that does not
    parse raises DestError — prune must not guess what an unreadable set points at."""
    out = {}
    for n in dest.sets():
        if not SET_RE.match(n):
            continue
        txt = dest.read_text(n, INDEX)
        if txt is None:
            out[n] = None
            continue
        try:
            out[n] = json.loads(txt)
        except ValueError:
            if strict:
                raise DestError(f"{n}/{INDEX} exists but does not parse")
            log(f"  {n}/{INDEX} does not parse — skipped")
    return out


def complete_sets(dest, strict=True):
    """[(name, index)] of finished sets, oldest first. Read errors always raise; strict=False
    (restore, list) only skips an INDEX that does not parse instead of raising."""
    return sorted((n, i) for n, i in read_indexes(dest, strict).items() if i is not None)


def run_backup(dest, work, volumes, pg_spec, now=None, producers=None):
    """One backup set. `producers` (tests) replaces the docker-backed artifact makers:
    [(name, kind, fn(out_path, scratch) -> (content_key, extra_dict))]."""
    now = now or datetime.now(timezone.utc)
    set_name = set_name_for(now)
    if set_name in dest.sets():
        raise SystemExit(f"{set_name} already exists at {dest.label}")
    prev = complete_sets(dest, strict=False)   # a bad old INDEX must not stop tonight's backup
    prev_index = prev[-1][1] if prev else {"artifacts": []}
    prev_by_name = {a["name"]: a for a in prev_index.get("artifacts", [])}

    if producers is None:
        producers = []
        for v in volumes:
            # the artifact is named without the project: vps-lab_<vol>.tar.gz whatever the stack
            short = "lab_" + v[len(VOL_PREFIX):] if v.startswith(VOL_PREFIX) else v

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

            def mk_img(out, scratch, spec=pg_spec):
                meta = image_meta(pg_image(spec.split(":")[0]))
                if meta["image_ref"]:
                    return None                  # pullable by digest: nothing to store
                save_image(meta["image"], out)
                return meta["image"], meta       # content key = image id (dedups for free)
            producers.append(("vps-pg-image.tar.gz", "image", mk_img))

    os.makedirs(work, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="stage-", dir=work)
    artifacts, stored, failures = [], [], []
    try:
        for name, kind, fn in producers:
            out = os.path.join(stage, name)
            scratch = tempfile.mkdtemp(dir=stage)
            try:
                made = fn(out, scratch)
            except Exception as exc:              # one bad volume must not cost the others
                log(f"!! {name}: FAILED — {exc}")
                failures.append(name)
                continue
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            if made is None:                      # this producer had nothing to store
                if os.path.exists(out):
                    os.remove(out)
                continue
            key, extra = made
            size = os.path.getsize(out)
            old = prev_by_name.get(name)
            reuse = old and old.get("content_key") == key
            if reuse and not dest.has(old["stored_in"], old):
                # Never point at a copy that is gone or no longer matches: store it again here.
                log(f"  {name}: unchanged, but the copy in {old['stored_in']} is missing or does "
                    f"not match INDEX — storing it again")
                reuse = False
            if reuse:
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
                       "md5": md5_b64_file(out), "content_key": key, "stored_in": set_name,
                       **extra}
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
    """Delete sets outside the keep rule. Conservative by construction:

    - it reads every set's INDEX first, and ANY read or listing error stops it (fail closed);
    - phase 1 deletes only INDEX files (that retires a set; its bytes stay);
    - phase 2 protects every object pointed at by an INDEX that is still standing: the first
      snapshot minus the INDEX deletes that actually succeeded, UNION a fresh re-list (if the
      re-list fails, it stops before deleting any artifact);
    - phase 3 deletes only unprotected objects of the retired / long-unfinished sets, and stops
      at the first listing error.
    Returns (deleted, refused) object counts."""
    now = now or datetime.now(timezone.utc)
    try:
        dest.sync()                          # what prune reasons about is on disk first
        snap = read_indexes(dest)
    except (DestError, OSError) as exc:
        log(f"!! prune ABORTED before deleting anything: {exc}")
        return 0, 0
    done = {n: i for n, i in snap.items() if i is not None}
    if not done:
        log("prune: no finished set at the destination — nothing is deleted")
        return 0, 0
    keep = choose_keep(list(done), *keep_spec)
    candidates = sorted(n for n in snap if n not in keep and (
        n in done or (now - set_time(n)).total_seconds() >= min_unfinished_age_h * 3600))
    deleted = refused = 0

    # Phase 1: retire obsolete sets (INDEX only).
    retired = set()
    for n in candidates:
        if n not in done:
            continue
        if dry_run:
            log(f"  would delete {n}/{INDEX}")
            retired.add(n)
        elif dest.delete(n, INDEX):
            retired.add(n)
        else:
            refused += 1
            log(f"  {n}: its {INDEX} could not be deleted — it stays a complete set")
    deleted += len(retired)

    # Phase 2: everything any standing INDEX points at is protected.
    standing = {n: i for n, i in done.items() if n not in retired}
    if not dry_run:
        try:
            again = read_indexes(dest)
        except (DestError, OSError) as exc:
            log(f"!! prune stopped before deleting any artifact (re-list failed: {exc}); "
                f"{len(retired)} set(s) retired, their bytes are still there")
            return deleted, refused
        for n, i in again.items():
            if i is not None:
                standing.setdefault(n, i)
    protected = {}
    for n, idx in standing.items():
        for art in idx["artifacts"]:
            protected.setdefault(art["stored_in"], set()).add(art["name"])
        protected.setdefault(n, set()).update({INDEX, MANIFEST})

    # Phase 3: delete what nothing standing protects.
    for n in candidates:
        hold = protected.get(n, set())
        try:
            objs = dest.objects(n)
        except (DestError, OSError) as exc:
            log(f"!! prune stopped: could not list {n} ({exc})")
            break
        for o in objs:
            if o in hold or (dry_run and o == INDEX):
                continue
            if dry_run:
                log(f"  would delete {n}/{o}")
                deleted += 1
            elif dest.delete(n, o):
                deleted += 1
            else:
                refused += 1
        if not dry_run:
            dest.drop_set_if_empty(n)
        if hold and n not in standing:
            log(f"  {n}: kept {len(hold)} object(s) that standing sets still use")
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
        for n, idx in complete_sets(dest, strict=False):
            here = [x for x in idx["artifacts"] if x["stored_in"] == n]
            print(f"{n}  {len(idx['artifacts'])} artifacts, {len(here)} stored here, "
                  f"{sum(x['bytes'] for x in here) / 1048576:.1f} MiB")
        return 0
    # One lock for everything that reads the destination and then writes or deletes there: a
    # prune that listed the sets before a backup published its INDEX could delete artifacts that
    # INDEX points at (unchanged artifacts are referenced, not re-stored).
    os.makedirs(a.work, exist_ok=True)
    lock = open(os.path.join(a.work, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another backup or prune holds {a.work}/.lock — exiting")
        return 1
    if a.cmd == "prune":
        run_prune(dest, keep, dry_run=a.dry_run)
        return 0
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
