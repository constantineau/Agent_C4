#!/usr/bin/env python3
"""Post-passage backfill: push the Pi's full-resolution archive to the cloud.

The live uplink sends 15-s aggregates; this tool sends the *full* archived record to the
cloud `/ingest/raw` once a real link is available (e.g. dockside on wifi after a passage).
It is resumable and safe to re-run: a `sync_state` cursor in the archive DB tracks the
highest row id already accepted by the cloud, so a re-run only sends what's new.

Usage (from the archiver container or any host with the DB + network to the VPS):
    python backfill.py                       # send everything not yet uploaded
    python backfill.py --since 2026-06-16T00:00:00Z --until 2026-06-16T23:59:59Z
    python backfill.py --reset               # forget the cursor, re-send from the start

Env: ARCHIVE_DB, VPS_URL, INGEST_TOKEN, BOAT_ID, BACKFILL_BATCH.
"""
import argparse
import json
import os
import urllib.request

from archiver import ARCHIVE_DB, BOAT_ID, open_db

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


def main():
    ap = argparse.ArgumentParser(description="Backfill the Pi full-res archive to the cloud.")
    ap.add_argument("--since", help="only rows with time >= this ISO8601 timestamp")
    ap.add_argument("--until", help="only rows with time <= this ISO8601 timestamp")
    ap.add_argument("--reset", action="store_true", help="reset the upload cursor first")
    args = ap.parse_args()

    conn = open_db()
    if args.reset:
        set_cursor(conn, 0)
        print("[backfill] cursor reset to 0", flush=True)

    # Two modes. Default (no time filter) is the routine catch-up: page by id from the
    # saved cursor and advance it as the cloud accepts batches, so re-runs only send new
    # rows. A time range is an ad-hoc re-export — it streams the requested window and never
    # touches the cursor (so it can't skip rows the catch-up flow still owes).
    ad_hoc = bool(args.since or args.until)
    where = ["id > ?"]
    params = [0 if ad_hoc else get_cursor(conn)]
    if args.since:
        where.append("time >= ?")
        params.append(args.since)
    if args.until:
        where.append("time <= ?")
        params.append(args.until)
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

    print(f"[backfill] done — {sent} reading(s) uploaded from {ARCHIVE_DB}", flush=True)


if __name__ == "__main__":
    main()
