#!/usr/bin/env python3
"""Post-passage backfill: push the Pi's full-resolution archive to the cloud.

The live uplink sends 15-s aggregates; this tool sends the *full* archived record to the
cloud `/ingest/raw` once a real link is available (e.g. dockside on wifi after a passage).
It is resumable and safe to re-run: a `sync_state` cursor in the archive DB tracks the
highest row id already accepted by the cloud, so a re-run only sends what's new.

SESSION-AWARE (default): only readings inside RACE-SESSION windows (the iPad record switch,
engine `sessions` table) are pushed — a day sail or delivery never leaves the boat. Each run
also pushes the crew SAIL LOG (`crew.sail.state` readings) and a `crew.session` marker per
closed session, so the cloud debrief can find the windows. `--all` restores the old
everything mode; `--since/--until` stays the ad-hoc window export.

Usage (from the archiver container or any host with the DB + network to the VPS):
    python backfill.py                       # send session windows not yet uploaded
    python backfill.py --all                 # send everything not yet uploaded
    python backfill.py --since 2026-06-16T00:00:00Z --until 2026-06-16T23:59:59Z
    python backfill.py --reset               # forget the cursor, re-send from the start

Env: ARCHIVE_DB, ENGINE_DB, VPS_URL, INGEST_TOKEN, BOAT_ID, BACKFILL_BATCH.
"""
import argparse
import json
import os
import sqlite3
import urllib.request

from archiver import ARCHIVE_DB, BOAT_ID, ENGINE_DB, open_db, session_windows

VPS_URL = os.environ.get("VPS_URL", "http://localhost:8101")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BATCH = int(os.environ.get("BACKFILL_BATCH", "1000"))
CURSOR_KEY = "backfill_last_id"


def _post(readings):
    body = json.dumps({"boat_id": BOAT_ID, "readings": readings}).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest/raw", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def get_cursor(conn):
    row = conn.execute("SELECT v FROM sync_state WHERE k=?", (CURSOR_KEY,)).fetchone()
    return int(row[0]) if row else 0


def set_cursor(conn, last_id):
    conn.execute(
        "INSERT INTO sync_state (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (CURSOR_KEY, str(last_id)),
    )
    conn.commit()


def row_to_reading(r):
    # r = (id, time, source, path, value, str_value)
    reading = {"time": r[1], "source": r[2], "path": r[3]}
    if r[4] is not None:
        reading["value"] = r[4]
    if r[5] is not None:
        reading["str_value"] = r[5]
    return reading


def _engine(ro=True):
    return sqlite3.connect(f"file:{ENGINE_DB}?mode=ro" if ro else ENGINE_DB,
                           uri=ro, timeout=10)


def push_sail_log(conn):
    """Push the crew sail-state history as `crew.sail.state` readings (cursor: last ts sent).
    SESSION-SCOPED like everything else: only changes inside race-session windows upload — a
    day sail's sail changes never leave the boat (and the per-config polar record is built
    from RACE data only)."""
    try:
        ec = _engine()
        row = conn.execute("SELECT v FROM sync_state WHERE k='backfill_sail_ts'").fetchone()
        since = float(row[0]) if row else 0.0
        wins = ec.execute("SELECT start_ts, end_ts FROM sessions").fetchall()
        logs = [r for r in ec.execute("SELECT ts, flying, reef, out_of_service FROM sail_log "
                                      "WHERE ts > ? ORDER BY ts", (since,)).fetchall()
                if any(a <= r[0] and (b is None or r[0] <= b) for a, b in wins)]
        ec.close()
    except Exception as exc:
        print(f"[backfill] sail log skipped ({exc})", flush=True)
        return
    if not logs:
        return
    from datetime import datetime, timezone
    readings = [{"time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                 "source": "crew", "path": "crew.sail.state",
                 "str_value": json.dumps({"flying": json.loads(fly or "[]"), "reef": reef,
                                          "out_of_service": json.loads(oos or "[]")})}
                for ts, fly, reef, oos in logs]
    _post(readings)
    conn.execute("INSERT INTO sync_state (k, v) VALUES ('backfill_sail_ts', ?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(max(r[0] for r in logs)),))
    conn.commit()
    print(f"[backfill] sent {len(readings)} sail-log entries", flush=True)


def push_session_markers(conn):
    """One `crew.session` marker per CLOSED session (cursor: last session id sent) — the cloud
    debrief finds the race windows through these."""
    try:
        ec = _engine()
        row = conn.execute("SELECT v FROM sync_state WHERE k='backfill_session_id'").fetchone()
        since = int(row[0]) if row else 0
        rows = ec.execute("SELECT id, name, race_id, kind, start_ts, end_ts FROM sessions "
                          "WHERE id > ? AND end_ts IS NOT NULL ORDER BY id", (since,)).fetchall()
        ec.close()
    except Exception as exc:
        print(f"[backfill] session markers skipped ({exc})", flush=True)
        return
    if not rows:
        return
    from datetime import datetime, timezone
    readings = [{"time": datetime.fromtimestamp(a, tz=timezone.utc).isoformat(),
                 "source": "crew", "path": "crew.session",
                 "str_value": json.dumps({"id": i, "name": n, "race_id": rid, "kind": k,
                                          "start_ts": a, "end_ts": b})}
                for i, n, rid, k, a, b in rows]
    _post(readings)
    conn.execute("INSERT INTO sync_state (k, v) VALUES ('backfill_session_id', ?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(rows[-1][0]),))
    conn.commit()
    print(f"[backfill] sent {len(readings)} session marker(s)", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Backfill the Pi full-res archive to the cloud.")
    ap.add_argument("--since", help="only rows with time >= this ISO8601 timestamp")
    ap.add_argument("--until", help="only rows with time <= this ISO8601 timestamp")
    ap.add_argument("--all", action="store_true",
                    help="push everything (default: only race-session windows)")
    ap.add_argument("--reset", action="store_true", help="reset the upload cursor first")
    args = ap.parse_args()

    conn = open_db()
    if args.reset:
        set_cursor(conn, 0)
        print("[backfill] cursor reset to 0", flush=True)

    # Modes. Default = routine SESSION catch-up: page by id from the saved cursor, but send
    # only rows inside race-session windows (the cursor still advances past skipped rows — a
    # day sail is deliberately never sent). --all = the old everything mode. A time range is
    # an ad-hoc re-export — it streams the requested window and never touches the cursor.
    ad_hoc = bool(args.since or args.until)
    where = ["id > ?"]
    params = [0 if ad_hoc else get_cursor(conn)]
    if args.since:
        where.append("time >= ?")
        params.append(args.since)
    if args.until:
        where.append("time <= ?")
        params.append(args.until)
    if not ad_hoc and not args.all:
        wins = session_windows()
        if wins is None:
            print("[backfill] engine sessions table unreadable — nothing sent "
                  "(use --all to push everything)", flush=True)
            return
        if not wins:
            print("[backfill] no race sessions recorded — nothing to send "
                  "(start one from the iPad, or use --all)", flush=True)
            push_sail_log(conn)
            return
        where.append("(" + " OR ".join(["(time >= ? AND time <= ?)"] * len(wins)) + ")")
        for a, b in wins:
            params += [a, b]
        print(f"[backfill] session mode — {len(wins)} window(s)", flush=True)
    sql = ("SELECT id, time, source, path, value, str_value FROM readings "
           f"WHERE {' AND '.join(where)} ORDER BY id LIMIT ?")

    sent = 0
    while True:
        rows = conn.execute(sql, (*params, BATCH)).fetchall()
        if not rows:
            break
        _post([row_to_reading(r) for r in rows])
        last_id = rows[-1][0]
        params[0] = last_id          # page forward by id regardless of mode
        if not ad_hoc:
            set_cursor(conn, last_id)
        sent += len(rows)
        print(f"[backfill] sent {len(rows)} (cumulative {sent}, through id {last_id})",
              flush=True)

    if not ad_hoc:
        push_sail_log(conn)
        push_session_markers(conn)
    print(f"[backfill] done — {sent} reading(s) uploaded from {ARCHIVE_DB}", flush=True)


if __name__ == "__main__":
    main()
