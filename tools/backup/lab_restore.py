#!/usr/bin/env python3
"""Restore a Lab backup set (made by lab_backup.py) and PROVE it restored (#180).

Default is a drill: everything is restored into throwaway containers and checked, then thrown
away. Nothing touches the running sr33-dev-* stack.

  - every artifact is fetched from the set that stores it and its sha256 checked against INDEX;
  - every volume tarball is unpacked in a `--rm` container on a tmpfs, every file in it is hashed
    against INDEX, and every SQLite file must pass `integrity_check` and have the row counts the
    backup recorded;
  - the Postgres dump is restored into a fresh `--rm` TimescaleDB container (same image as the
    one it was dumped from, no network) and every table's row count must equal the count the
    backup read out of the dump.

Usage:
  lab_restore.py [--dest D] [--set backup-...|latest] [--work DIR]
                 [--into-volume-prefix P]    really restore: unpack each volume into a NEW named
                                             volume P<volume> (refuses one that exists) and keep it
                 [--pg-tmpfs SIZE]           PGDATA on a tmpfs of SIZE (e.g. 4g) instead of a
                                             scratch dir under --work
                 [--skip-pg]

Exit 0 only when every check passed. Prints one [OK ]/[FAIL] line per check.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lab_backup import INDEX, complete_sets, log, open_dest, sha256_file  # noqa: E402

EXTENSION_SEEDED = {"_timescaledb_catalog.bgw_job", "_timescaledb_config.bgw_job",
                    "_timescaledb_catalog.metadata"}
VERIFY_IMAGE = os.environ.get("C4_RESTORE_VERIFY_IMAGE", "python:3.12-alpine")

# Runs INSIDE the throwaway container: unpack /in/<tar> into /v, then report what is there.
_UNPACK_AND_HASH = r"""
import hashlib, json, os, sqlite3, sys, tarfile
tar, root = sys.argv[1], "/v"
with tarfile.open(tar, "r:gz") as t:
    t.extractall(root, filter="tar")
files, dbs = {}, {}
for d, _, fs in os.walk(root):
    for n in fs:
        p = os.path.join(d, n)
        if os.path.islink(p):
            continue
        rel = os.path.relpath(p, root)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        files[rel] = h.hexdigest()
        with open(p, "rb") as f:
            magic = f.read(16)
        if magic == b"SQLite format 3\x00":
            c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            dbs[rel] = {"integrity": ok,
                        "counts": {x: c.execute(f'SELECT count(*) FROM "{x}"').fetchone()[0]
                                   for x in names}}
            c.close()
print(json.dumps({"files": files, "dbs": dbs}))
"""

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    return bool(cond)


def docker(*args, **kw):
    return subprocess.run(["docker", *args], **kw)


def verify_volume(art, path, into_volume=None):
    """Unpack in a throwaway container and compare with what the backup recorded."""
    mount = (["-v", f"{into_volume}:/v"] if into_volume
             else ["--tmpfs", "/v:rw,size=2g"])
    r = docker("run", "--rm", "--network", "none", *mount,
               "-v", f"{os.path.dirname(path)}:/in:ro", VERIFY_IMAGE,
               "python", "-I", "-c", _UNPACK_AND_HASH, f"/in/{os.path.basename(path)}",
               capture_output=True, text=True)
    if not check(f"{art['name']}: unpacks", r.returncode == 0, r.stderr.strip()[-300:]):
        return
    got = json.loads(r.stdout)
    want = art.get("files", {})
    bad = sorted(k for k in want if got["files"].get(k) != want[k])
    extra = sorted(set(got["files"]) - set(want))
    check(f"{art['name']}: {len(want)} files, every sha256 matches",
          not bad and not extra,
          f"mismatch {bad[:5]} unexpected {extra[:5]}" if bad or extra else "")
    # Every SQLite file must have come through as a snapshot the backup counted: one INDEX does
    # not know about was archived as loose bytes (live file + WAL), and a -wal/-shm beside it
    # means the same. An expected DB that is missing or not SQLite fails below.
    expected = set(art.get("sqlite_counts", {}))
    stray = sorted(set(got["dbs"]) - expected)
    check(f"{art['name']}: every SQLite file is a counted snapshot", not stray,
          f"not snapshotted: {stray[:5]}" if stray else "")
    wal = sorted(k for k in got["files"] if k.endswith(("-wal", "-shm", "-journal")))
    check(f"{art['name']}: no SQLite -wal/-shm/-journal in the archive", not wal, str(wal[:5]))
    for rel, counts in art.get("sqlite_counts", {}).items():
        d = got["dbs"].get(rel, {})
        rows = sum(counts.values())
        check(f"{art['name']}: {rel} integrity_check", d.get("integrity") == "ok",
              str(d.get("integrity")))
        check(f"{art['name']}: {rel} row counts ({len(counts)} tables, {rows} rows)",
              d.get("counts") == counts,
              "" if d.get("counts") == counts else f"got {d.get('counts')}")


def _psql(name, user, db, sql):
    return docker("exec", name, "psql", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1",
                  "-Atc", sql, capture_output=True, text=True)


# pg_restore stderr lines a drill may accept. EMPTY ON PURPOSE: the real-data restore of
# 2026-11-02 exited 0 with no error lines once the extension version matched. Add an entry here
# only with the reason; anything not listed fails the drill.
KNOWN_BENIGN_RESTORE_ERRORS = ()


def _step(label, r):
    """A restore command as a check: a non-zero exit fails the drill, whatever the counts say."""
    return check(label, r.returncode == 0,
                 "" if r.returncode == 0 else f"exit {r.returncode}: "
                 + (r.stderr or r.stdout or "").strip()[-300:])


def resolve_image(art, loaded=()):
    """The image to restore into, obtained the way a NEW machine would: pulled from the
    registry by its recorded digest, or loaded from the image saved in the set. Never just
    "whatever this host has cached" (the local id the old backups recorded is not pullable)."""
    ref = art.get("image_ref")
    if ref:
        r = docker("pull", ref, capture_output=True, text=True)
        if not check(f"image pulled from the registry by digest ({ref})", r.returncode == 0,
                     "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[-300:]):
            return None
        return ref
    if art.get("image") in loaded:
        return art["image"]
    check("the dump's image can be obtained on a new machine", False,
          f"no registry digest recorded and no image saved in the set ({art.get('image')})")
    return None


def load_image(path, art):
    """docker load the image saved in the set; it must be the image id the backup recorded."""
    r = docker("load", "-i", path, capture_output=True, text=True)
    got = docker("image", "inspect", "-f", "{{.Id}}", art["content_key"],
                 capture_output=True, text=True)
    return check(f"image loaded from the set ({art['content_key'][:19]}…)",
                 r.returncode == 0 and got.stdout.strip() == art["content_key"],
                 (r.stderr or "").strip()[-200:])


def verify_pg(art, path, work, tmpfs=None, loaded=()):
    user, db = art["user"], art["db"]
    image = resolve_image(art, loaded)
    if image is None:
        return
    name = f"c4restore-pg-{os.getpid()}"
    data = None
    if tmpfs:
        mount = ["--tmpfs", f"/var/lib/postgresql/data:rw,size={tmpfs}"]
    else:
        data = tempfile.mkdtemp(prefix="pgdata-", dir=work)
        mount = ["-v", f"{data}:/var/lib/postgresql/data"]
    r = docker("run", "-d", "--rm", "--name", name, "--network", "none", *mount,
               "-v", f"{os.path.dirname(path)}:/in:ro",
               "-e", f"POSTGRES_USER={user}", "-e", f"POSTGRES_DB={db}",
               "-e", "POSTGRES_PASSWORD=restore-drill", "-e", "TIMESCALEDB_TELEMETRY=off",
               "-e", "TS_TUNE_MEMORY=1GB", image, capture_output=True, text=True)
    if not check(f"throwaway postgres {name} started", r.returncode == 0, r.stderr.strip()):
        return
    try:
        # The image's init runs a temporary server first; wait for the real one to answer.
        ready = False
        for _ in range(120):
            q = _psql(name, user, db, "SELECT 1")
            if q.returncode == 0 and q.stdout.strip() == "1":
                logs = docker("logs", name, capture_output=True, text=True)
                if "PostgreSQL init process complete" in logs.stdout + logs.stderr:
                    ready = True
                    break
            time.sleep(2)
        if not check("throwaway postgres ready", ready):
            return
        # The image ships several TimescaleDB versions and its init creates the NEWEST in the
        # database; a dump restores only into the version it was taken from (the live DB was
        # created on an older one and never `ALTER EXTENSION ... UPDATE`d).
        want = art.get("timescaledb")
        _step("drop the image's own timescaledb",
              _psql(name, user, db, "DROP EXTENSION IF EXISTS timescaledb CASCADE"))
        _step(f"create timescaledb {want or ''}".rstrip(),
              _psql(name, user, db, "CREATE EXTENSION timescaledb"
                    + (f" VERSION '{want}'" if want else "")))
        ext =_psql(name, user, db,
                    "SELECT extversion FROM pg_extension WHERE extname='timescaledb'").stdout
        check("timescaledb version matches the dump's", ext.strip() == art.get("timescaledb"),
              f"restore {ext.strip()} vs dump {art.get('timescaledb')}")
        _step("timescaledb_pre_restore()",
              _psql(name, user, db, "SELECT timescaledb_pre_restore()"))
        t0 = time.time()
        pr = docker("exec", name, "pg_restore", "-U", user, "-d", db,
                    f"/in/{os.path.basename(path)}", capture_output=True, text=True)
        _step("timescaledb_post_restore()",
              _psql(name, user, db, "SELECT timescaledb_post_restore()"))
        errs = [ln for ln in pr.stderr.splitlines() if "error" in ln.lower()]
        unknown = [ln for ln in errs
                   if not any(k in ln for k in KNOWN_BENIGN_RESTORE_ERRORS)]
        print(f"  pg_restore took {time.time() - t0:.0f} s, exit {pr.returncode}, "
              f"{len(errs)} error line(s), {len(errs) - len(unknown)} known-benign")
        for ln in unknown[:8]:
            print(f"    {ln}")
        # A non-zero exit fails unless every error line it reported is a listed benign one.
        check("pg_restore succeeded (exit 0, or only known-benign errors)",
              not unknown and (pr.returncode == 0 or bool(errs)),
              f"exit {pr.returncode}, {len(unknown)} unlisted error line(s)")
        tables = art.get("tables", {})
        bad, total = [], 0
        for t, want in sorted(tables.items()):
            q = _psql(name, user, db, f"SELECT count(*) FROM ONLY {t}")
            got = int(q.stdout.strip()) if q.returncode == 0 and q.stdout.strip() else None
            total += want
            # CREATE EXTENSION seeds these with its own rows (built-in jobs, install uuid/time),
            # which pg_dump deliberately leaves out: the dump's rows must all be there, plus those.
            seeded = t in EXTENSION_SEEDED
            if got is None or (got < want if seeded else got != want):
                bad.append(f"{t}: want {want}{'+' if seeded else ''} got {got}")
        check(f"{db}: {len(tables)} tables, {total:,} rows — every count matches the dump",
              tables and not bad, "; ".join(bad[:5]))
        size = _psql(name, user, db, f"SELECT pg_size_pretty(pg_database_size('{db}'))")
        print(f"  restored database size {size.stdout.strip()}")
    finally:
        docker("stop", "-t", "5", name, capture_output=True)
        if data:
            # PGDATA files belong to the container's postgres uid; clear them from a container.
            docker("run", "--rm", "-v", f"{data}:/d", VERIFY_IMAGE, "sh", "-c",
                   "rm -rf /d/* /d/.[!.]*", capture_output=True)
            shutil.rmtree(data, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=os.environ.get("C4_BACKUP_DEST"))
    ap.add_argument("--set", default="latest")
    ap.add_argument("--work", default=os.environ.get("C4_BACKUP_WORK", "/mnt/data/c4-backup/work"))
    ap.add_argument("--into-volume-prefix")
    ap.add_argument("--pg-tmpfs")
    ap.add_argument("--skip-pg", action="store_true")
    a = ap.parse_args(argv)
    dest = open_dest(a.dest)
    sets = dict(complete_sets(dest, strict=False))
    if not sets:
        print(f"no finished backup set at {dest.label}")
        return 1
    name = max(sets) if a.set == "latest" else a.set
    if name not in sets:
        print(f"{name} is not a finished set at {dest.label}")
        return 1
    index = sets[name]
    log(f"restoring {name} from {dest.label} ({len(index['artifacts'])} artifacts)")
    os.makedirs(a.work, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="restore-", dir=a.work)
    os.chmod(stage, 0o755)                    # the verify container reads it as a non-root user
    loaded = set()
    try:
        # A saved image first: the database restore needs it.
        for art in sorted(index["artifacts"], key=lambda x: x["kind"] != "image"):
            path = os.path.join(stage, art["name"])
            try:
                dest.fetch(art["stored_in"], art["name"], path)
            except Exception as exc:
                check(f"{art['name']}: fetched from {art['stored_in']}", False, str(exc))
                continue
            if not check(f"{art['name']}: sha256 matches INDEX "
                         f"({art['bytes'] / 1048576:.1f} MiB, from {art['stored_in']})",
                         sha256_file(path) == art["sha256"]):
                continue
            os.chmod(path, 0o644)
            if art["kind"] == "volume":
                target = None
                if a.into_volume_prefix:
                    target = a.into_volume_prefix + art["volume"]
                    if docker("volume", "inspect", target, capture_output=True).returncode == 0:
                        check(f"{target} does not exist yet", False, "refusing to overwrite")
                        continue
                    docker("volume", "create", target, capture_output=True, check=True)
                verify_volume(art, path, target)
            elif art["kind"] == "image" and not a.skip_pg:
                if load_image(path, art):
                    loaded.add(art["content_key"])
            elif art["kind"] == "postgres" and not a.skip_pg:
                verify_pg(art, path, stage, a.pg_tmpfs, loaded)
            os.remove(path)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    ok = all(results) and results
    print(f"\n{'RESTORE VERIFIED' if ok else 'RESTORE FAILED'}: {sum(results)}/{len(results)} "
          f"checks passed ({name})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
