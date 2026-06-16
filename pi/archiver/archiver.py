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
from datetime import datetime, timezone
from pathlib import Path

import websockets

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


def parse_delta(msg, default_time):
    """Turn one Signal K delta message into a list of archive rows (full resolution)."""
    try:
        data = json.loads(msg)
    except ValueError:
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


async def flusher(conn, buf, loop):
    total = 0
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        if not buf:
            continue
        rows, buf[:] = buf[:], []
        await loop.run_in_executor(None, write_rows, conn, rows)
        total += len(rows)
        print(f"[archive] +{len(rows)} rows (total {total})", flush=True)


async def run():
    conn = open_db()
    n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    print(f"[archive] {ARCHIVE_DB} ready ({n} rows) <- {SIGNALK_WS} (full resolution)",
          flush=True)
    buf = []
    loop = asyncio.get_running_loop()
    asyncio.create_task(flusher(conn, buf, loop))
    while True:
        try:
            async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[archive] connected to Signal K", flush=True)
                async for msg in ws:
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    rows = parse_delta(msg, now)
                    if rows:
                        buf.extend(rows)
                        if len(buf) >= FLUSH_ROWS:
                            chunk, buf[:] = buf[:], []
                            await loop.run_in_executor(None, write_rows, conn, chunk)
        except Exception as exc:
            print(f"[archive] Signal K WS error ({exc}); retrying in 3s", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
