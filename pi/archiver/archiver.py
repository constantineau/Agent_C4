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
    try:
        conn.execute("PRAGMA journal_mode=WAL")     # readers (backfill) never block the writer
        conn.execute("PRAGMA synchronous=FULL")     # durable across power loss — boat-grade
        conn.executescript(SCHEMA)
        conn.commit()
    except BaseException:
        # Close BEFORE the caller rotates the file: a leaked WAL connection closed later (by the
        # garbage collector) cleans up sidecars by NAME, and by then the names are the fresh file's.
        try:
            conn.close()
        except Exception:
            pass
        raise
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
CORRUPT_MARKERS = ("malformed", "not a database", "disk image", "string or blob too big",
                   "file is encrypted", "unsupported file format", "database corrupt",
                   "could not decode")
# Errors that say nothing about the FILE. A good archive must never be moved aside for these:
# a lock clears, and a full disk, a missing directory or a read-only mount would fail the fresh
# file exactly the same way (rotating would only litter the card with empty "corrupt" files).
TRANSIENT_MARKERS = ("locked", "busy", "disk is full", "unable to open", "readonly",
                     "read-only", "out of memory", "interrupted")


def _is_corruption(exc: Exception) -> bool:
    """MID-RUN: True for the errors that mean the FILE is bad, not that the statement was.

    Narrow on purpose — a write that fails on a schema bug must not rotate a good archive. But a
    damaged page does not always say "malformed": a garbled record header comes back as
    `DataError: string or blob too big`, and garbled text as a UnicodeDecodeError (Python decoding
    SQLite's own error message). Both are the file."""
    if isinstance(exc, (UnicodeDecodeError, sqlite3.DataError)):
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(m in str(exc).lower() for m in CORRUPT_MARKERS)


def _is_bad_file(exc: Exception) -> bool:
    """AT OPEN (open, schema read, the bounded startup probes): every database error is the
    file, except the transient ones above.

    Wider than `_is_corruption` because at open no statement of ours can be wrong — the SQL is
    fixed — so an error here is the file. Re-raising instead is the crash loop: exit, Docker
    restarts, same error, forever (Jul 18: 48 times; test_brownout found two more shapes of it,
    `string or blob too big` and a UnicodeDecodeError from a damaged schema page)."""
    if isinstance(exc, UnicodeDecodeError):
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    return not any(m in msg for m in TRANSIENT_MARKERS)


def integrity_ok(conn) -> bool:
    """`PRAGMA quick_check` over the WHOLE file. Not for the startup path: on the boat's SD card
    it reads ~10 MB/s, so a 24 GB archive took 39 min (2026-10-28). Only the low-priority
    background check (`BackgroundCheck`) calls this kind of scan."""
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except (sqlite3.DatabaseError, UnicodeDecodeError):
        return False


# Bounded startup probes: each reads O(tree depth) pages, whatever the size of the file. They
# cover what a brownout most likely damaged — the header, the schema page, and the right-hand
# edge of every b-tree, where the last writes went. A damaged page in the MIDDLE of the file is
# left to the background check; the first insert also walks the index leaves it lands on, and a
# write that hits damage rotates (see `_flush_once`).
FAST_PROBES = (
    "SELECT name, rootpage FROM sqlite_master",
    "SELECT max(id) FROM readings",
    "SELECT * FROM readings ORDER BY id DESC LIMIT 64",
    "SELECT time FROM readings INDEXED BY readings_time_idx ORDER BY time DESC LIMIT 64",
    "SELECT path, time FROM readings INDEXED BY readings_path_time_idx "
    "ORDER BY path DESC, time DESC LIMIT 64",
    "SELECT k, v FROM sync_state LIMIT 64",
)


def fast_check(conn) -> int:
    """The startup check: the bounded probes above. Returns the highest row id (0 when empty),
    which the startup log prints instead of a `count(*)` — a count reads every page of the table.
    Raises whatever the file raises; the caller classifies it."""
    top = 0
    for sql in FAST_PROBES:
        rows = conn.execute(sql).fetchall()
        if sql == "SELECT max(id) FROM readings":
            top = rows[0][0] or 0
    return int(top)


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


# An open error that is neither clearly the file (malformed, not a database, string or blob
# too big, a decode error) nor clearly transient — typically "disk I/O error", which a brownout
# can produce once on a healthy card — is retried before the archive is moved aside. Moving a
# healthy archive loses nothing, but it takes the engine's history with it.
OPEN_RETRIES = int(os.environ.get("ARCHIVE_OPEN_RETRIES", "3"))
OPEN_RETRY_S = float(os.environ.get("ARCHIVE_OPEN_RETRY_S", "2"))


def _clearly_damaged(exc) -> bool:
    """The errors that can only mean a damaged file: rotate at once, no retry."""
    return _is_corruption(exc)


def open_db_resilient(path: Path = ARCHIVE_DB):
    """Open the archive, rotating it aside if it is unusable. Returns (conn, rotated_to|None).

    This is the whole brownout story: a bad card must cost seconds, not six weeks. BOUNDED:
    open + schema + `fast_check`, never a whole-file scan — the archiver records nothing until
    this returns (see `BackgroundCheck` for the full check, which runs after recording starts).
    Clearly damaged -> rotate at once; transient -> raise (the caller retries); anything else
    -> retry the open OPEN_RETRIES times, OPEN_RETRY_S apart, and rotate only if it persists."""
    finished = _clear_for_fresh(path)
    attempt = 0
    while True:
        conn = None
        try:
            conn = open_db(path)
            fast_check(conn)
            if finished is not None:
                _note_rotation(conn, finished)
                return conn, finished
            return conn, None
        except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            if not _is_bad_file(exc):
                if conn is not None:
                    conn.close()
                raise
            if not _clearly_damaged(exc) and attempt < OPEN_RETRIES:
                attempt += 1
                if conn is not None:
                    conn.close()
                print(f"[archive] opening {path.name} failed ({type(exc).__name__}: {exc}); "
                      f"retry {attempt}/{OPEN_RETRIES} in {OPEN_RETRY_S:.0f}s before moving "
                      f"it aside", flush=True)
                time.sleep(OPEN_RETRY_S)
                continue
            print(f"[archive] {path.name} is unusable ({type(exc).__name__}: {exc}) — moving "
                  f"it aside and recording into a fresh file", flush=True)
            # the same close -> resumable move -> check -> open sequence as a mid-run
            # rotation; CREATE TABLE IF NOT EXISTS self-initialises the fresh file
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
    # A background check reading this file holds its own connection to it, and a WAL
    # connection's close cleans sidecars by NAME — stop it before anything moves. If it will
    # not let go (a read stalled on a dying card), move NOTHING: raise before touching `conn`,
    # so the caller keeps recording into the current file and retries the rotation later.
    if not stop_checks(path):
        raise CheckerBusy(f"a background check still holds {path.name}; rotation deferred")
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
    """Startup open, BOUNDED: `open_db_resilient`, whose `fast_check` also yields the highest row
    id for the startup log. No `count(*)` (it read the whole table — the line the boat
    crash-looped on 4,000+ times after 2026-09-02, and 40+ min of no recording on a big file)
    and no quick_check (the background check does that once recording runs). Returns
    (conn, highest_row_id, rotated)."""
    conn, rotated = open_db_resilient(path)
    try:
        top = fast_check(conn)
    except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
        if not _is_bad_file(exc):
            raise
        print(f"[archive] {path.name} passed the open but its probes failed ({exc}) — "
              f"rotating it anyway", flush=True)
        conn, rotated = reopen_fresh(conn, path)
        top = fast_check(conn)
    return conn, top, rotated


# ---- BACKGROUND CHECK ---------------------------------------------------------------------
# The whole-file `PRAGMA quick_check` the startup path used to run, moved off it. It runs on its
# own read-only connection in a thread, AFTER recording has started, at low priority: nice 19,
# the idle I/O class where the kernel offers it, and a duty cycle (`ARCHIVE_CHECK_DUTY`, the
# share of wall time it may read) enforced by SQLite's progress handler — which is also how it
# is stopped mid-scan. On the live file a failed check rotates it (as the startup check used
# to); on a file already rotated aside it only reports, so a human knows whether that file is
# really damaged or was moved on a false alarm.
CHECK_DUTY = float(os.environ.get("ARCHIVE_CHECK_DUTY", "0.5"))
CHECK_SLICE_S = 0.05
# How long a rotation waits for a check to let go of the file before deferring itself.
CHECK_STOP_TIMEOUT_S = float(os.environ.get("ARCHIVE_CHECK_STOP_TIMEOUT_S", "30"))


class CheckerBusy(OSError):
    """A rotation was refused because a background check still has the file open."""
_CHECKS = {}                    # resolved path -> the BackgroundCheck reading it
_CHECKS_LOCK = threading.Lock()


def _lower_priority():
    """This THREAD only (Linux priorities are per-thread): CPU nice 19 and, on the two
    architectures whose syscall number is known here, the idle I/O class. Best effort."""
    try:
        tid = threading.get_native_id()
        os.setpriority(os.PRIO_PROCESS, tid, 19)
    except (AttributeError, OSError):
        return
    try:
        import ctypes
        import platform
        nr = {"x86_64": 251, "aarch64": 30}.get(platform.machine())
        if nr is not None:
            # ioprio_set(IOPRIO_WHO_PROCESS=1, tid, IOPRIO_CLASS_IDLE=3 << 13)
            ctypes.CDLL(None, use_errno=True).syscall(nr, 1, tid, 3 << 13)
    except Exception:
        pass


class BackgroundCheck:
    """`PRAGMA quick_check` of `path` in a low-priority thread. `result` is None while running,
    then a dict {status: ok|corrupt|aborted|error, detail}. `on_corrupt()` is called (from the
    thread) when the check finds damage — after its connection is closed."""

    def __init__(self, path, live, on_corrupt=None, duty=None):
        self.path = Path(path)
        self.live = live
        self.on_corrupt = on_corrupt
        self.duty = min(1.0, max(0.01, CHECK_DUTY if duty is None else float(duty)))
        self.stop_event = threading.Event()
        self.result = None
        self.conn_open = False          # True while the check holds a connection to the file
        self.slept = 0.0                # seconds given back by the duty cycle
        self.started = datetime.now(timezone.utc).isoformat()
        self.finished = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"check-{self.path.name}")

    def start(self):
        with _CHECKS_LOCK:
            _CHECKS[str(self.path.resolve())] = self
        self.thread.start()
        return self

    def stop(self, timeout=None):
        """Ask the check to stop and wait up to `timeout`. True once it no longer holds a
        connection to the file — the only state in which the file may be moved."""
        self.stop_event.set()
        timeout = CHECK_STOP_TIMEOUT_S if timeout is None else timeout
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout)
        return not self.conn_open

    def summary(self):
        doc = {"file": self.path.name, "live": self.live, "started": self.started,
               "finished": self.finished}
        doc.update(self.result or {"status": "running"})
        return doc

    def _throttle(self):
        """SQLite's progress handler: nonzero aborts the statement (stop requested); otherwise
        sleep so the check reads at most `duty` of the time."""
        if self.stop_event.is_set():
            return 1
        now = time.monotonic()
        if now - self._slice_start >= CHECK_SLICE_S:
            if self.duty < 1.0:
                pause = (now - self._slice_start) * (1.0 - self.duty) / self.duty
                self.slept += pause
                if self.stop_event.wait(pause):
                    return 1
            self._slice_start = time.monotonic()
        return 0

    def _run(self):
        _lower_priority()
        result, conn = None, None
        try:
            self.conn_open = True       # before connect: a stalled open holds the file too
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30,
                                   check_same_thread=False)
            self._slice_start = time.monotonic()
            conn.set_progress_handler(self._throttle, 1000)
            rows = [r[0] for r in conn.execute("PRAGMA quick_check(10)").fetchall()]
            if rows == ["ok"]:
                result = {"status": "ok", "detail": None}
            else:
                result = {"status": "corrupt", "detail": "; ".join(map(str, rows))[:500]}
        except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            if self.stop_event.is_set():
                result = {"status": "aborted", "detail": "stopped"}
            elif _is_bad_file(exc):
                result = {"status": "corrupt", "detail": f"{type(exc).__name__}: {exc}"[:500]}
            else:
                result = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:500]}
        except Exception as exc:            # never let the check take anything else down
            result = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:500]}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.conn_open = False
            with _CHECKS_LOCK:
                if _CHECKS.get(str(self.path.resolve())) is self:
                    del _CHECKS[str(self.path.resolve())]
        self.finished = datetime.now(timezone.utc).isoformat()
        self.result = result
        what = "the live archive" if self.live else "the file moved aside"
        if result["status"] == "ok":
            print(f"[archive] background check of {self.path.name} ({what}): ok"
                  + ("" if self.live else " — it was moved on a false alarm; its data is "
                     "intact, merge it back by hand if needed"), flush=True)
        elif result["status"] == "corrupt":
            print(f"[archive] background check of {self.path.name} ({what}): DAMAGED "
                  f"({result['detail']})"
                  + (" — rotating it aside" if self.live else " — keep it for salvage.py"),
                  flush=True)
            if self.live and self.on_corrupt is not None:
                try:
                    self.on_corrupt()
                except Exception as exc:
                    print(f"[archive] could not schedule the rotation: {exc}", flush=True)
        else:
            print(f"[archive] background check of {self.path.name}: {result['status']} "
                  f"({result['detail']})", flush=True)


def stop_checks(path, timeout=None):
    """Stop (and wait for) any background check reading `path` — before it is closed or moved.
    False when one still holds the file after `timeout`: the caller must not move it."""
    with _CHECKS_LOCK:
        chk = _CHECKS.get(str(Path(path).resolve()))
    if chk is None:
        return True
    return chk.stop(timeout)


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


def _clean(s):
    """A string SQLite can store: lone UTF-16 surrogates (valid JSON, "\\ud800", invalid
    UTF-8) become "?". Without this one such string failed its whole batch, forever."""
    if isinstance(s, str):
        try:
            s.encode("utf-8")
        except UnicodeEncodeError:
            return s.encode("utf-8", "replace").decode("utf-8")
    return s


def flatten(time, source, path, value, out):
    """Append one or more archive rows for a Signal K value, matching uplink flattening."""
    time, source, path, value = _clean(time), _clean(source), _clean(path), _clean(value)
    if isinstance(value, bool):
        out.append((time, BOAT_ID, source, path, None, str(value).lower()))
    elif isinstance(value, (int, float)):
        out.append((time, BOAT_ID, source, path, float(value), None))
    elif isinstance(value, dict):
        for k, sub in value.items():
            if isinstance(sub, (int, float)) and not isinstance(sub, bool):
                out.append((time, BOAT_ID, source, _clean(f"{path}.{k}"), float(sub), None))
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


# Errors that can ONLY come from one row's input values while binding them — an allow-list,
# nothing broader:
#   UnicodeEncodeError   a lone UTF-16 surrogate in a str (encoding OUR value to UTF-8)
#   OverflowError        an int too large for SQLite INTEGER
#   TypeError / InterfaceError / ProgrammingError "binding parameter"   an unbindable type
# Retried row by row (`write_rows_each`) so only the bad row is lost. Everything else — locks,
# I/O, corruption, and in particular UnicodeDecodeError (garbled bytes READ from a damaged page;
# it is a ValueError too, which is why ValueError is not here) — fails the batch as before, so a
# damaged file is rotated and its rows requeued, never silently dropped row by row.
ROW_ERRORS = (UnicodeEncodeError, OverflowError, TypeError, sqlite3.InterfaceError,
              sqlite3.ProgrammingError)


def _is_row_error(exc) -> bool:
    if isinstance(exc, UnicodeDecodeError):
        return False                        # never: reading the file, not binding a value
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)):
        return "binding parameter" in str(exc).lower()    # not "closed database" etc.
    return isinstance(exc, ROW_ERRORS)


_INSERT = ("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
           "VALUES (?, ?, ?, ?, ?, ?)")


def write_rows_each(conn, rows):
    """Insert `rows` one statement each in ONE transaction, skipping the rows whose values
    cannot be written. Returns [(row, exc)] for the skipped ones. Anything that is not a row
    error (lock, I/O, corruption) rolls back the whole batch and raises, as `write_rows` does."""
    bad = []
    with _WRITE_LOCK:
        try:
            for r in rows:
                try:
                    conn.execute(_INSERT, r)
                except ROW_ERRORS as exc:
                    if not _is_row_error(exc):
                        raise
                    bad.append((r, exc))
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    return bad


def _note_bad_rows(state, bad, exc):
    """Count the rows that could not be written, and say so at most once per window."""
    state["rows_dropped_bad"] = state.get("rows_dropped_bad", 0) + len(bad)
    state["_bad_unlogged"] = state.get("_bad_unlogged", 0) + len(bad)

    def line(_quiet):
        n, state["_bad_unlogged"] = state["_bad_unlogged"], 0
        first = repr(bad[0][0])[:160] if bad else "?"
        return (f"[archive] dropped {n} unwritable row(s) since the last report "
                f"({state['rows_dropped_bad']} since start; {type(exc).__name__}: "
                f"{str(exc)[:120]}); e.g. {first}. The rest of each batch was written.")
    _log_every(state, "bad_rows", DROP_LOG_EVERY_S, time.monotonic, line)


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


# Once the buffer is full EVERY incoming delta trims a few rows. Logging each trim was ~200
# lines/s (~2 GB/day of Docker logs on the archive's own card), so the line is rate-limited:
# at most one per DROP_LOG_EVERY_S, carrying how many rows were dropped since the last one.
DROP_LOG_EVERY_S = float(os.environ.get("ARCHIVE_DROP_LOG_EVERY_S", "30"))


def _log_every(state, key, every_s, clock, make_line):
    """Print `make_line(suppressed)` at most once per `every_s` for `key`; returns True when it
    printed. `suppressed` = calls swallowed since the last line."""
    now = clock()
    last = state.get(f"_log_{key}_at")
    if last is not None and now - last < every_s:
        state[f"_log_{key}_quiet"] = state.get(f"_log_{key}_quiet", 0) + 1
        return False
    quiet = state.pop(f"_log_{key}_quiet", 0)
    state[f"_log_{key}_at"] = now
    print(make_line(quiet), flush=True)
    return True


def cap_buffer(buf, state, limit=None, clock=time.monotonic):
    """Drop the OLDEST rows past `limit`, counting them in state['buffer_dropped'] and saying so
    (at most once per DROP_LOG_EVERY_S). Oldest, because the newest are what the crew and the
    engine are looking at."""
    limit = MAX_BUFFER_ROWS if limit is None else int(limit)
    over = len(buf) - limit
    if limit <= 0 or over <= 0:
        return 0
    del buf[:over]
    state["buffer_dropped"] = state.get("buffer_dropped", 0) + over
    state["_drop_unlogged"] = state.get("_drop_unlogged", 0) + over

    def line(_quiet):
        n, state["_drop_unlogged"] = state["_drop_unlogged"], 0
        return (f"[archive] !!! write buffer over {limit} rows — DROPPED the oldest {n} since "
                f"the last report ({state['buffer_dropped']} dropped since start). Writes are "
                f"failing; see above.")
    _log_every(state, "drop", DROP_LOG_EVERY_S, clock, line)
    return over


# The status file is rewritten this often (a small temp + rename), so the engine can tell a live
# archiver from a dead one by its age.
STATUS_EVERY_S = float(os.environ.get("ARCHIVE_STATUS_EVERY_S", "10"))
# Not writing for this long, with rows waiting, reads as "not recording: writes failing".
WRITE_STALL_S = float(os.environ.get("ARCHIVE_WRITE_STALL_S", "30"))
# Signal K down this long reads as "not recording: no data from Signal K".
WS_DOWN_S = float(os.environ.get("ARCHIVE_WS_DOWN_S", "30"))


def recording_state(state, guard=None, now=None):
    """(recording, reason) — whether full-res rows are reaching the disk right now, and if not
    the first cause, in order of what the crew can act on. Reasons are the keys the engine maps
    to text (shared/archiver_status.py)."""
    now = time.monotonic() if now is None else now
    if guard is not None and guard.paused:
        return False, "disk_full"
    if state.get("rotate_pending"):
        return False, "rotation_pending"
    down = state.get("ws_down_since")
    if down is not None and now - down >= WS_DOWN_S:
        return False, "no_signalk"
    last_ok = state.get("last_write_ok")
    if state.get("buffered", 0) and last_ok is not None and now - last_ok >= WRITE_STALL_S:
        return False, "write_failing"
    return True, None


def write_status(state, guard=None, path=None, now=None):
    """The archiver's health as a small JSON file next to the archive, for the engine (which
    mounts the archive volume read-only and serves it on /session and /health) and for a human
    with `cat`. Best effort."""
    path = Path(path or state.get("status_path") or STATUS_FILE)
    recording, reason = recording_state(state, guard, now)
    chk = state.get("check")
    doc = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "recording": recording,
        "reason": reason,
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
        "rows_dropped_unwritable": state.get("rows_dropped_bad", 0),
        "rows_waiting": state.get("buffered", 0),
        "rows_written": state.get("total", 0),
        "corrupt_rotations_this_run": state.get("rotations", 0),
        "background_check": chk.summary() if chk is not None else None,
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
        state["buffered"] = len(buf)
        now = time.monotonic()
        if not buf:
            state["last_write_ok"] = now  # nothing stuck: a quiet bus is not a failing disk
        if (guard is not None or state.get("status_path")) and (
                state.pop("_status_dirty", False)
                or now - state.get("_status_t", float("-inf")) >= STATUS_EVERY_S):
            state["_status_t"] = now
            try:
                write_status(state, guard)
            except Exception as exc:
                print(f"[archive] status error ({exc})", flush=True)


async def _recover(buf, loop, state, path):
    """Finish a pending corruption rotation. True when the archive is usable again. On failure
    nothing is lost: rows stay in `buf` (capped) and the next flush tries again."""
    old, state["conn"] = state.get("conn"), None   # closed by reopen_fresh, whatever happens
    try:
        state["conn"], _rot = await loop.run_in_executor(None, reopen_fresh, old, path)
    except CheckerBusy as exc:
        # Nothing was closed or moved: keep writing into the current file, retry next flush.
        state["conn"] = old
        why = str(exc)
        _log_every(state, "busy", DROP_LOG_EVERY_S, time.monotonic,
                   lambda quiet: f"[archive] {why}; still recording into the current file")
        return "busy"
    except Exception as exc:
        print(f"[archive] rotation failed ({type(exc).__name__}: {exc}); "
              f"{len(buf)} rows waiting, will retry", flush=True)
        cap_buffer(buf, state)
        return False
    state["rotate_pending"] = False
    state["rotations"] = state.get("rotations", 0) + 1
    state["generation"] = state.get("generation", 0) + 1   # a new file: older verdicts are moot
    state["_status_dirty"] = True
    return True


async def _flush_once(buf, loop, state, path, guard):
    # A pending rotation goes FIRST, before the disk guard: the guard reads free pages through
    # the connection, and deciding "full" from a half-recovered archive would throw away the
    # very rows the rotation is meant to save.
    busy = False                          # a check holds the file: write to it, rotate later
    if state.get("rotate_pending"):
        got = await _recover(buf, loop, state, path)
        if not got:
            return
        busy = got == "busy"
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
    dropped = []
    for attempt in (1, 2):
        if state.get("rotate_pending") and not busy:
            buf[:0] = rows                # _recover keeps them on failure, in order
            got = await _recover(buf, loop, state, path)
            if not got:
                return
            busy = got == "busy"
            rows, buf[:] = buf[:], []
        try:
            try:
                await loop.run_in_executor(None, write_rows, state["conn"], rows)
            except ROW_ERRORS as exc:
                if not _is_row_error(exc):
                    raise
                # One bad row must not hold up the batch (it would be requeued at the front
                # forever): write the rows one by one, drop only the ones that fail.
                dropped = await loop.run_in_executor(None, write_rows_each, state["conn"], rows)
                _note_bad_rows(state, dropped, exc)
            break
        except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
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
    state["total"] = state.get("total", 0) + len(rows) - len(dropped)
    state["last_write_ok"] = time.monotonic()
    # report the AIS skip count too — silence here would look identical to a filter that
    # had quietly stopped working, or to one wrongly eating own-ship data
    skipped = state.get("skipped", 0)
    note = f" (skipped {skipped} AIS deltas)" if skipped else ""
    print(f"[archive] +{len(rows) - len(dropped)} rows (total {state['total']}){note}",
          flush=True)


def _requeue(buf, rows, state, why):
    """Put a failed batch back at the FRONT (order kept; write_rows rolled it back, so nothing
    is half-written), then enforce the RAM cap."""
    buf[:0] = rows
    _log_every(state, "requeue", DROP_LOG_EVERY_S, time.monotonic,
               lambda quiet: f"[archive] {why}; {len(rows)} rows requeued ({len(buf)} waiting)"
               + (f" [{quiet} more failed flushes not logged]" if quiet else ""))
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


# The prune deletes in chunks, taking the write lock for one chunk at a time and pausing between
# them, so the flusher gets in. One big DELETE held the lock for as long as it ran — days of
# rows on a ~10 MB/s card is minutes — and the 300k-row buffer fills in ~7 min of that.
PRUNE_CHUNK_ROWS = int(os.environ.get("ARCHIVE_PRUNE_CHUNK_ROWS", "20000"))
PRUNE_PAUSE_S = float(os.environ.get("ARCHIVE_PRUNE_PAUSE_S", "0.05"))


def prune(conn, engine_db=None, retain_days=None, chunk_rows=None, pause_s=None):
    """Delete out-of-session readings older than the retention window, `chunk_rows` at a time
    under the write lock (released between chunks). Returns rows deleted, or None when pruning
    was skipped (disabled / engine DB unreadable)."""
    days = RETAIN_DAYS if retain_days is None else float(retain_days)
    if days <= 0:
        return None
    chunk = max(1, int(PRUNE_CHUNK_ROWS if chunk_rows is None else chunk_rows))
    pause = PRUNE_PAUSE_S if pause_s is None else float(pause_s)
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
    # Walk the time index forward from a cursor, so rows kept inside a session window are read
    # once in all, not once per chunk. Each chunk deletes every row it selected, so the loop
    # always progresses and ends.
    total, chunks, since = 0, 0, ""
    while True:
        with _WRITE_LOCK:
            sel = conn.execute(
                f"SELECT id, time FROM readings "
                f"WHERE time >= ? AND {cond} ORDER BY time LIMIT ?",
                [since] + args + [chunk]).fetchall()
            if not sel:
                break
            conn.execute("DELETE FROM readings WHERE id IN (SELECT value FROM json_each(?))",
                         (json.dumps([r[0] for r in sel]),))
            conn.commit()
        total += len(sel)
        chunks += 1
        since = sel[-1][1]
        if pause > 0:
            time.sleep(pause)             # outside the lock: the flusher's turn
    if total:
        print(f"[archive] pruned {total} out-of-session rows older than {cutoff} in {chunks} "
              f"chunk(s) ({len(wins)} session window(s) kept)", flush=True)
    return total


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


def start_background_check(state, loop, db, rotated):
    """After recording has started: quick_check, in the background. The live file when it opened
    cleanly (damage found -> the flusher rotates it); the file just moved aside when it did not
    (a verdict for the human; the live file is fresh and has nothing to check)."""
    if rotated is not None:
        chk = BackgroundCheck(rotated, live=False)
    else:
        gen = state.get("generation", 0)   # the archive file this check is reading

        def on_corrupt():
            def mark():
                if state.get("generation", 0) != gen:
                    # The file was already replaced (e.g. a write found the damage first): this
                    # verdict is about the OLD file. Rotating now would move the healthy new one.
                    print("[archive] background check verdict is for a file already rotated "
                          "aside; ignoring", flush=True)
                    return
                state["rotate_pending"] = True
                state["_status_dirty"] = True
            loop.call_soon_threadsafe(mark)
        chk = BackgroundCheck(db, live=True, on_corrupt=on_corrupt)
    state["check"] = chk
    return chk.start()


async def open_until_ready(db, loop, retry_s=5.0):
    """`open_for_run`, retried instead of exiting. An error that rotation cannot fix (a missing
    or read-only directory, a dead card) must not become a crash loop that rotates a fresh file
    on every restart; wait and try again."""
    while True:
        try:
            return await loop.run_in_executor(None, open_for_run, db)
        except Exception as exc:
            print(f"[archive] cannot open {db} ({type(exc).__name__}: {exc}); "
                  f"retrying in {retry_s:.0f}s", flush=True)
            await asyncio.sleep(retry_s)


async def run(db=None, ws_connect=None, state=None, check=True):
    """Open (bounded, seconds), start recording, THEN check the file in the background.

    `db`/`ws_connect`/`state` are for tests; the boat runs `run()`."""
    # `state` carries the `self` context from the hello frame + the AIS skip count (surviving
    # reconnects, so a dropped socket does not briefly re-admit AIS before the next hello) and
    # now also the live connection, because a corruption rotation replaces it underneath every
    # writer. One holder, so nobody keeps writing to a file that has been moved aside.
    db = Path(db or ARCHIVE_DB)
    ws_connect = ws_connect or websockets.connect
    state = {} if state is None else state
    state["status_path"] = (STATUS_FILE if db == ARCHIVE_DB
                            else db.with_name("archiver-status.json"))
    loop = asyncio.get_running_loop()
    conn, top, rotated = await open_until_ready(db, loop)
    state["conn"] = conn
    state["last_write_ok"] = time.monotonic()
    state["ws_down_since"] = time.monotonic()
    if rotated:
        state["rotations"] = 1
    print(f"[archive] {db} ready (highest row id {top}) <- {SIGNALK_WS} (full resolution)"
          + (f" [recovered from {rotated.name}]" if rotated else ""), flush=True)
    guard = DiskGuard(db.parent)
    print(f"[archive] disk guard: stop full-res at {guard.high:.0f}% disk use, resume below "
          f"{guard.low:.0f}%; RAM buffer cap {MAX_BUFFER_ROWS} rows; status -> "
          f"{state['status_path']}", flush=True)
    write_status(state, guard)            # visible from the first second, not the first flush
    buf = []
    wake = asyncio.Event()
    asyncio.create_task(flusher(conn, buf, loop, state, db=db, wake=wake, guard=guard))
    asyncio.create_task(pruner(state, loop))
    if check:
        start_background_check(state, loop, db, rotated)
    while True:
        try:
            async with ws_connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[archive] connected to Signal K", flush=True)
                state["ws_down_since"] = None
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
        if state.get("ws_down_since") is None:
            state["ws_down_since"] = time.monotonic()
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
