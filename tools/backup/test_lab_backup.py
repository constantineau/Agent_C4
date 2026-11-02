"""The Lab backup's logic, against a local destination: no docker, no network, no bucket (#180).

Covers what decides whether a backup can be trusted and whether a prune can lose one:
  - a volume tarball is deterministic, and a live SQLite file goes in as a consistent snapshot
    (its rows readable from the tar, its -wal left out) with recorded row counts;
  - the plain-SQL scan counts COPY rows per table and ignores the dump's metadata;
  - a run stores, verifies and indexes every artifact; an unchanged artifact is not stored
    again but pointed at; a failed artifact leaves the set unfinished (no INDEX);
  - grandfather-father-son retention keeps the right sets, and prune never deletes an object a
    kept set points at, never a fresh unfinished set, and refuses a GCS bucket root.

Run: python3 tools/backup/test_lab_backup.py
"""
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab_backup as lb                                                      # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


lb.log = lambda msg: None                    # quiet


def make_volume(root):
    os.makedirs(os.path.join(root, "boats"))
    with open(os.path.join(root, "boats", "sr33.json"), "w") as f:
        f.write('{"name": "C4"}')
    with open(os.path.join(root, "_labstate.json"), "w") as f:
        f.write("{}")
    db = os.path.join(root, "learning.db")
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO runs (v) VALUES (?)", [(str(i),) for i in range(50)])
    c.commit()
    return c                                  # left open: the WAL holds the last commit


print("tar_volume")
with tempfile.TemporaryDirectory() as tmp:
    vol = os.path.join(tmp, "vol")
    conn = make_volume(vol)
    check("the live DB has an unmerged -wal (the case a plain tar gets wrong)",
          os.path.getsize(os.path.join(vol, "learning.db-wal")) > 0)
    s1, s2 = os.path.join(tmp, "s1"), os.path.join(tmp, "s2")
    os.makedirs(s1)
    os.makedirs(s2)
    k1, files1, counts1 = lb.tar_volume(vol, os.path.join(tmp, "a.tar.gz"), s1)
    k2, files2, counts2 = lb.tar_volume(vol, os.path.join(tmp, "b.tar.gz"), s2)
    check("same volume, same content key", k1 == k2)
    check("same volume, byte-identical tar.gz",
          lb.sha256_file(os.path.join(tmp, "a.tar.gz")) == lb.sha256_file(os.path.join(tmp, "b.tar.gz")))
    check("row counts recorded for the SQLite file", counts1 == {"learning.db": {"runs": 50}})
    check("-wal/-shm not archived", not any(k.endswith(("-wal", "-shm")) for k in files1))
    with tarfile.open(os.path.join(tmp, "a.tar.gz")) as t:
        t.extractall(os.path.join(tmp, "x"), filter="tar")
    x = sqlite3.connect(os.path.join(tmp, "x", "learning.db"))
    check("the archived DB alone holds all 50 rows (WAL content included)",
          x.execute("SELECT count(*) FROM runs").fetchone()[0] == 50)
    x.close()
    got = {rel: lb.sha256_file(os.path.join(tmp, "x", rel)) for rel in files1}
    check("every recorded file hash matches the unpacked file", got == files1)
    conn.execute("INSERT INTO runs (v) VALUES ('new')")
    conn.commit()
    s3 = os.path.join(tmp, "s3")
    os.makedirs(s3)
    k3, _, counts3 = lb.tar_volume(vol, os.path.join(tmp, "c.tar.gz"), s3)
    check("a changed DB changes the content key", k3 != k1)
    check("...and its count", counts3["learning.db"]["runs"] == 51)
    conn.close()

print("scan_plain_sql")
sql = (b"-- Dumped from database version 16.4\n"
       b"COPY public.telemetry_raw (time, boat_id) FROM stdin;\n"
       b"2026-11-01\tsr33\n2026-11-02\tsr33\n\\.\n"
       b"COPY _timescaledb_internal._hyper_3_20_chunk (time) FROM stdin;\n"
       b"\\.\n"
       b"COPY public.alerts (id) FROM stdin;\n1\n2\n3\n\\.\n")
key, counts = lb.scan_plain_sql(io.BytesIO(sql))
check("COPY rows counted per table", counts == {"public.telemetry_raw": 2,
                                                "_timescaledb_internal._hyper_3_20_chunk": 0,
                                                "public.alerts": 3})
check("content key is the hash of the plain SQL", key == hashlib.sha256(sql).hexdigest())


def producer(content, key=None, fail=False):
    def fn(out, scratch):
        if fail:
            raise RuntimeError("volume vanished")
        with gzip.GzipFile(out, "wb", mtime=0) as f:
            f.write(content)
        return key or hashlib.sha256(content).hexdigest(), {"files": {"a": "b"}}
    return fn


T0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)

print("run_backup")
with tempfile.TemporaryDirectory() as tmp:
    dest = lb.LocalDest(os.path.join(tmp, "dest"))
    work = os.path.join(tmp, "work")
    prods = [("vps-lab_ingested.tar.gz", "volume", producer(b"ingested-1")),
             ("vps-lab_retro.tar.gz", "volume", producer(b"retro-big" * 1000))]
    n1, idx1 = lb.run_backup(dest, work, [], None, now=T0, producers=prods)
    objs = dest.objects(n1)
    check("first set stores both artifacts + MANIFEST + INDEX",
          sorted(objs) == sorted(["vps-lab_ingested.tar.gz", "vps-lab_retro.tar.gz",
                                  lb.MANIFEST, lb.INDEX]))
    man = dest.read_text(n1, lb.MANIFEST)
    okman = all(lb.sha256_file(os.path.join(dest.root, n1, line.split()[1])) == line.split()[0]
                for line in man.splitlines())
    check("MANIFEST-sha256.txt is sha256sum-compatible and correct", okman and man.count("\n") == 2)
    prods2 = [("vps-lab_ingested.tar.gz", "volume", producer(b"ingested-2")),
              ("vps-lab_retro.tar.gz", "volume", producer(b"retro-big" * 1000))]
    n2, idx2 = lb.run_backup(dest, work, [], None, now=T0 + timedelta(days=1), producers=prods2)
    by = {a["name"]: a for a in idx2["artifacts"]}
    check("changed artifact stored in the new set", by["vps-lab_ingested.tar.gz"]["stored_in"] == n2)
    check("unchanged artifact points at the set that holds it",
          by["vps-lab_retro.tar.gz"]["stored_in"] == n1
          and "vps-lab_retro.tar.gz" not in dest.objects(n2))
    check("MANIFEST of the new set lists only what it stores",
          dest.read_text(n2, lb.MANIFEST).count("\n") == 1)
    check("no staging left behind", os.listdir(work) == [] or os.listdir(work) == [".lock"])

    bad = [("vps-lab_ingested.tar.gz", "volume", producer(b"x")),
           ("vps-lab_retro.tar.gz", "volume", producer(b"", fail=True))]
    try:
        lb.run_backup(dest, work, [], None, now=T0 + timedelta(days=2), producers=bad)
        raised = False
    except RuntimeError:
        raised = True
    n3 = lb.set_name_for(T0 + timedelta(days=2))
    check("a failed artifact fails the run", raised)
    check("...and the set has no INDEX (not a backup)", lb.INDEX not in dest.objects(n3))
    check("...and is not listed as complete", n3 not in dict(lb.complete_sets(dest)))

    # a destination copy that does not verify must not be indexed
    real_verify = dest.verify
    dest.verify = lambda local, s, n: False if n.endswith(".tar.gz") else real_verify(local, s, n)
    try:
        lb.run_backup(dest, work, [], None, now=T0 + timedelta(days=3),
                      producers=[("vps-lab_ingested.tar.gz", "volume", producer(b"y"))])
        raised = False
    except RuntimeError:
        raised = True
    dest.verify = real_verify
    check("a copy that fails verification fails the run", raised)

print("choose_keep")
start = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
names = [lb.set_name_for(start + timedelta(days=i)) for i in range(400)]
names.append(lb.set_name_for(start + timedelta(days=399, hours=12)))    # 2 sets on the last day
keep = lb.choose_keep(names, 7, 4, 12)
newest = max(names, key=lb.set_time)
check("newest kept", newest in keep)
check("only the newest of a day counts", lb.set_name_for(start + timedelta(days=399)) not in keep)
days = {lb.set_time(n).date() for n in keep}
last7 = {(start + timedelta(days=399 - i)).date() for i in range(7)}
check("the last 7 days each kept", last7 <= days)
months = {(lb.set_time(n).year, lb.set_time(n).month) for n in keep}
check("12 months represented", len(months) == 12)
check("bounded: at most 7+4+12 sets", len(keep) <= 23)
check("nothing older than 12 months + slack kept",
      min(lb.set_time(n) for n in keep) > lb.set_time(newest) - timedelta(days=370))

print("run_prune")
with tempfile.TemporaryDirectory() as tmp:
    dest = lb.LocalDest(os.path.join(tmp, "dest"))
    work = os.path.join(tmp, "work")
    t = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    # retro never changes (stored once, day 0); ingested changes daily
    for i in range(30):
        lb.run_backup(dest, work, [], None, now=t + timedelta(days=i), producers=[
            ("vps-lab_ingested.tar.gz", "volume", producer(f"ingested-{i}".encode())),
            ("vps-lab_retro.tar.gz", "volume", producer(b"retro"))])
    first = lb.set_name_for(t)
    # an unfinished set from a crashed run 5 days ago, and one from a run still going
    old_dead = lb.set_name_for(t + timedelta(days=25, hours=5))
    os.makedirs(os.path.join(dest.root, old_dead))
    open(os.path.join(dest.root, old_dead, "vps-lab_ingested.tar.gz"), "w").close()
    now = t + timedelta(days=29, hours=1)
    running = lb.set_name_for(now - timedelta(minutes=10))
    os.makedirs(os.path.join(dest.root, running))
    open(os.path.join(dest.root, running, "vps-sr33_dev.pgdump"), "w").close()
    # an unrelated directory under the prefix
    os.makedirs(os.path.join(dest.root, "backup-2026-10-31"))
    open(os.path.join(dest.root, "backup-2026-10-31", "vps-sr33_dev.pgdump"), "w").close()

    before = len(lb.complete_sets(dest))
    d_dry, _ = lb.run_prune(dest, (7, 4, 12), dry_run=True, now=now)
    check("dry run deletes nothing", len(lb.complete_sets(dest)) == before and d_dry > 0)
    lb.run_prune(dest, (7, 4, 12), now=now)
    left = dict(lb.complete_sets(dest))
    check("retention applied (30 daily sets -> at most 7+4+12)", 7 <= len(left) <= 12)
    check("the first set's retro object survives (kept sets point at it)",
          "vps-lab_retro.tar.gz" in dest.objects(first))
    check("...but the first set no longer reads as a backup (its INDEX is gone)",
          first in left or lb.INDEX not in dest.objects(first))
    check("old unfinished set cleared", old_dead not in dest.sets())
    check("a run in progress is left alone", running in dest.sets())
    check("a directory not named like our sets is never touched",
          os.path.exists(os.path.join(dest.root, "backup-2026-10-31", "vps-sr33_dev.pgdump")))
    # every kept set must still be fully restorable from what is on disk
    whole = all(os.path.isfile(os.path.join(dest.root, a["stored_in"], a["name"]))
                and lb.sha256_file(os.path.join(dest.root, a["stored_in"], a["name"])) == a["sha256"]
                for idx in left.values() for a in idx["artifacts"])
    check("every artifact of every kept set is still there and hashes right", whole)

    with tempfile.TemporaryDirectory() as empty:
        d, r = lb.run_prune(lb.LocalDest(empty), (7, 4, 12), now=now)
        check("no finished set -> nothing deleted", (d, r) == (0, 0))

print("prune and a refused INDEX delete (Codex on #198)")
with tempfile.TemporaryDirectory() as tmp:
    class Locked(lb.LocalDest):
        """A bucket under a retention lock: INDEX.json deletes are refused."""
        def delete(self, set_name, name):
            return False if name == lb.INDEX else super().delete(set_name, name)
    dest = Locked(os.path.join(tmp, "dest"))
    t = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    for i in range(3):
        lb.run_backup(dest, os.path.join(tmp, "w"), [], None, now=t + timedelta(days=i),
                      producers=[("vps-lab_ingested.tar.gz", "volume",
                                  producer(f"i-{i}".encode()))])
    first = lb.set_name_for(t)
    d, r = lb.run_prune(dest, (1, 1, 1), now=t + timedelta(days=3))
    whole = all(os.path.isfile(os.path.join(dest.root, a["stored_in"], a["name"]))
                for idx in dict(lb.complete_sets(dest)).values() for a in idx["artifacts"])
    check(f"INDEX refused -> the rest of that set is left alone ({d} deleted, {r} refused)",
          d == 0 and r == 2 and "vps-lab_ingested.tar.gz" in dest.objects(first))
    check("every set that still reads as complete can still restore", whole)

print("a set whose INDEX survives keeps what it points at (Codex round 3 on #198)")
with tempfile.TemporaryDirectory() as tmp:
    class LockB(lb.LocalDest):
        """Retention lock on set B's INDEX only."""
        b_name = None

        def delete(self, set_name, name):
            if set_name == self.b_name and name == lb.INDEX:
                return False
            return super().delete(set_name, name)
    dest = LockB(os.path.join(tmp, "dest"))
    t = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    for i, content in enumerate((b"x-v1", b"x-v1", b"x-v2")):     # A stores x, B points at A/x
        lb.run_backup(dest, os.path.join(tmp, "w"), [], None, now=t + timedelta(days=i),
                      producers=[("vps-lab_retro.tar.gz", "volume", producer(content))])
    a_set, b_set = lb.set_name_for(t), lb.set_name_for(t + timedelta(days=1))
    dest.b_name = b_set
    check("setup: B points at A's copy", dict(lb.complete_sets(dest))[b_set]["artifacts"][0][
        "stored_in"] == a_set)
    lb.run_prune(dest, (1, 1, 1), now=t + timedelta(days=3))
    standing = dict(lb.complete_sets(dest))
    check("B is still standing (its INDEX was refused)", b_set in standing)
    check("...and A's copy that B points at survived the prune",
          all(os.path.isfile(os.path.join(dest.root, a["stored_in"], a["name"]))
              for idx in standing.values() for a in idx["artifacts"]))
    check("A itself no longer reads as a backup", a_set not in standing)

print("a file replaced (tmp + rename) while it is being archived (Codex round 3 on #198)")
with tempfile.TemporaryDirectory() as tmp:
    vol = os.path.join(tmp, "vol")
    os.makedirs(vol)
    target = os.path.join(vol, "race.json")
    with open(target, "w") as f:
        f.write("old " * 1000)
    real_gti, fired = tarfile.TarFile.gettarinfo, []

    def replace_after_stat(self, *a, **kw):
        ti = real_gti(self, *a, **kw)
        if ti.name == "race.json" and not fired:             # the Lab saves right now
            fired.append(1)
            with open(target + ".tmp", "w") as f:
                f.write("new, longer content " * 400)
            os.replace(target + ".tmp", target)
        return ti
    tarfile.TarFile.gettarinfo = replace_after_stat
    try:
        os.makedirs(os.path.join(tmp, "s"))
        key, files, _ = lb.tar_volume(vol, os.path.join(tmp, "v.tar.gz"), os.path.join(tmp, "s"))
    finally:
        tarfile.TarFile.gettarinfo = real_gti
    with tarfile.open(os.path.join(tmp, "v.tar.gz")) as t:
        member = t.extractfile("race.json").read()
    check("the replace happened mid-archive", fired == [1])
    check("the archived bytes are one whole version of the file",
          member in (b"old " * 1000, b"new, longer content " * 400))
    check("...and INDEX's hash is the hash of exactly those bytes",
          files["race.json"] == hashlib.sha256(member).hexdigest())

print("prune takes the same lock as backup (Codex on #198)")
with tempfile.TemporaryDirectory() as tmp:
    import fcntl
    dest_dir, work = os.path.join(tmp, "dest"), os.path.join(tmp, "work")
    dest = lb.LocalDest(dest_dir)
    t = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    for i in range(3):
        lb.run_backup(dest, work, [], None, now=t + timedelta(days=i),
                      producers=[("vps-lab_ingested.tar.gz", "volume",
                                  producer(f"i-{i}".encode()))])
    held = open(os.path.join(work, ".lock"), "w")
    fcntl.flock(held, fcntl.LOCK_EX)                 # a backup is running
    os.environ["C4_BACKUP_KEEP"] = "1,1,1"
    rc = lb.main(["prune", "--dest", dest_dir, "--work", work])
    check("prune while a backup holds the lock -> exits 1, deletes nothing",
          rc == 1 and len(lb.complete_sets(dest)) == 3)
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    rc = lb.main(["prune", "--dest", dest_dir, "--work", work])
    check("...and prunes once the lock is free", rc == 0 and len(lb.complete_sets(dest)) == 1)
    del os.environ["C4_BACKUP_KEEP"]

print("destinations")
for bad in ("gs://constantineau-c4-archive", "gs://constantineau-c4-archive/"):
    try:
        lb.open_dest(bad)
        refused = False
    except SystemExit:
        refused = True
    check(f"bucket root refused ({bad})", refused)
check("prefix accepted", lb.open_dest("gs://b/vps-nightly").prefix == "gs://b/vps-nightly")

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
