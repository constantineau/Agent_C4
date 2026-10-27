#!/usr/bin/env python3
"""SR33 full-resolution local archive (Pi onboard) — boat is the source of truth.

A silent second subscriber to the Signal K WebSocket delta stream. Where the uplink
forwards 15-s *aggregates* to the cloud, this records EVERY delta verbatim into a durable
local SQLite database on the Pi. Nothing is averaged, downsampled, or dropped — the local
archive is the gold-standard full-resolution log. The cloud gets aggregates live and the
full log post-passage (see backfill.py).

Why a separate service from the uplink: the archive must survive things the uplink can't.
A crashed uplink, a dropped Starlink link, or a full disk queue never costs archived data
because this process owns its own subscription and its own crash-safe store. "Link outage
loses nothing" is the design promise; this is the piece that keeps it.

The SQLite schema mirrors the cloud `telemetry_raw(time, boat_id, source, path, value,
str_value)` so a post-passage backfill is a straight copy. Object values (position,
attitude) are flattened into dotted numeric sub-paths exactly as the uplink does, so the
archive and the live aggregates use identical path naming once they reach the cloud.

Identical on bench and boat; the only difference is CAN_IFACE (vcan0 vs can0), upstream of
Signal K.
"""
import asyncio
import errno
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

# The retention prune keeps race-session windows, and a window derived from the record beats one
# derived from a button press (see `session_windows`). Guarded: an image built before `shared/`
# was copied in must still record — it just prunes on the markers alone, loudly.
try:
    from shared import n2k_sources, race_window
except ImportError:                     # pragma: no cover - image without shared/
    n2k_sources = race_window = None

# Writes are offloaded to executor threads (fsync on a slow SD card shouldn't stall the
# event loop), so the connection is shared across threads and a lock serializes writers.
_WRITE_LOCK = threading.Lock()

SIGNALK_WS = os.environ.get(
    "SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all"
)
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
ARCHIVE_DB = Path(os.environ.get("ARCHIVE_DB", "/var/lib/sr33/archive/archive.db"))
# Crash-safety vs. throughput: flush the buffer to disk whenever it reaches FLUSH_ROWS or
# every FLUSH_SECONDS, whichever comes first. With WAL + synchronous=FULL each flush fsyncs,
# so at most the last (<FLUSH_SECONDS) of readings is at risk on power loss — acceptable for
# a full-res archive, and one fsync/second is free.
FLUSH_ROWS = int(os.environ.get("ARCHIVE_FLUSH_ROWS", "1000"))
FLUSH_SECONDS = float(os.environ.get("ARCHIVE_FLUSH_SECONDS", "2"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    time      TEXT NOT NULL,   -- ISO8601 UTC (Signal K source timestamp when available)
    boat_id   TEXT NOT NULL,
    source    TEXT NOT NULL,   -- Signal K $source (bus.address / device label)
    path      TEXT NOT NULL,   -- Signal K path, e.g. navigation.headingMagnetic
    value     REAL,            -- numeric SI value as Signal K provides it
    str_value TEXT             -- non-numeric values (mode strings, etc.)
);
CREATE INDEX IF NOT EXISTS readings_time_idx      ON readings(time);
CREATE INDEX IF NOT EXISTS readings_path_time_idx ON readings(path, time);

-- Backfill bookkeeping: how far the cloud has been caught up (see backfill.py).
CREATE TABLE IF NOT EXISTS sync_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def open_db(path: Path = ARCHIVE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")     # readers (backfill) never block the writer
    conn.execute("PRAGMA synchronous=FULL")     # durable across power loss — boat-grade
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---- BROWNOUT TOLERANCE -------------------------------------------------------------------
# WAL + synchronous=FULL above protects the *last flush* against power loss. It does not
# protect against the SD card itself going bad, which on this boat is the common case: three
# SQLite corruptions in six weeks (archive.corrupt-20260718, archive.corrupt-20260830, then
# live pages), and REMOTE_OPS.md already lists SD mortality as an accepted risk.
#
# What actually cost the data was the response, not the corruption. On 2026-07-18 at 20:40:30Z
# — at the bottom of a six-hour discharge to 11.08 V, see vps/agent/app/power.py — the archive
# went malformed mid-race. `open_db()` raised at startup, the process exited, Docker restarted
# it, and that repeated 48 times: the archiver recorded NOTHING from Jul 19 until a human
# applied the fix by hand on Aug 30. Six weeks of full-resolution telemetry, lost to a failure
# the code already knew how to survive — `CREATE TABLE IF NOT EXISTS` self-initialises a fresh
# DB, so all that was ever needed was to move the bad file aside and reopen.
#
# So: check on startup, rotate on detection, never exit, and never delete. The corrupt file is
# KEPT — pi/archiver/tools/salvage.py recovered 99.9994% of one of them, which is how the
# Jul 15–17 archive exists at all.
CORRUPT_MARKERS = ("malformed", "not a database", "disk image")


def _is_corruption(exc: Exception) -> bool:
    """True for the errors that mean the FILE is bad, not that the statement was."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(m in str(exc).lower() for m in CORRUPT_MARKERS)


def integrity_ok(conn) -> bool:
    """`PRAGMA quick_check` — the cheap variant (skips per-row index cross-checks), which still
    catches the malformed-page case and returns in well under a second on a multi-GB archive."""
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


_SIDECARS = ("", "-wal", "-shm")


def _fsync_dir(d):
    """fsync a DIRECTORY, which is what makes a rename/create/unlink in it survive a power cut
    (fsyncing the file alone does not). Two of this archive's four corruptions were power cuts.
    A filesystem that cannot fsync a directory (EINVAL) is logged, not fatal."""
    fd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
        print(f"[archive] cannot fsync directory {d} ({exc}); continuing", flush=True)
    finally:
        os.close(fd)


def _durable_write(path: Path, text: str):
    """Write `text` to `path` so that after a power cut it is either the old file or the
    complete new one: temp file, fsync it, rename into place, fsync the directory."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _rotation_marker(path: Path) -> Path:
    """`archive.db.rotating`: names the destination of a rotation that has started and not
    finished. Lives beside the archive so a restarted process finishes the same rotation."""
    return path.with_name(path.name + ".rotating")


def _present(path: Path):
    return [sfx for sfx in _SIDECARS if Path(str(path) + sfx).exists()]


def rotate_corrupt(path: Path = ARCHIVE_DB, stamp=None):
    """Move a corrupt archive (and its -wal/-shm) aside and return the new path, or None when
    there was nothing at `path` to move.

    Named `archive.corrupt-<YYYYmmddTHHMMSS>.db`, matching the two files a human created by
    hand for exactly this. Kept, never deleted: salvage recovers almost all of it.

    RESUMABLE. The three renames are three steps, and any one can fail (or the process can die
    between them). A db moved without its -wal leaves committed rows beside the ORIGINAL name,
    where a fresh archive would pick them up as its own WAL. So the destination is written to
    `archive.db.rotating` before the first rename and removed only after the last; a call that
    finds the marker finishes moving whatever is still at `path` to that same destination.

    DURABLE, because the failure this exists for is a power cut: the marker is written durably
    (temp + fsync + rename + directory fsync) BEFORE anything moves; the directory is fsynced
    after the moves; the marker is removed and the directory fsynced again before returning —
    so before any caller opens a fresh archive. Without that, a brownout can keep the db rename
    and lose the marker's contents, and the restart parks the WAL somewhere else."""
    marker = _rotation_marker(path)
    dest = None
    try:
        name = marker.read_text().strip()
        dest = path.with_name(name) if name else None
    except FileNotFoundError:
        pass
    if dest is not None:
        print(f"[archive] finishing an interrupted rotation -> {dest.name}", flush=True)
    else:
        if not _present(path):
            return None
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        # Second-resolution names collide when two rotations land in the same second, and a
        # plain rename would then DELETE the first corrupt archive — the one thing this must
        # never do. (Caught by test_brownout, which rotated twice in one second.)
        n = 1
        while any(Path(str(dest) + sfx).exists() for sfx in _SIDECARS):
            dest = path.with_name(f"{path.stem}.corrupt-{stamp}-{n}{path.suffix}")
            n += 1
        _durable_write(marker, dest.name)
    for sfx in _SIDECARS:
        src = Path(str(path) + sfx)
        if not src.exists():
            continue
        target = Path(str(dest) + sfx)
        if target.exists():               # never overwrite a kept file; park beside it
            n = 1
            while Path(f"{target}.{n}").exists():
                n += 1
            target = Path(f"{target}.{n}")
            print(f"[archive] {target.name}: destination already taken, kept beside it",
                  flush=True)
        src.rename(target)                # raises on failure: the marker stays, retry resumes
    _fsync_dir(path.parent)               # the moves are on disk...
    marker.unlink(missing_ok=True)
    _fsync_dir(path.parent)               # ...and so is the marker's removal
    print(f"[archive] CORRUPT archive moved aside -> {dest.name}; starting a fresh one. "
          f"KEEP that file: pi/archiver/tools/salvage.py recovers ~all of it.", flush=True)
    return dest


def _clear_for_fresh(path: Path):
    """Before opening `path`: finish an interrupted rotation, and move aside a -wal/-shm left
    at the path with no database (SQLite would replay a foreign WAL into the new file). Returns
    where things went, or None when there was nothing to do."""
    if _rotation_marker(path).exists() or (not path.exists() and _present(path)):
        return rotate_corrupt(path)
    return None


def open_db_resilient(path: Path = ARCHIVE_DB):
    """Open the archive, rotating it aside if it is unusable. Returns (conn, rotated_to|None).

    This is the whole brownout story: a bad card must cost minutes, not six weeks."""
    rotated = None
    conn = None
    try:
        finished = _clear_for_fresh(path)
        conn = open_db(path)
        if finished is not None:
            _note_rotation(conn, finished)
            return conn, finished
        if not integrity_ok(conn):
            raise sqlite3.DatabaseError("quick_check failed: database disk image is malformed")
        return conn, None
    except sqlite3.DatabaseError as exc:
        if not _is_corruption(exc):
            raise
        print(f"[archive] {path.name} is unusable ({exc})", flush=True)
        # the same close -> resumable move -> check -> open sequence as a mid-run rotation;
        # CREATE TABLE IF NOT EXISTS self-initialises the fresh file
        return reopen_fresh(conn, path)


def reopen_fresh(conn, path: Path = ARCHIVE_DB):
    """Rotate the archive aside WITHOUT re-checking it, and open a fresh one. Returns
    (conn, rotated_to).

    For when a statement has already raised a corruption error. `open_db_resilient` gates on
    `PRAGMA quick_check`, and the boat's archive.db (2026-09-02) is the case where that gate is
    not enough: a file can pass quick_check and still fail a full `count(*)` or a write. Asking
    quick_check again would hand back the same broken file and drop every flush, forever. The
    write already told us the file is bad; believe it.

    Order matters: CLOSE the old connection first, then move the file and its -wal/-shm, then
    open the fresh one. A WAL connection's close cleans up the sidecars BY NAME
    (`archive.db-wal`/`-shm`); closed after the move, those names belong to the fresh archive.
    SQLite 3.46 notices the move and skips that cleanup (verified), but the archive's safety
    should not rest on that. The cost is a window with no connection at all: the caller sets
    its handle to None BEFORE calling, and None is a handled state (disk guard: free space
    unknown; pruner: skips; next flush: retries this). A half-done move (db moved, -wal not)
    is finished to the same destination before any fresh file is opened — see
    `rotate_corrupt`."""
    if conn is not None:
        # Under the write lock: the pruner runs on another thread and may be mid-DELETE on this
        # very connection. Closing it underneath that is undefined; waiting for it is not.
        with _WRITE_LOCK:
            try:
                conn.close()
            except Exception as exc:      # a corrupt file may fail its close-time checkpoint
                print(f"[archive] closing the corrupt archive raised ({exc}); moving it anyway",
                      flush=True)
    rotated = rotate_corrupt(path)        # resumes a half-done one; None if nothing is left
    left = _present(path)
    if left or _rotation_marker(path).exists():
        raise OSError(f"rotation incomplete, still at {path.name}: {left}")
    new = open_db(path)
    if rotated is not None:
        _note_rotation(new, rotated)
    return new, rotated


def open_for_run(path: Path = ARCHIVE_DB):
    """Startup open: `open_db_resilient`, then the row count the startup log prints. If the
    count itself hits corruption, rotate unconditionally — that `count(*)` is the exact line
    the boat crash-looped on 4,000+ times after 2026-09-02. Returns (conn, rows, rotated)."""
    conn, rotated = open_db_resilient(path)
    try:
        n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        if not _is_corruption(exc):
            raise
        print(f"[archive] {path.name} passed quick_check but count(*) failed ({exc}) — "
              f"rotating it anyway", flush=True)
        conn, rotated = reopen_fresh(conn, path)
        n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    return conn, n, rotated


def _note_rotation(conn, rotated: Path):
    """Record the rotation in `sync_state` so it is visible to backfill and the engine — a
    silent recovery would be almost as bad as a silent failure, because a fresh archive looks
    exactly like a boat that has not sailed yet."""
    try:
        with _WRITE_LOCK:
            row = conn.execute("SELECT v FROM sync_state WHERE k='corrupt_rotations'").fetchone()
            n = int(row[0]) + 1 if row and str(row[0]).isdigit() else 1
            conn.execute("INSERT OR REPLACE INTO sync_state (k, v) VALUES ('corrupt_rotations', ?)",
                         (str(n),))
            conn.execute("INSERT OR REPLACE INTO sync_state (k, v) "
                         "VALUES ('last_corrupt_rotation', ?)",
                         (f"{datetime.now(timezone.utc).isoformat()} {rotated.name}",))
            conn.commit()
    except Exception as exc:          # bookkeeping must never stop the recorder
        print(f"[archive] could not record the rotation: {exc}", flush=True)


def flatten(time, source, path, value, out):
    """Append one or more archive rows for a Signal K value, matching uplink flattening."""
    if isinstance(value, bool):
        out.append((time, BOAT_ID, source, path, None, str(value).lower()))
    elif isinstance(value, (int, float)):
        out.append((time, BOAT_ID, source, path, float(value), None))
    elif isinstance(value, dict):
        for k, sub in value.items():
            if isinstance(sub, (int, float)) and not isinstance(sub, bool):
                out.append((time, BOAT_ID, source, f"{path}.{k}", float(sub), None))
    elif isinstance(value, str) and value:
        out.append((time, BOAT_ID, source, path, None, value))


def _mmsi_from_context(ctx):
    """Pull the numeric MMSI out of an AIS vessel context urn, else None.

    e.g. 'vessels.urn:mrn:imo:mmsi:366123456' -> 366123456. Own ship is a uuid context, so it
    (correctly) returns None. Mirrors pi/uplink/uplink.py."""
    if ctx and "mmsi:" in ctx:
        tail = ctx.split("mmsi:")[-1].strip()
        return int(tail) if tail.isdigit() else None
    return None


def _is_other_vessel(ctx, self_ctx):
    """True when this delta describes a vessel or navigation aid that is NOT us.

    `subscribe=all` delivers own-ship deltas AND every AIS target on one socket. Without this
    test the archiver wrote them all under the single own-ship BOAT_ID, so `navigation.position`
    in the archive was a mix of this boat and whatever shipping was in range — on Jul 18 that
    made 17% of own-ship position reads another vessel, with implied speeds to 171,000 kn and a
    longitude of -2.4 deg (the Atlantic, not Lake Huron).

    Deliberately conservative: silently DROPPING own-ship telemetry is far worse than keeping
    the odd AIS row, so anything not positively identifiable as someone else is kept.
      - no context               -> own ship (Signal K omits it for self)
      - context == self          -> own ship
      - self known and differs   -> someone else
      - self not yet known       -> drop only when the context names an MMSI (an AIS target)
    """
    if not ctx:
        return False
    if self_ctx:
        return ctx != self_ctx
    return _mmsi_from_context(ctx) is not None


def parse_delta(msg, default_time, state=None):
    """Turn one Signal K delta message into a list of archive rows (full resolution).

    `state` carries the `self` context learned from the hello frame, so AIS traffic can be told
    apart from own-ship data; pass a dict to enable the filter (and to count what it drops)."""
    try:
        data = json.loads(msg)
    except ValueError:
        return []
    # The hello frame names the self context — remember it, it is what makes the filter exact.
    if "self" in data and "updates" not in data:
        if state is not None:
            state["self"] = data["self"]
            print(f"[archive] own-ship context = {data['self']} (AIS contexts will be skipped)",
                  flush=True)
        return []
    if state is not None and _is_other_vessel(data.get("context"), state.get("self")):
        state["skipped"] = state.get("skipped", 0) + 1
        return []
    rows = []
    for upd in data.get("updates", []):
        source = upd.get("$source") or (upd.get("source") or {}).get("label") or "unknown"
        # Prefer the source's own timestamp so the archive reflects when data was measured,
        # not when we received it; fall back to receive-time.
        ts = upd.get("timestamp") or default_time
        for v in upd.get("values", []):
            path = v.get("path")
            if path:
                flatten(ts, source, path, v.get("value"), rows)
    return rows


def write_rows(conn, rows):
    """Insert one batch, all or nothing. A failed `executemany` leaves the rows before the
    failing one inside an open transaction; without the rollback the requeued batch is written
    again on top of them on the next commit (reproduced: an I/O error on row 2 of 2 left
    [1, 1, 2])."""
    with _WRITE_LOCK:
        try:
            conn.executemany(
                "INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass                    # a dead connection has nothing to roll back
            raise


# ---- DISK GUARD + RAM CAP -----------------------------------------------------------------
# The archive shares the SD card with Signal K, Docker and the uplink's queue. At the dock it
# grows ~7 GB/day (2026-08-30 → 09-02: 24 GB in 3.4 days), and the retention prune frees
# nothing while a session is open. A full card takes the whole boat stack down with it, so
# above DISK_HIGH_PCT the archiver stops writing full-res (the uplink's aggregates still go
# ashore) and says so, and it resumes only below DISK_LOW_PCT so it does not flap at the line.
#
# "Used" counts the archive's own free pages as free: SQLite reuses deleted pages but never
# gives them back to the filesystem, so after a prune `df` does not move — without this the
# guard, once tripped, could never resume.
DISK_HIGH_PCT = float(os.environ.get("ARCHIVE_DISK_HIGH_PCT", "85"))
DISK_LOW_PCT = float(os.environ.get("ARCHIVE_DISK_LOW_PCT", "80"))
# The HARD floor is on the filesystem's real free space, with no free-page credit: the WAL and
# every new page need actual bytes, and a reader holding a snapshot can stop SQLite reusing its
# own free pages. Either limit trips it, and the free-page credit can never override it.
DISK_HARD_PCT = float(os.environ.get("ARCHIVE_DISK_HARD_PCT", "95"))
DISK_MIN_FREE_MB = float(os.environ.get("ARCHIVE_DISK_MIN_FREE_MB", "2048"))
DISK_CHECK_S = float(os.environ.get("ARCHIVE_DISK_CHECK_S", "30"))
# While stopped, repeat the loud line this often (a once-only warning scrolls away).
DISK_NAG_S = float(os.environ.get("ARCHIVE_DISK_NAG_S", "600"))
# The in-RAM buffer is where rows wait while writes fail. Uncapped, a disk that keeps failing
# eats the Pi's ~3 GB free RAM in hours and the OOM killer takes Signal K with it. ~300k rows
# is ~10 minutes at the dock rate and ~150 MB.
MAX_BUFFER_ROWS = int(os.environ.get("ARCHIVE_MAX_BUFFER_ROWS", "300000"))
STATUS_FILE = Path(os.environ.get("ARCHIVE_STATUS_FILE",
                                  str(ARCHIVE_DB.with_name("archiver-status.json"))))


def disk_usage(path, conn=None, statvfs=os.statvfs):
    """(credited_pct, raw_pct, free_bytes) for the archive's filesystem. credited_pct is None
    when there is no usable connection to read the free pages from — UNKNOWN, not "full".

    raw_pct and free_bytes are what the filesystem really has. credited_pct also counts the
    archive's SQLite free pages as free — right for "is there room to keep recording once the
    prune has run", wrong for "is the card about to fill", which is why both are returned."""
    st = statvfs(str(path))
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return 0.0, 0.0, free
    used = total - free                          # reserved blocks count as used: conservative
    raw = 100.0 * used / total
    if conn is None:
        return None, raw, free
    try:
        with _WRITE_LOCK:
            free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    except Exception:
        return None, raw, free                   # e.g. a closed or broken connection
    return max(0.0, 100.0 * (used - free_pages * page_size) / total), raw, free


def disk_used_pct(path, conn=None, statvfs=os.statvfs):
    """Percent of the archive's filesystem in use, counting SQLite free pages as free (the raw
    figure when they can't be read)."""
    credited, raw, _free = disk_usage(path, conn, statvfs)
    return raw if credited is None else credited


class DiskGuard:
    """Hysteresis switch on disk use: stop full-res at `high`, resume below `low`.

    Two kinds of limit. `high`/`low` use the free-page-credited figure, so a prune that frees
    pages inside the file lets recording resume. The hard floor (`hard_pct` of the filesystem
    really used, or less than `min_free_mb` really free) ignores that credit: it stops recording
    on its own, and recording cannot resume until the real numbers are clear of it by a margin
    (2 points, and twice the minimum free) — so it does not flap at the line either.

    When the free pages can't be read (no connection, or a broken one mid-recovery) the
    credited figure is UNKNOWN: the high/low marks make no decision that check, and only the
    hard floor can stop recording. Unknown must never read as "full, discard".

    `allow()` is called before every flush but measures at most every `every_s` (a statvfs and
    two pragmas — microseconds). If the measurement itself fails it keeps recording: an unknown
    disk is not a reason to stop the boat's only full-res record."""

    def __init__(self, path, high=None, low=None, every_s=None, nag_s=None,
                 statvfs=os.statvfs, clock=time.monotonic, hard_pct=None, min_free_mb=None):
        self.path = path
        self.high = DISK_HIGH_PCT if high is None else float(high)
        self.low = DISK_LOW_PCT if low is None else float(low)
        if self.low >= self.high:
            self.low = self.high - 5.0
        self.every_s = DISK_CHECK_S if every_s is None else float(every_s)
        self.nag_s = DISK_NAG_S if nag_s is None else float(nag_s)
        self.hard_pct = DISK_HARD_PCT if hard_pct is None else float(hard_pct)
        self.min_free = (DISK_MIN_FREE_MB if min_free_mb is None else float(min_free_mb)) * 2**20
        self.statvfs = statvfs
        self.clock = clock
        self.paused = False
        self.pct = None             # credited
        self.raw_pct = None         # what the filesystem really has in use
        self.free_bytes = None
        self.reason = None
        self.dropped = 0            # rows not written because the disk was too full
        self.paused_since = None
        self._last_check = None
        self._last_nag = None

    def allow(self, conn=None):
        now = self.clock()
        if self._last_check is None or now - self._last_check >= self.every_s:
            self._last_check = now
            try:
                self.pct, self.raw_pct, self.free_bytes = disk_usage(self.path, conn, self.statvfs)
            except OSError as exc:
                print(f"[archive] disk guard: cannot read disk use ({exc}) — recording anyway",
                      flush=True)
                return not self.paused
            hard_hit = self.raw_pct >= self.hard_pct or self.free_bytes < self.min_free
            hard_clear = (self.raw_pct < self.hard_pct - 2
                          and self.free_bytes >= 2 * self.min_free)
            known = self.pct is not None
            if not self.paused and ((known and self.pct >= self.high) or hard_hit):
                self.paused = True
                self.reason = "hard_floor" if hard_hit else "high_mark"
                self.paused_since = datetime.now(timezone.utc).isoformat()
                self._last_nag = now
                print(f"[archive] !!! DISK {self._describe()} — FULL-RES RECORDING STOPPED to "
                      f"protect Signal K and the uplink. Resumes below {self.low:.0f}% (and "
                      f"real use < {self.hard_pct - 2:.0f}%, free >= "
                      f"{2 * self.min_free / 2**30:.1f} GB). Free space on the card or close "
                      f"the open race session so the prune can work.", flush=True)
            elif self.paused and known and self.pct < self.low and hard_clear:
                self.paused = False
                self.reason = None
                print(f"[archive] disk {self._describe()} — full-res recording "
                      f"RESUMED; {self.dropped} rows were not recorded while stopped "
                      f"(since {self.paused_since})", flush=True)
                self.paused_since = None
        if self.paused and self._last_nag is not None and now - self._last_nag >= self.nag_s:
            self._last_nag = now
            print(f"[archive] !!! full-res recording STILL STOPPED: disk {self._describe()}; "
                  f"{self.dropped} rows not recorded since {self.paused_since}", flush=True)
        return not self.paused

    def _describe(self):
        credited = "unknown" if self.pct is None else f"{self.pct:.1f}%"
        return (f"{credited} counting archive free pages (limit {self.high:.0f}%), "
                f"{self.raw_pct:.1f}% really used (hard limit {self.hard_pct:.0f}%), "
                f"{self.free_bytes / 2**30:.1f} GB free (minimum {self.min_free / 2**30:.1f} GB)")


def cap_buffer(buf, state, limit=None):
    """Drop the OLDEST rows past `limit`, counting them in state['buffer_dropped'] and saying so.
    Oldest, because the newest are what the crew and the engine are looking at."""
    limit = MAX_BUFFER_ROWS if limit is None else int(limit)
    over = len(buf) - limit
    if limit <= 0 or over <= 0:
        return 0
    del buf[:over]
    state["buffer_dropped"] = state.get("buffer_dropped", 0) + over
    print(f"[archive] !!! write buffer over {limit} rows — DROPPED the oldest {over} "
          f"({state['buffer_dropped']} dropped since start). Writes are failing; see above.",
          flush=True)
    return over


def write_status(state, guard=None, path=None):
    """The archiver's health as a small JSON file next to the archive, for the engine/console
    (which mount the archive volume read-only) and for a human with `cat`. Best effort."""
    path = Path(path or STATUS_FILE)
    doc = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "recording": not (guard and guard.paused) and not state.get("rotate_pending"),
        "reason": ("disk_full" if guard and guard.paused
                   else "rotation_pending" if state.get("rotate_pending") else None),
        "disk_used_pct": None if not guard or guard.pct is None else round(guard.pct, 1),
        "disk_really_used_pct": None if not guard or guard.raw_pct is None
        else round(guard.raw_pct, 1),
        "disk_free_bytes": guard.free_bytes if guard else None,
        "disk_hard_pct": guard.hard_pct if guard else None,
        "stop_cause": guard.reason if guard and guard.paused else None,
        "disk_high_pct": guard.high if guard else None,
        "disk_low_pct": guard.low if guard else None,
        "paused_since": guard.paused_since if guard else None,
        "rows_not_recorded_disk_full": guard.dropped if guard else 0,
        "rows_dropped_buffer_full": state.get("buffer_dropped", 0),
        "rows_written": state.get("total", 0),
        "corrupt_rotations_this_run": state.get("rotations", 0),
    }
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        os.replace(tmp, path)
    except Exception as exc:
        if not state.get("_status_warned"):
            state["_status_warned"] = True
            print(f"[archive] could not write {path} ({exc})", flush=True)
    return doc


async def flusher(conn, buf, loop, state=None, db=None, wake=None, guard=None):
    """Drain the buffer to disk every FLUSH_SECONDS, for as long as this process lives.

    Wrapped end to end because an unhandled exception here does not crash the service — it
    kills this ONE task, asyncio swallows the traceback, and the archiver goes on looking
    perfectly healthy while recording nothing. That is a worse failure than the crash-loop it
    replaces, so the recorder must be un-killable.

    Failure handling, in order:
      - disk over the guard's limit -> the batch is NOT written (counted, logged, status file);
      - a write raised corruption   -> rotate on the next attempt, unconditionally (no
        quick_check re-gate), and the batch is requeued, never dropped;
      - anything else               -> requeued, retried next tick;
      - the requeue buffer is capped at MAX_BUFFER_ROWS, dropping the oldest, loudly."""
    state = state if state is not None else {}
    state.setdefault("conn", conn)
    state.setdefault("total", 0)
    path = db or ARCHIVE_DB
    while True:
        # Wake on the timer OR as soon as the reader says the buffer is full (FLUSH_ROWS).
        # The reader signals rather than writes, so there is exactly ONE writer and therefore
        # exactly one place that handles corruption.
        if wake is None:
            await asyncio.sleep(FLUSH_SECONDS)
        else:
            try:
                await asyncio.wait_for(wake.wait(), timeout=FLUSH_SECONDS)
            except asyncio.TimeoutError:
                pass
            wake.clear()
        try:
            await _flush_once(buf, loop, state, path, guard)
        except Exception as exc:          # belt and braces: the loop itself must never die
            print(f"[archive] flusher error ({type(exc).__name__}: {exc}); continuing",
                  flush=True)
        if guard is not None and (guard._last_check != state.get("_status_at")
                                  or state.pop("_status_dirty", False)):
            state["_status_at"] = guard._last_check
            write_status(state, guard)


async def _recover(buf, loop, state, path):
    """Finish a pending corruption rotation. True when the archive is usable again. On failure
    nothing is lost: rows stay in `buf` (capped) and the next flush tries again."""
    old, state["conn"] = state.get("conn"), None   # closed by reopen_fresh, whatever happens
    try:
        state["conn"], _rot = await loop.run_in_executor(None, reopen_fresh, old, path)
    except Exception as exc:
        print(f"[archive] rotation failed ({type(exc).__name__}: {exc}); "
              f"{len(buf)} rows waiting, will retry", flush=True)
        cap_buffer(buf, state)
        return False
    state["rotate_pending"] = False
    state["rotations"] = state.get("rotations", 0) + 1
    state["_status_dirty"] = True
    return True


async def _flush_once(buf, loop, state, path, guard):
    # A pending rotation goes FIRST, before the disk guard: the guard reads free pages through
    # the connection, and deciding "full" from a half-recovered archive would throw away the
    # very rows the rotation is meant to save.
    if state.get("rotate_pending") and not await _recover(buf, loop, state, path):
        return
    if guard is not None:
        allowed = await loop.run_in_executor(None, guard.allow, state.get("conn"))
        if not allowed:
            if buf:
                guard.dropped += len(buf)
                buf[:] = []
            return
    if not buf:
        return
    rows, buf[:] = buf[:], []
    # Two attempts: a corruption error on the first rotates and writes the same batch into the
    # fresh file straight away. If the rotation or the second write fails too, the rows go back.
    for attempt in (1, 2):
        if state.get("rotate_pending"):
            buf[:0] = rows                # _recover keeps them on failure, in order
            if not await _recover(buf, loop, state, path):
                return
            rows, buf[:] = buf[:], []
        try:
            await loop.run_in_executor(None, write_rows, state["conn"], rows)
            break
        except sqlite3.DatabaseError as exc:
            if _is_corruption(exc):
                # Once a write has said "corrupt", the file is rotated — no quick_check re-gate.
                state["rotate_pending"] = True
                state["_status_dirty"] = True
                print(f"[archive] CORRUPTION during write ({exc}) — rotating the file",
                      flush=True)
                if attempt == 1:
                    continue
            _requeue(buf, rows, state, f"write failed ({exc})")
            return
        except Exception as exc:
            _requeue(buf, rows, state, f"unexpected flush error ({type(exc).__name__}: {exc})")
            return
    state["total"] = state.get("total", 0) + len(rows)
    # report the AIS skip count too — silence here would look identical to a filter that
    # had quietly stopped working, or to one wrongly eating own-ship data
    skipped = state.get("skipped", 0)
    note = f" (skipped {skipped} AIS deltas)" if skipped else ""
    print(f"[archive] +{len(rows)} rows (total {state['total']}){note}", flush=True)


def _requeue(buf, rows, state, why):
    """Put a failed batch back at the FRONT (order kept; write_rows rolled it back, so nothing
    is half-written), then enforce the RAM cap."""
    buf[:0] = rows
    print(f"[archive] {why}; {len(rows)} rows requeued ({len(buf)} waiting)", flush=True)
    cap_buffer(buf, state)


# ---- RETENTION PRUNE (race sessions) ------------------------------------------------------
# The archive records EVERYTHING (collect-everything), but only RACE-SESSION windows — the
# owner's record switch, started/ended from the iPad console (engine `sessions` table) — are
# kept long-term. Outside any session, readings older than ARCHIVE_RETAIN_DAYS are deleted, so
# a day sail or a delivery never accumulates on the SD card (deleted pages are reused; the file
# stops growing). SAFETY: if the engine DB / sessions table can't be read, NOTHING is pruned —
# we must never delete a race because a volume didn't mount. 0 disables (the bench keeps its
# replayed sample data).
# 7 days on the boat (compose.pi.yml); the default matches so a bare run behaves the same.
RETAIN_DAYS = float(os.environ.get("ARCHIVE_RETAIN_DAYS", "7"))
ENGINE_DB = os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db")
PRUNE_EVERY_S = float(os.environ.get("ARCHIVE_PRUNE_EVERY_S", "3600"))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


MOTION_PATHS = ("navigation.speedOverGround", "navigation.courseOverGroundTrue")
# How far either side of a marker to read motion when deriving. The whole point is that the
# marker may have stopped early, so look a long way past it — but bound it so one session cannot
# scan a season. Same numbers the cloud agent uses.
DERIVE_BEHIND_S = 6 * 3600
DERIVE_AHEAD_S = 24 * 3600
# Motion is read in buckets; the keep-window is widened by one on each side (see _derived_window).
DERIVE_BUCKET_S = 300
_DERIVED_CACHE = {}         # (session id, start, end) -> window, for sessions that can't change


def motion_series(conn, t0, t1, bucket_s=300):
    """[(epoch_s, sog_kn, cog_deg)] from the archive, one sample per `bucket_s`, from a SINGLE
    source — the boat's own record, read the same way the cloud agent reads it. A series that
    alternates between the two GPSs is a series of manufactured turns, and `find_turnaround`
    would read them as the end of a race.

    The archiver never stores AIS (own-ship contexts only), so unlike the cloud there is nothing
    to exclude here — but the source still has to be CHOSEN, not merged."""
    import math
    # SQLite's built-in trig is a compile-time option; these always exist.
    conn.create_function("m_sin", 1, math.sin)
    conn.create_function("m_cos", 1, math.cos)
    rows = conn.execute(
        "SELECT source, CAST(strftime('%s', time) AS INTEGER) / ? * ? AS b, "
        "  avg(CASE WHEN path = ? THEN value END) AS sog, "
        "  avg(CASE WHEN path = ? THEN m_sin(value) END) AS cs, "
        "  avg(CASE WHEN path = ? THEN m_cos(value) END) AS cc "
        "FROM readings WHERE boat_id = ? AND path IN (?, ?) "
        "AND time >= ? AND time <= ? AND value IS NOT NULL GROUP BY 1, 2 ORDER BY 2",
        (int(bucket_s), int(bucket_s), MOTION_PATHS[0], MOTION_PATHS[1], MOTION_PATHS[1],
         BOAT_ID, MOTION_PATHS[0], MOTION_PATHS[1], _iso(t0), _iso(t1))).fetchall()
    by_source = {}
    for src, b, sog, cs, cc in rows:
        by_source.setdefault(src, []).append((float(b), sog, cs, cc))
    best, _resolved = race_window.choose_motion_source(by_source, n2k_sources.devices_for(BOAT_ID))
    if best is None:
        return []
    return [(b, None if sog is None else float(sog) * 1.943844,
             None if cs is None or cc is None else math.degrees(math.atan2(float(cs), float(cc))))
            for b, sog, cs, cc in by_source[best]]


def _derived_window(conn, sessions, s, now):
    """The window `shared/race_window` derives for one marker, as (start_iso, end_iso)."""
    if s["end_ts"] is None:
        return None                     # an open session already reaches into the future
    key = (s["id"], s["start_ts"], s["end_ts"])
    settled = now - s["end_ts"] > 3600  # a race that ended an hour ago cannot grow new telemetry
    if settled and key in _DERIVED_CACHE:
        return _DERIVED_CACHE[key]
    motion = motion_series(conn, s["start_ts"] - DERIVE_BEHIND_S, s["end_ts"] + DERIVE_AHEAD_S,
                           bucket_s=DERIVE_BUCKET_S)
    w = race_window.derive(sessions, motion, anchor_ts=s["start_ts"])
    if not w:
        return None
    # ⚠ A bucketed sample is labelled with the START of its bucket, so the derived end lands up
    # to one bucket BEFORE the last reading it was derived from — and here that difference is
    # deleted, not merely hidden (measured: 4 rows of a five-hour sail, every prune, forever).
    # Widen the keep-window by a bucket on each side. Quantising a continuous quantity
    # reintroduces discontinuities; the honest fix is to keep the quantisation error, not to
    # pretend the edge is exact.
    out = (_iso(w["start_ts"] - DERIVE_BUCKET_S), _iso(w["end_ts"] + DERIVE_BUCKET_S))
    if w["hours"] > w["marker_hours"] + 0.02:
        print(f"[archive] session {s['id']}: the record supports {w['hours']:.2f} h, the button "
              f"recorded {w['marker_hours']:.2f} h — keeping the derived window too "
              f"({'; '.join(w['provenance']) or 'no reason given'})", flush=True)
    if settled:
        _DERIVED_CACHE[key] = out
    return out


def session_windows(engine_db=None, conn=None):
    """Everything the prune must KEEP, as [(start_iso, end_iso)]; None = unreadable (DON'T prune).

    The markers are taps on the iPad's ⏺ LOG button, and Jul 18 2026 is what trusting them costs:
    the stop was caught during a kite hoist and written 4.2 s before the sail bar registered the
    hoist, claiming 1 h 49 m of a 7 h race. Shore-side that is a short debrief. HERE it is
    different in kind — everything after 18:52 is out-of-session, so the retention prune puts the
    rest of the race on the DELETION path, on the boat, against "lose no telemetry".

    So when the archive is available (`conn`), every closed marker is also widened by
    `shared/race_window`, derived from the boat's own record, and BOTH windows are returned. The
    kept set can only grow: a derivation that is wrong, that raises, or that is missing entirely
    can never delete something the old behaviour would have kept."""
    try:
        ec = sqlite3.connect(f"file:{engine_db or ENGINE_DB}?mode=ro", uri=True, timeout=10)
        rows = ec.execute("SELECT id, race_id, start_ts, end_ts FROM sessions").fetchall()
        ec.close()
    except Exception:
        return None
    now = datetime.now(timezone.utc).timestamp()
    sessions = [{"id": i, "race_id": rid, "start_ts": float(a),
                 "end_ts": None if b is None else float(b)} for i, rid, a, b in rows]
    wins = [(_iso(s["start_ts"]),
             _iso(s["end_ts"] if s["end_ts"] is not None else now + 86400)) for s in sessions]
    if conn is None or not sessions:
        return wins
    if race_window is None:
        # Never silently: a prune keeping only the button's windows is exactly the failure this
        # code exists to prevent, and it must not look like a working one.
        print("[archive] prune: shared/race_window is not in this image — keeping ONLY the "
              "button's windows; rebuild the archiver", flush=True)
        return wins
    for s in sessions:
        try:
            w = _derived_window(conn, sessions, s, now)
        except Exception as exc:
            print(f"[archive] session {s['id']}: window derivation failed "
                  f"({type(exc).__name__}: {exc}) — keeping the marker window", flush=True)
            continue
        if w:
            wins.append(w)
    return wins


def prune(conn, engine_db=None, retain_days=None):
    """Delete out-of-session readings older than the retention window. Returns rows deleted,
    or None when pruning was skipped (disabled / engine DB unreadable)."""
    days = RETAIN_DAYS if retain_days is None else float(retain_days)
    if days <= 0:
        return None
    wins = session_windows(engine_db, conn=conn)
    if wins is None:
        print("[archive] prune SKIPPED — engine sessions table unreadable (never delete blind)",
              flush=True)
        return None
    cutoff = _iso(datetime.now(timezone.utc).timestamp() - days * 86400)
    cond = "time < ?"
    args = [cutoff]
    for a, b in wins:
        cond += " AND NOT (time >= ? AND time <= ?)"
        args += [a, b]
    with _WRITE_LOCK:
        cur = conn.execute(f"DELETE FROM readings WHERE {cond}", args)
        conn.commit()
    if cur.rowcount:
        print(f"[archive] pruned {cur.rowcount} out-of-session rows older than {cutoff} "
              f"({len(wins)} session window(s) kept)", flush=True)
    return cur.rowcount


async def pruner(state, loop):
    while True:
        try:
            if state.get("rotate_pending") or state.get("conn") is None:
                # never prune a file that just reported corruption, nor through a missing handle
                print("[archive] prune skipped this hour — archive recovery pending", flush=True)
            else:
                await loop.run_in_executor(None, prune, state["conn"])
        except Exception as exc:      # never let housekeeping kill the recorder
            print(f"[archive] prune error: {exc}", flush=True)
            if _is_corruption(exc):
                # the file is bad even if the last write was not: the flusher rotates it next
                state["rotate_pending"] = True
                state["_status_dirty"] = True
        await asyncio.sleep(PRUNE_EVERY_S)


async def run():
    # `state` carries the `self` context from the hello frame + the AIS skip count (surviving
    # reconnects, so a dropped socket does not briefly re-admit AIS before the next hello) and
    # now also the live connection, because a corruption rotation replaces it underneath every
    # writer. One holder, so nobody keeps writing to a file that has been moved aside.
    state = {}
    conn, n, rotated = open_for_run()
    state["conn"] = conn
    if rotated:
        state["rotations"] = 1
    print(f"[archive] {ARCHIVE_DB} ready ({n} rows) <- {SIGNALK_WS} (full resolution)"
          + (f" [recovered from {rotated.name}]" if rotated else ""), flush=True)
    guard = DiskGuard(ARCHIVE_DB.parent)
    print(f"[archive] disk guard: stop full-res at {guard.high:.0f}% disk use, resume below "
          f"{guard.low:.0f}%; RAM buffer cap {MAX_BUFFER_ROWS} rows; status -> {STATUS_FILE}",
          flush=True)
    buf = []
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    asyncio.create_task(flusher(conn, buf, loop, state, wake=wake, guard=guard))
    asyncio.create_task(pruner(state, loop))
    while True:
        try:
            async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[archive] connected to Signal K", flush=True)
                async for msg in ws:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    rows = parse_delta(msg, now, state)
                    if rows:
                        buf.extend(rows)
                        if len(buf) > MAX_BUFFER_ROWS:
                            cap_buffer(buf, state)     # the flusher is stuck; keep RAM bounded
                        if len(buf) >= FLUSH_ROWS:
                            # Signal, don't write: the flusher owns the corruption/retry path,
                            # and a second writer with its own error handling is how you end up
                            # with half-recovered state.
                            wake.set()
        except Exception as exc:
            print(f"[archive] Signal K WS error ({exc}); retrying in 3s", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
