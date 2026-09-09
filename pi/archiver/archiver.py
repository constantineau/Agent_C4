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
import json
import os
import sqlite3
import threading
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


def rotate_corrupt(path: Path = ARCHIVE_DB, stamp=None) -> Path:
    """Move a corrupt archive (and its -wal/-shm) aside and return the new path.

    Named `archive.corrupt-<YYYYmmddTHHMMSS>.db`, matching the two files a human created by
    hand for exactly this. Kept, never deleted: salvage recovers almost all of it."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
    # Second-resolution names collide when two rotations land in the same second, and a plain
    # rename would then DELETE the first corrupt archive — the one thing this must never do.
    # (Caught by test_brownout, which rotated twice in one second.)
    n = 1
    while Path(str(dest)).exists():
        dest = path.with_name(f"{path.stem}.corrupt-{stamp}-{n}{path.suffix}")
        n += 1
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            src.rename(Path(str(dest) + suffix))
    print(f"[archive] CORRUPT archive moved aside -> {dest.name}; starting a fresh one. "
          f"KEEP that file: pi/archiver/tools/salvage.py recovers ~all of it.", flush=True)
    return dest


def open_db_resilient(path: Path = ARCHIVE_DB):
    """Open the archive, rotating it aside if it is unusable. Returns (conn, rotated_to|None).

    This is the whole brownout story: a bad card must cost minutes, not six weeks."""
    rotated = None
    conn = None
    try:
        conn = open_db(path)
        if not integrity_ok(conn):
            raise sqlite3.DatabaseError("quick_check failed: database disk image is malformed")
        return conn, None
    except sqlite3.DatabaseError as exc:
        if not _is_corruption(exc):
            raise
        print(f"[archive] {path.name} is unusable ({exc})", flush=True)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        rotated = rotate_corrupt(path)
        conn = open_db(path)          # CREATE TABLE IF NOT EXISTS self-initialises
        _note_rotation(conn, rotated)
        return conn, rotated


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
    with _WRITE_LOCK:
        conn.executemany(
            "INSERT INTO readings (time, boat_id, source, path, value, str_value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


async def flusher(conn, buf, loop, state=None, db=None, wake=None):
    """Drain the buffer to disk every FLUSH_SECONDS, for as long as this process lives.

    Wrapped end to end because an unhandled exception here does not crash the service — it
    kills this ONE task, asyncio swallows the traceback, and the archiver goes on looking
    perfectly healthy while recording nothing. That is a worse failure than the crash-loop it
    replaces, so the recorder must be un-killable: corruption rotates the file and continues,
    and anything else is logged and retried on the next tick with the rows put back."""
    total = 0
    state = state if state is not None else {}
    while True:
        # Wake on the timer OR as soon as the reader says the buffer is full (FLUSH_ROWS).
        # The reader used to write that case itself; it now signals instead, so there is
        # exactly ONE writer and therefore exactly one place that handles corruption.
        if wake is None:
            await asyncio.sleep(FLUSH_SECONDS)
        else:
            try:
                await asyncio.wait_for(wake.wait(), timeout=FLUSH_SECONDS)
            except asyncio.TimeoutError:
                pass
            wake.clear()
        if not buf:
            continue
        rows, buf[:] = buf[:], []
        try:
            await loop.run_in_executor(None, write_rows, state["conn"], rows)
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                buf[:0] = rows            # transient (locked/busy) — retry next tick, in order
                print(f"[archive] write failed, {len(rows)} rows requeued ({exc})", flush=True)
                continue
            print(f"[archive] CORRUPTION during write ({exc}) — rotating and continuing",
                  flush=True)
            try:
                state["conn"], _rot = await loop.run_in_executor(
                    None, open_db_resilient, db or ARCHIVE_DB)
                await loop.run_in_executor(None, write_rows, state["conn"], rows)
            except Exception as exc2:     # keep recording even if recovery half-failed
                print(f"[archive] recovery incomplete ({exc2}); {len(rows)} rows dropped",
                      flush=True)
                continue
        except Exception as exc:
            buf[:0] = rows
            print(f"[archive] unexpected flush error, {len(rows)} rows requeued ({exc})",
                  flush=True)
            continue
        total += len(rows)
        # report the AIS skip count too — silence here would look identical to a filter that
        # had quietly stopped working, or to one wrongly eating own-ship data
        skipped = state.get("skipped", 0)
        note = f" (skipped {skipped} AIS deltas)" if skipped else ""
        print(f"[archive] +{len(rows)} rows (total {total}){note}", flush=True)


# ---- RETENTION PRUNE (race sessions) ------------------------------------------------------
# The archive records EVERYTHING (collect-everything), but only RACE-SESSION windows — the
# owner's record switch, started/ended from the iPad console (engine `sessions` table) — are
# kept long-term. Outside any session, readings older than ARCHIVE_RETAIN_DAYS are deleted, so
# a day sail or a delivery never accumulates on the SD card (deleted pages are reused; the file
# stops growing). SAFETY: if the engine DB / sessions table can't be read, NOTHING is pruned —
# we must never delete a race because a volume didn't mount. 0 disables (the bench keeps its
# replayed sample data).
RETAIN_DAYS = float(os.environ.get("ARCHIVE_RETAIN_DAYS", "14"))
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
            await loop.run_in_executor(None, prune, state["conn"])
        except Exception as exc:      # never let housekeeping kill the recorder
            print(f"[archive] prune error: {exc}", flush=True)
        await asyncio.sleep(PRUNE_EVERY_S)


async def run():
    # `state` carries the `self` context from the hello frame + the AIS skip count (surviving
    # reconnects, so a dropped socket does not briefly re-admit AIS before the next hello) and
    # now also the live connection, because a corruption rotation replaces it underneath every
    # writer. One holder, so nobody keeps writing to a file that has been moved aside.
    state = {}
    conn, rotated = open_db_resilient()
    state["conn"] = conn
    n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    print(f"[archive] {ARCHIVE_DB} ready ({n} rows) <- {SIGNALK_WS} (full resolution)"
          + (f" [recovered from {rotated.name}]" if rotated else ""), flush=True)
    buf = []
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    asyncio.create_task(flusher(conn, buf, loop, state, wake=wake))
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
