#!/usr/bin/env python3
"""Copy the Pi's full-res archive off the boat, and PROVE each copy, before anything may delete
it (#181).

Goal (Cole): lose no telemetry; copy all of it off the Pi; delete from the boat only after an
off-boat copy is sha256-verified. Until now the full-res archive lived only on the SD card.

Two kinds of thing leave the boat:
  - SEGMENTS of the live `archive.db`: every row by id, in order, `--max-rows` at a time, written
    to a standalone SQLite file in the outbox. The live file is opened read-only; the archiver
    keeps recording. Rows are hashed as they are read from the archive (the `content_sha256`), and
    the segment is re-read and re-hashed after it is written, so a bad write on the SD card is
    caught on the boat. The cursor is per archive FILE (its inode): after a corruption rotation the
    fresh file starts again from id 1, and the old file ships as a closed file.
  - CLOSED FILES: every other `*.db` in the archive dir (the `archive.corrupt-<stamp>.db` a
    rotation leaves). They do not change, so they ship in place, as they are; no outbox copy.

Every artifact has a JSON sidecar with its hashes. `ship` copies artifact + sidecar to the
destination and runs `verify` THERE: the receiver re-hashes the bytes, and for a segment re-reads
every row and recomputes the content hash. Only a receipt that matches the boat's own numbers
counts. Then the ledger records it, and a segment's outbox copy is deleted (it is a copy; the rows
are still in archive.db).

The ledger's `verified_through_id` (per archive inode, contiguous from the start) is what the
archiver's retention prune may read (ARCHIVE_OFFBOAT_LEDGER): with it set, the prune deletes only
rows that are already verified off the boat. This tool itself NEVER deletes or writes anything in
the archive: it opens archive.db read-only and never touches a closed file.

Commands:
  copyoff.py seal   --archive-dir D --outbox O [--max-rows N] [--max-segments K] [--max-outbox-mb M]
  copyoff.py ship   --archive-dir D --outbox O --dest DIR | --dest host:DIR [--verify-cmd ...]
  copyoff.py verify DIR NAME            (runs on the receiving side; prints a JSON receipt)
  copyoff.py status --outbox O

Remote destinations use rsync (-z: compression on the wire) and ssh; override the binaries with
COPYOFF_RSYNC / COPYOFF_SSH. On the receiver, `verify` needs only python3 and this file
(COPYOFF_REMOTE_SCRIPT, default /home/constantineau/Agent_C4/pi/archiver/copyoff.py).

Standard library only, so it runs on the Pi host (python3) or in the archiver image.
"""
import argparse
import contextlib
import fcntl
import functools
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

LIVE = "archive.db"
LEDGER = "ledger.json"
CHUNK = 1 << 20
COLS = ("id", "time", "boat_id", "source", "path", "value", "str_value")
SEGMENT_SCHEMA = """
CREATE TABLE readings (
    id INTEGER PRIMARY KEY, time TEXT NOT NULL, boat_id TEXT NOT NULL, source TEXT NOT NULL,
    path TEXT NOT NULL, value REAL, str_value TEXT);
CREATE TABLE segment (k TEXT PRIMARY KEY, v TEXT);
"""


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} copyoff: {msg}",
          flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def row_line(row):
    """One row, canonically: the same bytes on the boat and on the receiver."""
    return (json.dumps(list(row), separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def segment_content(conn):
    """(rows, content_sha256, min_time, max_time) of a segment DB, read in id order."""
    h, n, lo, hi = hashlib.sha256(), 0, None, None
    for row in conn.execute(f"SELECT {', '.join(COLS)} FROM readings ORDER BY id"):
        h.update(row_line(row))
        n += 1
        lo = row[1] if lo is None or row[1] < lo else lo
        hi = row[1] if hi is None or row[1] > hi else hi
    return n, h.hexdigest(), lo, hi


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_ledger(outbox):
    p = os.path.join(outbox, LEDGER)
    if not os.path.exists(p):
        return {"live": {}, "closed": {}}
    with open(p) as f:
        return json.load(f)


def save_ledger(outbox, ledger):
    _write_json(os.path.join(outbox, LEDGER), ledger)        # tmp + fsync + rename


@contextlib.contextmanager
def outbox_lock(outbox):
    """One seal or ship at a time per outbox. Both read-modify-write the ledger: without this a
    seal that loaded it before a ship saved would write back the ship's verified ranges as
    missing (after the ship deleted those segments), or the reverse, a stale cursor."""
    os.makedirs(outbox, exist_ok=True)
    with open(os.path.join(outbox, ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked(fn):
    """Hold the outbox lock for the WHOLE operation (`outbox` is the second argument)."""
    @functools.wraps(fn)
    def wrapper(archive_dir, outbox, *a, **kw):
        with outbox_lock(outbox):
            return fn(archive_dir, outbox, *a, **kw)
    return wrapper


def contiguous_through(ranges):
    """Highest id H such that every id in 1..H lies in a verified range (ranges = [[a, b]])."""
    top = 0
    for a, b in sorted(ranges):
        if a > top + 1:
            break
        top = max(top, b)
    return top


# ---- seal ------------------------------------------------------------------------------------

def outbox_bytes(outbox):
    return sum(os.path.getsize(os.path.join(outbox, n)) for n in os.listdir(outbox)
               if n.startswith("seg-"))


@_locked
def seal_segments(archive_dir, outbox, max_rows=2_000_000, max_segments=50, max_outbox_mb=4096):
    """Cut the next id ranges of the live archive into outbox segments. Returns segments made."""
    live = os.path.join(archive_dir, LIVE)
    if not os.path.exists(live):
        log(f"no {LIVE} in {archive_dir}")
        return 0
    if os.path.exists(os.path.join(archive_dir, ROTATING_MARKER)):
        log("a rotation is in progress (archive.db.rotating) — not sealing now")
        return 0
    # The cursor belongs to an inode, so the file actually OPENED must be the one whose inode
    # we look up. Stat the path, open and read it, stat again: if both stats name the same
    # inode, the open file is that inode (a rotation in between would have replaced the path
    # with a NEW file, and the old inode lives on as archive.corrupt-*, so it can't come back
    # under the name). Otherwise do nothing this run; the next one binds to the fresh file.
    before = os.stat(live).st_ino
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True, timeout=30)
    try:
        src.execute("SELECT 1 FROM readings LIMIT 1").fetchall()   # SQLite opens lazily
        after = os.stat(live).st_ino
    except (OSError, sqlite3.Error) as exc:
        src.close()
        log(f"{LIVE} could not be opened ({exc}) — not sealing now")
        return 0
    if before != after:
        src.close()
        log(f"{LIVE} was replaced while being opened (inode {before} -> {after}, a rotation) "
            f"— not sealing now; the next run starts the new file from id 1")
        return 0
    ino = str(before)
    ledger = load_ledger(outbox)
    ent = ledger["live"].setdefault(ino, {"sealed_through_id": 0, "verified": [],
                                          "first_seen": _now()})
    made = 0
    try:
        while made < max_segments:
            if outbox_bytes(outbox) > max_outbox_mb * 1048576:
                log(f"outbox over {max_outbox_mb} MB — not sealing more until it ships")
                break
            cur = ent["sealed_through_id"]
            # One read transaction: the rows and max(id) come from the same snapshot.
            src.execute("BEGIN")
            try:
                rows = src.execute(f"SELECT {', '.join(COLS)} FROM readings WHERE id > ? "
                                   f"ORDER BY id LIMIT ?", (cur, max_rows)).fetchall()
            finally:
                src.execute("COMMIT")
            if not rows:
                break
            first, last = rows[0][0], rows[-1][0]
            name = f"seg-{ino}-{first:012d}-{last:012d}.db"
            path = os.path.join(outbox, name)
            tmp = path + ".part"
            if os.path.exists(tmp):
                os.remove(tmp)
            h = hashlib.sha256()
            for r in rows:
                h.update(row_line(r))
            want = h.hexdigest()
            seg = sqlite3.connect(tmp)
            # A scratch file: one fsync at the end (below) and a full re-read are the guarantee,
            # not SQLite's journal — and it spares the SD card a journal's worth of writes.
            seg.execute("PRAGMA journal_mode=OFF")
            seg.execute("PRAGMA synchronous=OFF")
            seg.executescript(SEGMENT_SCHEMA)
            seg.executemany(f"INSERT INTO readings ({', '.join(COLS)}) VALUES (?,?,?,?,?,?,?)",
                            rows)
            meta = {"archive_inode": ino, "first_id": first, "last_id": last,
                    "cursor_before": cur}
            seg.executemany("INSERT INTO segment VALUES (?, ?)",
                            [(k, str(v)) for k, v in meta.items()])
            seg.commit()
            n, got, lo, hi = segment_content(seg)
            seg.close()
            if got != want or n != len(rows):
                os.remove(tmp)
                raise RuntimeError(f"{name}: the segment written to disk does not read back as "
                                   f"the rows read from the archive — not sealing (bad card?)")
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp, path)
            side = {"kind": "segment", "name": name, "sha256": sha256_file(path),
                    "bytes": os.path.getsize(path), "rows": n, "content_sha256": got,
                    "min_time": lo, "max_time": hi, "sealed": _now(), **meta}
            _write_json(path + ".json", side)
            # Cursor moves only once the segment and its sidecar are whole on disk. The rows from
            # `cur` to `first` that are not there were pruned or never written; nothing to copy.
            ent["sealed_through_id"] = last
            save_ledger(outbox, ledger)
            made += 1
            log(f"sealed {name}: {n} rows {lo} -> {hi}, {side['bytes'] / 1048576:.1f} MiB")
    finally:
        src.close()
    return made


# What a rotation leaves beside a moved-aside db (archiver.rotate_corrupt moves all three).
COMPANIONS = ("-wal", "-shm")
ROTATING_MARKER = LIVE + ".rotating"


def closed_files(archive_dir):
    """{db name: [member names]} for every closed SQLite file: the db plus whichever of its -wal
    and -shm exist. They are ONE unit: rows committed only in the -wal are not in the .db, so a
    .db shipped without its -wal is not a copy of what the card holds."""
    names = set(os.listdir(archive_dir))
    out = {}
    for n in sorted(names):
        p = os.path.join(archive_dir, n)
        if (n == LIVE or not n.endswith(".db") or not os.path.isfile(p)
                or os.path.islink(p)):
            continue
        out[n] = [n] + [n + c for c in COMPANIONS if n + c in names]
    for n in sorted(names):
        for c in COMPANIONS:
            if n.endswith(".db" + c) and n[:-len(c)] not in names and n[:-len(c)] != LIVE:
                log(f"!! {n}: a {c} with no {n[:-len(c)]} beside it — not shipped, look at it")
    return out


def _unit_stat(archive_dir, members):
    return [(m, *(lambda st: (st.st_size, st.st_mtime_ns, st.st_ino))(
        os.stat(os.path.join(archive_dir, m)))) for m in members]


def unit_sha256(members):
    """One hash over a unit's members: "<sha256>  <name>" lines, sorted by name."""
    h = hashlib.sha256()
    for m in sorted(members, key=lambda x: x["name"]):
        h.update(f"{m['sha256']}  {m['name']}\n".encode())
    return h.hexdigest()


@_locked
def seal_closed(archive_dir, outbox, settle_s=600):
    """Hash each closed unit (in place, read-only) and write its sidecar to the outbox."""
    if os.path.exists(os.path.join(archive_dir, ROTATING_MARKER)):
        log("a rotation is in progress (archive.db.rotating) — not sealing closed files now")
        return 0
    ledger = load_ledger(outbox)
    made = 0
    for n, members in closed_files(archive_dir).items():
        st = _unit_stat(archive_dir, members)
        age = time.time() - max(x[2] for x in st) / 1e9
        if age < settle_s:
            log(f"{n}: changed {age:.0f} s ago — waiting for it to settle")
            continue
        # Keyed by what the unit IS on disk (members, sizes, mtimes, inodes): a -wal that turns
        # up later, or a file that changes, is a new unit and ships again.
        key = f"{n}:" + hashlib.sha256(json.dumps(st).encode()).hexdigest()[:16]
        if key in ledger["closed"]:
            continue
        hashed = [{"name": m, "sha256": sha256_file(os.path.join(archive_dir, m)),
                   "bytes": os.path.getsize(os.path.join(archive_dir, m))} for m in members]
        if _unit_stat(archive_dir, members) != st:
            log(f"{n}: changed while being hashed — not sealing (it is not closed)")
            continue
        digest = unit_sha256(hashed)
        side = {"kind": "closed", "name": n, "members": hashed, "sha256": digest,
                "bytes": sum(m["bytes"] for m in hashed), "key": key,
                "archive_inode": str(os.stat(os.path.join(archive_dir, n)).st_ino),
                "sealed": _now()}
        _write_json(os.path.join(outbox, f"closed-{n}.json"), side)
        ledger["closed"][key] = {"name": n, "members": hashed, "sha256": digest,
                                 "verified": None}
        save_ledger(outbox, ledger)
        made += 1
        log(f"sealed closed unit {n} ({len(hashed)} file(s), {side['bytes'] / 1048576:.1f} MiB)")
    return made


# ---- verify (receiver side) ------------------------------------------------------------------

def verify(dir_, name):
    """Check one received artifact against its sidecar. Returns the receipt (and writes it)."""
    side_path = os.path.join(dir_, (f"closed-{name}" if not name.startswith("seg-") else name)
                             + ".json")
    with open(side_path) as f:
        side = json.load(f)
    path = os.path.join(dir_, name)
    rec = {"name": name, "kind": side["kind"], "checked": _now(), "ok": False}
    if side["kind"] == "closed":
        # Every member of the unit (db + -wal + -shm), each against the boat's hash.
        got = []
        for m in side["members"]:
            p = os.path.join(dir_, m["name"])
            if not os.path.isfile(p):
                rec["error"] = f"missing {m['name']}"
                return rec
            got.append({"name": m["name"], "sha256": sha256_file(p), "bytes": os.path.getsize(p)})
        rec["members"] = got
        rec["sha256"] = unit_sha256(got)
        rec["bytes"] = sum(m["bytes"] for m in got)
        rec["ok"] = (sorted(got, key=lambda m: m["name"])
                     == sorted(side["members"], key=lambda m: m["name"])
                     and rec["sha256"] == side["sha256"])
        _write_json(path + ".verified.json", rec)
        return rec
    if not os.path.isfile(path):
        rec["error"] = "missing"
        return rec
    rec["sha256"] = sha256_file(path)
    rec["bytes"] = os.path.getsize(path)
    good = rec["sha256"] == side["sha256"] and rec["bytes"] == side["bytes"]
    if side["kind"] == "segment" and good:
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            rec["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            rec["rows"], rec["content_sha256"], _, _ = segment_content(c)
            c.close()
        except sqlite3.DatabaseError as exc:
            rec["error"] = str(exc)
            good = False
        good = (good and rec.get("integrity") == "ok" and rec.get("rows") == side["rows"]
                and rec.get("content_sha256") == side["content_sha256"])
    rec["ok"] = bool(good)
    _write_json(path + ".verified.json", rec)
    return rec


# ---- ship ------------------------------------------------------------------------------------

def _is_remote(dest):
    return ":" in dest and not dest.startswith("/")


def _transfer(src, dest, name):
    """Copy one file to the destination under `name` (atomic at the receiver)."""
    if _is_remote(dest):
        rsync = shlex.split(os.environ.get("COPYOFF_RSYNC", "rsync"))
        subprocess.run([*rsync, "-z", "--partial", "--inplace", "--timeout=300", src,
                        f"{dest.rstrip('/')}/{name}"], check=True)
    else:
        os.makedirs(dest, exist_ok=True)
        tmp = os.path.join(dest, f".{name}.part")
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(dest, name))


def _remote_verify(dest, name):
    if not _is_remote(dest):
        return verify(dest, name)
    host, dir_ = dest.split(":", 1)
    ssh = shlex.split(os.environ.get("COPYOFF_SSH", "ssh"))
    script = os.environ.get("COPYOFF_REMOTE_SCRIPT",
                            "/home/constantineau/Agent_C4/pi/archiver/copyoff.py")
    r = subprocess.run([*ssh, host, f"python3 {shlex.quote(script)} verify "
                        f"{shlex.quote(dir_)} {shlex.quote(name)}"],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"name": name, "ok": False, "error": (r.stderr or r.stdout).strip()[-300:]}


@_locked
def ship(archive_dir, outbox, dest):
    """Send everything sealed and not yet verified. Returns (verified, failed)."""
    ledger = load_ledger(outbox)
    done = fail = 0
    for side_name in sorted(n for n in os.listdir(outbox) if n.endswith(".json")
                            and n != LEDGER and not n.endswith(".verified.json")):
        with open(os.path.join(outbox, side_name)) as f:
            side = json.load(f)
        name = side["name"]
        if side["kind"] == "segment":
            src = os.path.join(outbox, name)
            if not os.path.exists(src):
                continue                      # shipped and cleaned up already
            files = [(src, name)]
        else:
            if ledger["closed"].get(side["key"], {}).get("verified"):
                continue
            src = os.path.join(archive_dir, name)
            files = [(os.path.join(archive_dir, m["name"]), m["name"]) for m in side["members"]]
        try:
            for f_src, f_name in files:
                _transfer(f_src, dest, f_name)
            _transfer(os.path.join(outbox, side_name), dest, side_name)
            rec = _remote_verify(dest, name)
        except (OSError, subprocess.CalledProcessError) as exc:
            rec = {"ok": False, "error": str(exc)}
        # Our numbers, not just the receiver's "ok": the receipt must name the same bytes/rows.
        match = (rec.get("ok") and rec.get("sha256") == side["sha256"]
                 and (side["kind"] != "closed"
                      or sorted(rec.get("members") or [], key=lambda m: m["name"])
                      == sorted(side["members"], key=lambda m: m["name"]))
                 and (side["kind"] != "segment"
                      or (rec.get("content_sha256") == side["content_sha256"]
                          and rec.get("rows") == side["rows"])))
        if not match:
            fail += 1
            log(f"!! {name}: NOT verified off the boat ({rec.get('error') or 'hash mismatch'}) — "
                f"kept, will retry")
            continue
        stamp = {"at": _now(), "dest": dest, "sha256": rec["sha256"]}
        if side["kind"] == "segment":
            ent = ledger["live"].setdefault(side["archive_inode"],
                                            {"sealed_through_id": 0, "verified": []})
            # The segment covers (cursor_before, last_id]: ids in there that it lacks did not
            # exist in the archive when it was cut, so there is nothing else to verify for them.
            ent["verified"].append([side["cursor_before"] + 1, side["last_id"]])
            ent["verified"] = _merge(ent["verified"])
            ent["verified_through_id"] = contiguous_through(ent["verified"])
            ent["last_verified"] = stamp
            save_ledger(outbox, ledger)
            os.remove(src)                    # the outbox copy only; archive.db is untouched
            os.remove(os.path.join(outbox, side_name))
        else:
            ledger["closed"][side["key"]]["verified"] = stamp
            save_ledger(outbox, ledger)
        done += 1
        log(f"{name}: verified off the boat at {dest} (sha256 {rec['sha256'][:16]}…)")
    return done, fail


def _merge(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal")
    s.add_argument("--archive-dir", required=True)
    s.add_argument("--outbox", required=True)
    s.add_argument("--max-rows", type=int, default=2_000_000)
    s.add_argument("--max-segments", type=int, default=50)
    s.add_argument("--max-outbox-mb", type=int, default=4096)
    s.add_argument("--settle-s", type=int, default=600)
    p = sub.add_parser("ship")
    p.add_argument("--archive-dir", required=True)
    p.add_argument("--outbox", required=True)
    p.add_argument("--dest", required=True)
    v = sub.add_parser("verify")
    v.add_argument("dir")
    v.add_argument("name")
    st = sub.add_parser("status")
    st.add_argument("--outbox", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "verify":
        rec = verify(a.dir, a.name)
        print(json.dumps(rec, sort_keys=True))
        return 0 if rec["ok"] else 1
    os.makedirs(a.outbox, exist_ok=True)
    if a.cmd == "seal":
        seal_closed(a.archive_dir, a.outbox, a.settle_s)
        seal_segments(a.archive_dir, a.outbox, a.max_rows, a.max_segments, a.max_outbox_mb)
        return 0
    if a.cmd == "ship":
        _, fail = ship(a.archive_dir, a.outbox, a.dest)
        return 1 if fail else 0
    print(json.dumps(load_ledger(a.outbox), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
