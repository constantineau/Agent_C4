#!/usr/bin/env python3
"""Generate fake SR33 telemetry and POST it through the ingestion API.

Exercises the whole Phase 0 path (ingestion -> TimescaleDB) with no boat and no extra
deps — stdlib only. Simulates a boat working upwind on Lake Huron with slowly shifting
breeze, emitting 15-s aggregates like the real Pi uplink will.

Usage:
    python3 vps/db/seed/fake_telemetry.py [--minutes 120] [--url http://localhost:8101]
                                          [--token dev-ingest-token]
"""
import argparse
import json
import math
import urllib.request
from datetime import datetime, timedelta, timezone

PORT_HURON = (43.00, -82.42)


def make_points(minutes: int):
    n = (minutes * 60) // 15
    start = datetime.now(timezone.utc) - timedelta(seconds=n * 15)
    lat, lon = PORT_HURON
    pts = []
    for i in range(n + 1):
        t = start + timedelta(seconds=i * 15)
        # Breeze: ~12 kn mean, slow oscillation + a persistent shift.
        twd = 200 + 12 * math.sin(i / 120) + 0.01 * i
        tws = 12 + 3 * math.sin(i / 90) + 1.5 * math.sin(i / 17)
        twa = 42 + 4 * math.sin(i / 40)          # close-hauled, port tack
        heading = (twd - twa) % 360
        stw = 6.3 + 0.6 * math.sin(i / 30) + 0.15 * math.sin(i / 7)
        sog = stw + 0.3 * math.sin(i / 50)       # a little current
        # crude dead-reckon north-ish
        lat += (stw / 3600.0) * 0.25 * math.cos(math.radians(heading)) / 60.0 * 15
        lon += (stw / 3600.0) * 0.25 * math.sin(math.radians(heading)) / 60.0 * 15
        pts.append({
            "time": t.isoformat(),
            "tws": round(tws, 2), "twa": round(twa, 2), "twd": round(twd % 360, 2),
            "aws": round(tws + stw * math.cos(math.radians(twa)), 2),
            "awa": round(twa - 3, 2),
            "stw": round(stw, 2), "sog": round(sog, 2),
            "cog": round(heading + 2, 2), "heading": round(heading, 2),
            "lat": round(lat, 6), "lon": round(lon, 6),
            "depth": round(30 + 10 * math.sin(i / 60), 1),
        })
    return pts


def post_batch(url, token, boat_id, points):
    body = json.dumps({"boat_id": boat_id, "points": points}).encode()
    req = urllib.request.Request(
        f"{url}/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120)
    ap.add_argument("--url", default="http://localhost:8101")
    ap.add_argument("--token", default="dev-ingest-token")
    ap.add_argument("--boat-id", default="sr33")
    args = ap.parse_args()

    pts = make_points(args.minutes)
    total = 0
    for i in range(0, len(pts), 200):  # chunk so batches stay small
        chunk = pts[i:i + 200]
        res = post_batch(args.url, args.token, args.boat_id, chunk)
        total += res.get("accepted", 0)
    print(f"posted {total} telemetry points over the last {args.minutes} min to {args.url}")


if __name__ == "__main__":
    main()
