#!/usr/bin/env python3
"""Synthetic moving-target AIS injector for the bench (stands in for the em-trak B951).

No real AIS log exists yet, so this drives the Phase-6 AIS path end to end: it POSTs raw
target observations to the ingestion `/ingest/ais` endpoint exactly as the Pi uplink does,
and the agent's ais.py recomputes range/bearing/CPA/TCPA against own ship live.

By default it also injects a STABLE synthetic own-ship fix (source `sim-ownship`) to
`/ingest/raw`, so the CPA geometry is deterministic without depending on the Signal K sample
boat (which teleports and sits in the Baltic). Targets are placed relative to that own ship —
one on a deliberate CLOSING course (small CPA, positive TCPA) to exercise the collision guard
and, later, Phase-6.1 alerting; one crossing; one opening astern.

Usage:
    python3 vps/db/seed/ais_inject.py                     # 10 min, 5-s ticks, with own ship
    python3 vps/db/seed/ais_inject.py --once              # single observation, then exit
    python3 vps/db/seed/ais_inject.py --minutes 30 --dt 5
    python3 vps/db/seed/ais_inject.py --no-own            # use the live own-ship fix instead
"""
import argparse
import json
import math
import time
import urllib.request
from datetime import datetime, timezone

OWN_START = (43.00, -82.42)   # Port Huron-ish; only used when injecting a synthetic own ship
OWN_SOG_KN = 6.5
OWN_COG_DEG = 0.0             # heading north

_KN = 1 / 1.943844           # kn -> m/s, for the raw own-ship SI payload


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset(lat0, lon0, bearing_deg, range_nm):
    """Position at bearing/range (nm) from a point — equirectangular, matches ais.py."""
    br = math.radians(bearing_deg)
    dn = range_nm * math.cos(br)
    de = range_nm * math.sin(br)
    return lat0 + dn / 60.0, lon0 + de / (60.0 * math.cos(math.radians(lat0)))


def _advance(lat, lon, sog_kn, cog_deg, dt_s):
    """Dead-reckon a position forward dt seconds along course at speed."""
    dist_nm = sog_kn * (dt_s / 3600.0)
    return _offset(lat, lon, cog_deg, dist_nm)


def make_targets(own_lat, own_lon):
    """Three targets defined relative to own ship; courses fixed once, then dead-reckoned."""
    # A collision-course target: course aimed back at own ship, offset a few degrees so the
    # CPA is small but non-zero (a realistic near-miss the guard should flag).
    brg_to_own = (30 + 180 + 8) % 360
    tgt_lat, tgt_lon = _offset(own_lat, own_lon, 30, 4.0)
    closing = {"mmsi": 366000111, "name": "CLOSING TUG",
               "lat": tgt_lat, "lon": tgt_lon, "sog": 9.0, "cog": brg_to_own}

    cr_lat, cr_lon = _offset(own_lat, own_lon, 300, 5.0)
    crossing = {"mmsi": 366000222, "name": "CROSSING BULKER",
                "lat": cr_lat, "lon": cr_lon, "sog": 12.0, "cog": 70.0}

    as_lat, as_lon = _offset(own_lat, own_lon, 180, 2.0)
    opening = {"mmsi": 366000333, "name": "TRAILING YACHT",
               "lat": as_lat, "lon": as_lon, "sog": 5.0, "cog": 180.0}
    return [closing, crossing, opening]


def post(url, path, token, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def own_readings(boat_id, lat, lon, sog_kn, cog_deg):
    now = _now()
    src = "sim-ownship"
    return {"boat_id": boat_id, "readings": [
        {"time": now, "source": src, "path": "navigation.position.latitude", "value": lat},
        {"time": now, "source": src, "path": "navigation.position.longitude", "value": lon},
        {"time": now, "source": src, "path": "navigation.speedOverGround", "value": sog_kn * _KN},
        {"time": now, "source": src, "path": "navigation.courseOverGroundTrue",
         "value": math.radians(cog_deg)},
    ]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8101")
    ap.add_argument("--token", default="dev-ingest-token")
    ap.add_argument("--boat-id", default="sr33")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--dt", type=float, default=5, help="seconds between observations")
    ap.add_argument("--once", action="store_true", help="post a single observation and exit")
    ap.add_argument("--no-own", action="store_true",
                    help="don't inject a synthetic own ship (use the live fix)")
    args = ap.parse_args()

    inject_own = not args.no_own
    own_lat, own_lon = OWN_START
    targets = make_targets(own_lat, own_lon)
    ticks = 1 if args.once else max(1, int((args.minutes * 60) / args.dt))

    for i in range(ticks):
        now = _now()
        if inject_own:
            post(args.url, "/ingest/raw", args.token,
                 own_readings(args.boat_id, own_lat, own_lon, OWN_SOG_KN, OWN_COG_DEG))

        obs = [{"time": now, "mmsi": t["mmsi"], "name": t["name"],
                "lat": round(t["lat"], 6), "lon": round(t["lon"], 6),
                "sog": t["sog"], "cog": round(t["cog"], 1)} for t in targets]
        res = post(args.url, "/ingest/ais", args.token,
                   {"boat_id": args.boat_id, "targets": obs})
        print(f"[ais_inject] tick {i+1}/{ticks} -> {res.get('accepted')} target(s)"
              + (" + own fix" if inject_own else ""), flush=True)

        if args.once:
            break
        # advance everyone for the next tick
        if inject_own:
            own_lat, own_lon = _advance(own_lat, own_lon, OWN_SOG_KN, OWN_COG_DEG, args.dt)
        for t in targets:
            t["lat"], t["lon"] = _advance(t["lat"], t["lon"], t["sog"], t["cog"], args.dt)
        time.sleep(args.dt)


if __name__ == "__main__":
    main()
