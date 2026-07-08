#!/usr/bin/env python3
"""Generate fake SR33 telemetry and POST it through the ingestion API.

Exercises the real path (ingestion `/ingest/raw` -> TimescaleDB `telemetry_raw`) with no
boat and no extra deps — stdlib only. Simulates a boat working upwind on Lake Huron with
slowly shifting breeze, emitting one 15-s reading per channel like the real Pi uplink,
as raw SI (source,path,value) rows — the collect-everything model every reader queries.
(The original wide-table `/ingest` -> `telemetry` path is retired; migration 006.)

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
KN = 0.514444          # kn -> m/s
RAD = math.pi / 180.0  # deg -> rad
SOURCE = "fake-seed"   # $source label the readings carry (shows up in /sources)


def make_readings(minutes: int):
    n = (minutes * 60) // 15
    start = datetime.now(timezone.utc) - timedelta(seconds=n * 15)
    lat, lon = PORT_HURON
    readings = []
    for i in range(n + 1):
        t = (start + timedelta(seconds=i * 15)).isoformat()
        # Breeze: ~12 kn mean, slow oscillation + a persistent shift.
        twd = (200 + 12 * math.sin(i / 120) + 0.01 * i) % 360
        tws = 12 + 3 * math.sin(i / 90) + 1.5 * math.sin(i / 17)
        twa = 42 + 4 * math.sin(i / 40)          # close-hauled, port tack
        heading = (twd - twa) % 360
        stw = 6.3 + 0.6 * math.sin(i / 30) + 0.15 * math.sin(i / 7)
        sog = stw + 0.3 * math.sin(i / 50)       # a little current
        aws = tws + stw * math.cos(twa * RAD)
        # crude dead-reckon north-ish
        lat += (stw / 3600.0) * 0.25 * math.cos(heading * RAD) / 60.0 * 15
        lon += (stw / 3600.0) * 0.25 * math.sin(heading * RAD) / 60.0 * 15
        depth = 30 + 10 * math.sin(i / 60)
        for path, value in (
            ("environment.wind.speedTrue", tws * KN),
            ("environment.wind.angleTrueWater", twa * RAD),
            ("environment.wind.directionTrue", twd * RAD),
            ("environment.wind.speedApparent", aws * KN),
            ("environment.wind.angleApparent", (twa - 3) * RAD),
            ("navigation.speedThroughWater", stw * KN),
            ("navigation.speedOverGround", sog * KN),
            ("navigation.courseOverGroundTrue", ((heading + 2) % 360) * RAD),
            ("navigation.headingTrue", heading * RAD),
            ("navigation.position.latitude", lat),
            ("navigation.position.longitude", lon),
            ("environment.depth.belowTransducer", depth),
        ):
            readings.append({"time": t, "source": SOURCE, "path": path,
                             "value": round(value, 6)})
    return readings


def post_batch(url, token, boat_id, readings):
    body = json.dumps({"boat_id": boat_id, "readings": readings}).encode()
    req = urllib.request.Request(
        f"{url}/ingest/raw", data=body, method="POST",
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

    rows = make_readings(args.minutes)
    total = 0
    for i in range(0, len(rows), 1200):  # chunk so batches stay small (~100 x 12-channel ticks)
        chunk = rows[i:i + 1200]
        res = post_batch(args.url, args.token, args.boat_id, chunk)
        total += res.get("accepted", 0)
    print(f"posted {total} raw readings over the last {args.minutes} min to {args.url}/ingest/raw")


if __name__ == "__main__":
    main()
