"""Carry the drain across a corrupt page without losing the readable rows around it.

backfill.py pages with `id > cursor ORDER BY id LIMIT n`. When that page spans a corrupt
region the whole SELECT raises, so the cursor can never advance and the drain wedges — which
is what happened at ids 470500..471000.

Same technique as salvage.py: bisect the range, and on failure recurse until single rows, so
only the genuinely unreadable rows are lost instead of the whole block. Recovered rows are
POSTed to the VPS, then the cursor is advanced past the region so the supervised drain
resumes on its own.

Re-run safe: it only sends rows above the current cursor, and advances the cursor only after
a successful POST.
"""
import json
import os
import sqlite3
import sys
import urllib.request

DB = "/var/lib/sr33/archive/archive.db"
VPS = os.environ.get("VPS_URL", "http://100.88.252.115:8101")
TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BOAT = os.environ.get("BOAT_ID", "sr33")
UP_TO = int(sys.argv[1])          # advance the cursor to this id

ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
lost = []


def fetch(lo, hi):
    """Rows in (lo, hi], bisecting around corrupt pages. Returns list of row tuples."""
    try:
        return ro.execute(
            "select id, time, source, path, value, str_value from readings "
            "where id > ? and id <= ? order by id", (lo, hi)).fetchall()
    except sqlite3.DatabaseError:
        if hi - lo <= 1:
            lost.append(hi)
            return []
        mid = (lo + hi) // 2
        return fetch(lo, mid) + fetch(mid, hi)


def post(readings):
    body = json.dumps({"boat_id": BOAT, "readings": readings}).encode()
    req = urllib.request.Request(
        f"{VPS}/ingest/raw", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    urllib.request.urlopen(req, timeout=60).read()


def to_reading(r):
    out = {"time": r[1], "source": r[2], "path": r[3]}
    if r[4] is not None:
        out["value"] = r[4]
    if r[5] is not None:
        out["str_value"] = r[5]
    return out


cur = int(ro.execute("select v from sync_state where k='backfill_last_id'").fetchone()[0])
print(f"cursor={cur} target={UP_TO}")
if cur >= UP_TO:
    print("already past target — nothing to do")
    sys.exit(0)

rows = fetch(cur, UP_TO)
print(f"recovered {len(rows)} readable rows, {len(lost)} unreadable")

for i in range(0, len(rows), 2000):
    post([to_reading(r) for r in rows[i:i + 2000]])
    print(f"  sent {min(i + 2000, len(rows))}/{len(rows)}")

# Advance the cursor only after everything readable is safely accepted.
rw = sqlite3.connect(DB, timeout=60)
rw.execute("insert into sync_state (k, v) values ('backfill_last_id', ?) "
           "on conflict(k) do update set v=excluded.v", (str(UP_TO),))
rw.commit()
rw.close()
print(f"cursor advanced to {UP_TO}")
if lost:
    print("UNREADABLE rowids:", lost[:50], "..." if len(lost) > 50 else "")
