"""Copy-off of the full-res archive, against a fake Pi archive dir and a local "VPS" dir (#181).

What must hold, because the boat's copy is the only one:
  - segments carry every row of the live archive, in id order, with no gaps or overlaps, while
    the archiver keeps writing (a concurrent writer thread here);
  - the archive file is never written by the copy-off, and closed files are never touched;
  - a copy that does not verify at the receiver (one flipped byte, a lost row, a missing file)
    does not count: the ledger does not advance and the outbox copy is kept for a retry;
  - a remote destination (rsync + ssh, shimmed here) gives the same result;
  - with ARCHIVE_OFFBOAT_LEDGER set, the archiver's prune deletes only verified rows, nothing at
    all for a rotated (new-inode) file, and nothing when the ledger is unreadable.

Run (needs `websockets` for the archiver import — the archiver image has it):
  docker run --rm -v $PWD:/repo:ro -e PYTHONPATH=/repo/pi/archiver:/repo -w /repo sr33-ci-archiver \
    python3 pi/archiver/test_copyoff.py
"""
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import archiver                                                          # noqa: E402
import copyoff                                                           # noqa: E402

copyoff.log = lambda msg: None
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fake_pi(tmp, n=5000, t0=None):
    """archive.db (the archiver's own schema, WAL) + a closed corrupt file next to it."""
    d = os.path.join(tmp, "sk_archive")
    os.makedirs(d)
    conn = sqlite3.connect(os.path.join(d, "archive.db"), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(archiver.SCHEMA)
    t0 = t0 or time.time() - 30 * 86400
    conn.executemany("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                     "VALUES (?,?,?,?,?,?)",
                     [(iso(t0 + i), "sr33", "n2k.3", "navigation.speedOverGround", i * 0.01,
                       None if i % 7 else "ünïcode ⛵") for i in range(n)])
    conn.commit()
    with open(os.path.join(d, "archive.corrupt-20261028T154237.db"), "wb") as f:
        f.write(b"SQLite format 3\x00" + os.urandom(300_000))
    old = time.time() - 3600
    os.utime(os.path.join(d, "archive.corrupt-20261028T154237.db"), (old, old))
    return d, conn


def all_rows(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT {', '.join(copyoff.COLS)} FROM readings ORDER BY id").fetchall()
    finally:
        c.close()


def received_rows(vps):
    out = []
    for n in sorted(x for x in os.listdir(vps) if x.startswith("seg-") and x.endswith(".db")):
        out += all_rows(os.path.join(vps, n))
    return out


print("seal + ship, with the archiver writing all along")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp)
    outbox, vps = os.path.join(tmp, "outbox"), os.path.join(tmp, "vps")
    os.makedirs(outbox)
    stop = threading.Event()

    def writer():
        conn.execute("PRAGMA synchronous=OFF")    # a test: no fsync storm on a busy CI disk
        i = 0
        while not stop.is_set():
            conn.execute("INSERT INTO readings (time, boat_id, source, path, value) "
                         "VALUES (?,?,?,?,?)", (iso(time.time()), "sr33", "n2k.5", "x", i))
            conn.commit()
            i += 1
            time.sleep(0.005)
    w = threading.Thread(target=writer)
    w.start()
    corrupt = os.path.join(arc_dir, "archive.corrupt-20261028T154237.db")
    corrupt_before = (copyoff.sha256_file(corrupt), os.stat(corrupt).st_mtime)
    made_closed = copyoff.seal_closed(arc_dir, outbox, settle_s=60)
    made = copyoff.seal_segments(arc_dir, outbox, max_rows=1200, max_segments=8)
    time.sleep(0.2)
    made += copyoff.seal_segments(arc_dir, outbox, max_rows=1200, max_segments=3)
    stop.set()
    w.join()
    conn.close()
    check(f"segments cut while writing ({made} segments, 1 closed file)",
          made >= 5 and made_closed == 1)
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    check(f"everything shipped and verified ({done} ok, {fail} failed)", fail == 0)
    ledger = copyoff.load_ledger(outbox)
    ino = str(os.stat(os.path.join(arc_dir, "archive.db")).st_ino)
    got = received_rows(vps)
    src = all_rows(os.path.join(arc_dir, "archive.db"))
    sealed = ledger["live"][ino]["sealed_through_id"]
    check("received rows == archive rows up to the sealed id: no gap, no overlap, same values",
          got == [r for r in src if r[0] <= sealed] and len(got) == sealed)
    check("verified_through_id == sealed_through_id", ledger["live"][ino][
        "verified_through_id"] == sealed)
    check("outbox emptied of shipped segments (only the closed sidecar + ledger left)",
          sorted(n for n in os.listdir(outbox) if not n.startswith(".")) == [
              "closed-archive.corrupt-20261028T154237.db.json",
                                         "ledger.json"])
    check("closed file verified and recorded",
          all(v["verified"] for v in ledger["closed"].values()))
    check("closed file on the receiver is byte-identical",
          copyoff.sha256_file(os.path.join(vps, "archive.corrupt-20261028T154237.db"))
          == corrupt_before[0])
    check("closed file on the boat untouched (hash and mtime)",
          (copyoff.sha256_file(corrupt), os.stat(corrupt).st_mtime) == corrupt_before)
    d2, f2 = copyoff.ship(arc_dir, outbox, vps)
    check("a second ship has nothing to do", (d2, f2) == (0, 0))
    check("a re-seal of a caught-up archive cuts nothing new beyond the tail",
          copyoff.seal_segments(arc_dir, outbox, max_rows=10**6) <= 1)

print("the archive file is never written")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=3000)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    live = os.path.join(arc_dir, "archive.db")
    before = copyoff.sha256_file(live)
    os.chmod(live, stat.S_IRUSR | stat.S_IRGRP)       # read-only: a write would raise
    outbox = os.path.join(tmp, "outbox")
    os.makedirs(outbox)
    n = copyoff.seal_segments(arc_dir, outbox, max_rows=1000)
    os.chmod(live, 0o644)
    check(f"sealed from a read-only archive ({n} segments)", n == 3)
    check("archive bytes unchanged", copyoff.sha256_file(live) == before)

print("a bad copy does not count")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=3000)
    conn.close()
    os.remove(os.path.join(arc_dir, "archive.corrupt-20261028T154237.db"))
    outbox, vps = os.path.join(tmp, "outbox"), os.path.join(tmp, "vps")
    os.makedirs(outbox)
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)
    segs = sorted(n for n in os.listdir(outbox) if n.endswith(".db"))
    real = copyoff._transfer

    def flip_byte(src, dest, name):                 # the wire corrupts the second segment
        real(src, dest, name)
        if name == segs[1]:
            p = os.path.join(dest, name)
            with open(p, "r+b") as f:
                f.seek(os.path.getsize(p) // 2)
                b = f.read(1)
                f.seek(-1, 1)
                f.write(bytes([b[0] ^ 0xFF]))
    copyoff._transfer = flip_byte
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    copyoff._transfer = real
    ledger = copyoff.load_ledger(outbox)
    ino = next(iter(ledger["live"]))
    check(f"one flipped byte -> that segment fails ({done} ok, {fail} failed)",
          fail == 1 and done == 2)
    check("verified_through_id stops before the bad segment (contiguous only)",
          ledger["live"][ino]["verified_through_id"] == 1000)
    check("the bad segment stays in the outbox for a retry", segs[1] in os.listdir(outbox))
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    ledger = copyoff.load_ledger(outbox)
    check("the retry verifies it and the ledger catches up",
          fail == 0 and ledger["live"][ino]["verified_through_id"] == 3000)

    # a receiver that says ok but names different rows is not believed
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)    # nothing new
    c = sqlite3.connect(os.path.join(arc_dir, "archive.db"))
    c.execute("INSERT INTO readings (time, boat_id, source, path, value) VALUES "
              "('2026-11-02T00:00:00Z','sr33','s','p',1)")
    c.commit()
    c.close()
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)
    real_v = copyoff._remote_verify
    copyoff._remote_verify = lambda dest, name: {**real_v(dest, name), "rows": 0}
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    copyoff._remote_verify = real_v
    check("a receipt with the wrong row count is rejected", fail == 1 and done == 0)

    # the receiver's own verify catches a lost row even when bytes and sidecar were rewritten
    seg = next(n for n in os.listdir(vps) if n.startswith("seg-") and n.endswith(".db"))
    c = sqlite3.connect(os.path.join(vps, seg))
    c.execute("DELETE FROM readings WHERE id = (SELECT min(id) FROM readings)")
    c.commit()
    c.close()
    side = json.load(open(os.path.join(vps, seg + ".json")))
    side["sha256"] = copyoff.sha256_file(os.path.join(vps, seg))
    side["bytes"] = os.path.getsize(os.path.join(vps, seg))
    json.dump(side, open(os.path.join(vps, seg + ".json"), "w"))
    check("receiver verify: a lost row fails the content hash", not copyoff.verify(vps, seg)["ok"])

print("a rotated db travels with its -wal/-shm (Codex on #198)")
CORRUPT = "archive.corrupt-20261028T154237.db"
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=10)
    conn.close()
    old = time.time() - 3600
    for sfx, payload in (("-wal", b"WAL: rows committed only here " + os.urandom(5000)),
                         ("-shm", os.urandom(32768))):
        with open(os.path.join(arc_dir, CORRUPT + sfx), "wb") as f:
            f.write(payload)
        os.utime(os.path.join(arc_dir, CORRUPT + sfx), (old, old))
    outbox, vps = os.path.join(tmp, "outbox"), os.path.join(tmp, "vps")
    os.makedirs(outbox)
    copyoff.seal_closed(arc_dir, outbox, settle_s=60)
    side = json.load(open(os.path.join(outbox, f"closed-{CORRUPT}.json")))
    check("the sidecar lists the db, its -wal and its -shm",
          sorted(m["name"] for m in side["members"]) == [CORRUPT, CORRUPT + "-shm",
                                                         CORRUPT + "-wal"])
    real = copyoff._transfer

    def bad_wal(src, dest, name):                   # the -wal arrives damaged
        real(src, dest, name)
        if name.endswith("-wal"):
            with open(os.path.join(dest, name), "r+b") as f:
                f.write(b"X")
    copyoff._transfer = bad_wal
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    copyoff._transfer = real
    led = copyoff.load_ledger(outbox)
    check("a damaged -wal fails the whole unit: the db is NOT marked verified",
          fail == 1 and not any(v["verified"] for v in led["closed"].values()))
    done, fail = copyoff.ship(arc_dir, outbox, vps)
    led = copyoff.load_ledger(outbox)
    check("the retry ships the unit and it verifies",
          fail == 0 and all(v["verified"] for v in led["closed"].values()))
    check("the -wal on the receiver is byte-identical to the boat's",
          copyoff.sha256_file(os.path.join(vps, CORRUPT + "-wal"))
          == copyoff.sha256_file(os.path.join(arc_dir, CORRUPT + "-wal")))
    rec = json.load(open(os.path.join(vps, CORRUPT + ".verified.json")))
    check("the receipt covers all three files", len(rec.get("members", [])) == 3)
    # a -wal that appears after the db was verified is a new unit, not "already done"
    with open(os.path.join(arc_dir, CORRUPT + "-wal"), "ab") as f:
        f.write(b"more")
    os.utime(os.path.join(arc_dir, CORRUPT + "-wal"), (old, old))
    check("a changed -wal re-seals the unit", copyoff.seal_closed(arc_dir, outbox, 60) == 1)
    open(os.path.join(arc_dir, "archive.db.rotating"), "w").close()
    check("no closed unit is sealed while a rotation is half-done",
          copyoff.seal_closed(arc_dir, outbox, 60) == 0)

print("seal and ship never overwrite each other's ledger (Codex on #198)")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=1000)
    os.remove(os.path.join(arc_dir, CORRUPT))
    outbox, vps = os.path.join(tmp, "outbox"), os.path.join(tmp, "vps")
    os.makedirs(outbox)
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)        # seg 1..1000
    conn.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES "
                     "(?,?,?,?,?)", [(iso(time.time()), "sr33", "s", "p", i) for i in range(500)])
    conn.commit()
    conn.close()
    in_verify, release = threading.Event(), threading.Event()
    real_v = copyoff._remote_verify

    def slow_verify(dest, name):                    # ship pauses mid-flight, ledger loaded
        in_verify.set()
        release.wait(3)
        return real_v(dest, name)
    copyoff._remote_verify = slow_verify
    shipper = threading.Thread(target=copyoff.ship, args=(arc_dir, outbox, vps))
    shipper.start()
    in_verify.wait(5)
    sealer = threading.Thread(target=copyoff.seal_segments, args=(arc_dir, outbox),
                              kwargs={"max_rows": 1000})
    sealer.start()
    sealer.join(1.0)                                # with the lock it is still waiting here
    release.set()
    shipper.join()
    sealer.join()
    copyoff._remote_verify = real_v
    led = copyoff.load_ledger(outbox)
    ent = next(iter(led["live"].values()))
    check(f"ship's verified range survives an overlapping seal (verified_through_id "
          f"{ent.get('verified_through_id')})", ent.get("verified_through_id") == 1000)
    check(f"seal's cursor survives an overlapping ship (sealed_through_id "
          f"{ent['sealed_through_id']})", ent["sealed_through_id"] == 1500)
    segs = sorted(n for n in os.listdir(outbox) if n.startswith("seg-") and n.endswith(".db"))
    check("exactly the new segment is waiting in the outbox", len(segs) == 1
          and segs[0].endswith("-000000001001-000000001500.db"))

print("remote destination (rsync + ssh shims)")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=2500)
    conn.close()
    outbox, vps = os.path.join(tmp, "outbox"), os.path.join(tmp, "vps")
    os.makedirs(outbox)
    os.makedirs(vps)
    shim = os.path.join(tmp, "shim")
    os.makedirs(shim)
    me = os.path.abspath(copyoff.__file__)
    # rsync <flags...> SRC host:DST  -> copy SRC to DST;  ssh host CMD -> run CMD locally
    with open(os.path.join(shim, "rsync"), "w") as f:
        f.write("#!/bin/sh\nfor a; do :; done\nsrc=\"\"\nfor a; do case $a in -*) ;; *) "
                "[ -z \"$src\" ] && src=$a ;; esac; done\ncp \"$src\" \"${a#*:}\"\n")
    with open(os.path.join(shim, "ssh"), "w") as f:
        f.write("#!/bin/sh\nshift\nexec sh -c \"$*\"\n")
    for n in ("rsync", "ssh"):
        os.chmod(os.path.join(shim, n), 0o755)
    os.environ.update(COPYOFF_RSYNC=os.path.join(shim, "rsync"),
                      COPYOFF_SSH=os.path.join(shim, "ssh"), COPYOFF_REMOTE_SCRIPT=me)
    copyoff.seal_closed(arc_dir, outbox, settle_s=60)
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)
    done, fail = copyoff.ship(arc_dir, outbox, f"vps.example:{vps}")
    ledger = copyoff.load_ledger(outbox)
    ino = next(iter(ledger["live"]))
    check(f"remote ship verified via ssh receipt ({done} ok, {fail} failed)",
          fail == 0 and done == 4 and ledger["live"][ino]["verified_through_id"] == 2500)
    check("receipts written on the receiver",
          len([n for n in os.listdir(vps) if n.endswith(".verified.json")]) == 4)
    for n in ("COPYOFF_RSYNC", "COPYOFF_SSH", "COPYOFF_REMOTE_SCRIPT"):
        del os.environ[n]

print("the archiver's prune, gated on the ledger")
with tempfile.TemporaryDirectory() as tmp:
    arc_dir, conn = fake_pi(tmp, n=4000)            # all 30 days old, no sessions: all prunable
    eng = os.path.join(tmp, "engine.db")
    ec = sqlite3.connect(eng)
    ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, race_id TEXT, "
               "kind TEXT, start_ts REAL, end_ts REAL)")
    ec.commit()
    ec.close()
    live = os.path.join(arc_dir, "archive.db")
    outbox = os.path.join(tmp, "outbox")
    os.makedirs(outbox)
    led = os.path.join(outbox, "ledger.json")
    count = lambda: conn.execute("SELECT count(*) FROM readings").fetchone()[0]  # noqa: E731
    check("no ledger file -> prune SKIPPED (None), nothing deleted",
          archiver.prune(conn, engine_db=eng, retain_days=7, offboat_ledger=led,
                         archive_path=live) is None and count() == 4000)
    copyoff.seal_segments(arc_dir, outbox, max_rows=1000)
    copyoff.ship(arc_dir, outbox, os.path.join(tmp, "vps"))
    ledger = copyoff.load_ledger(outbox)
    ino = str(os.stat(live).st_ino)
    ledger["live"][ino]["verified_through_id"] = 2500        # pretend the tail is not yet off
    copyoff.save_ledger(outbox, ledger)
    n = archiver.prune(conn, engine_db=eng, retain_days=7, chunk_rows=300, pause_s=0,
                       offboat_ledger=led, archive_path=live)
    check(f"prune deletes only verified rows ({n} deleted)",
          n == 2500 and conn.execute("SELECT min(id) FROM readings").fetchone()[0] == 2501)
    ledger["live"] = {"999999": {"verified_through_id": 10**9}}   # a different (old) file
    copyoff.save_ledger(outbox, ledger)
    check("a ledger for another archive file (a rotation) deletes nothing",
          archiver.prune(conn, engine_db=eng, retain_days=7, offboat_ledger=led,
                         archive_path=live) == 0 and count() == 1500)
    with open(led, "w") as f:
        f.write("{not json")
    check("an unreadable ledger -> prune SKIPPED",
          archiver.prune(conn, engine_db=eng, retain_days=7, offboat_ledger=led,
                         archive_path=live) is None and count() == 1500)
    check("no gate configured -> the old behaviour (everything out of session goes)",
          archiver.prune(conn, engine_db=eng, retain_days=7, offboat_ledger="",
                         archive_path=live) == 1500)
    conn.close()

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
