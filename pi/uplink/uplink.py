#!/usr/bin/env python3
"""SR33 telemetry uplink (Pi -> VPS).

Subscribes to the Signal K WebSocket delta stream, maps Signal K paths (SI units) to our
normalized channels (knots/degrees/metres), builds 15-second aggregates, and POSTs them to
the VPS ingestion API. A disk-backed queue holds batches when the cloud link (Starlink) is
down and replays them in order on reconnect — the boat is the source of truth, so nothing
is lost.

Identical on bench and boat; the ONLY difference is CAN_IFACE (vcan0 vs can0), which only
affects Signal K's input, not this service.
"""
import asyncio
import json
import math
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import websockets

from shared.units import ms_to_kn, rad_to_deg

CAN_IFACE = os.environ.get("CAN_IFACE", "vcan0")
SIGNALK_WS = os.environ.get(
    "SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all"
)
VPS_URL = os.environ.get("VPS_URL", "http://localhost:8101")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AGG_SECONDS = int(os.environ.get("AGG_SECONDS", "15"))
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "/var/lib/sr33/queue"))

_ident = lambda x: x

# Signal K path -> (our channel, SI->stored converter). True-wind paths are populated by
# the signalk-derived-data plugin; until it's enabled they're simply absent (null).
PATH_MAP = {
    "navigation.speedThroughWater":    ("stw", ms_to_kn),
    "navigation.speedOverGround":      ("sog", ms_to_kn),
    "navigation.courseOverGroundTrue": ("cog", rad_to_deg),
    "navigation.headingTrue":          ("heading", rad_to_deg),
    "environment.wind.speedApparent":  ("aws", ms_to_kn),
    "environment.wind.angleApparent":  ("awa", rad_to_deg),
    "environment.wind.speedTrue":      ("tws", ms_to_kn),
    "environment.wind.angleTrueWater": ("twa", rad_to_deg),
    "environment.wind.directionTrue":  ("twd", rad_to_deg),
    "environment.depth.belowTransducer": ("depth", _ident),
}
# Compass channels (0–360° true) need a circular mean; everything else a plain mean.
CIRCULAR = {"cog", "heading", "twd"}


def _mean(vals):
    return sum(vals) / len(vals)


def _circular_mean(vals):
    s = sum(math.sin(math.radians(v)) for v in vals)
    c = sum(math.cos(math.radians(v)) for v in vals)
    return (math.degrees(math.atan2(s, c)) + 360) % 360


def build_point(window, last_pos):
    point = {}
    for ch, vals in window.items():
        if vals:
            point[ch] = round(_circular_mean(vals) if ch in CIRCULAR else _mean(vals), 3)
    if last_pos.get("lat") is not None:
        point["lat"] = round(last_pos["lat"], 6)
        point["lon"] = round(last_pos["lon"], 6)
    return point


def parse_delta(msg, window, last_pos):
    try:
        data = json.loads(msg)
    except ValueError:
        return
    for upd in data.get("updates", []):
        for v in upd.get("values", []):
            path, val = v.get("path"), v.get("value")
            if path == "navigation.position" and isinstance(val, dict):
                last_pos["lat"] = val.get("latitude")
                last_pos["lon"] = val.get("longitude")
            elif path in PATH_MAP and isinstance(val, (int, float)):
                ch, conv = PATH_MAP[path]
                window[ch].append(conv(val))


# --- cloud POST with store-and-forward ---------------------------------------
def _post(batch):
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def _enqueue(batch):
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = batch["points"][0]["time"].replace(":", "").replace("-", "")
    (QUEUE_DIR / f"{stamp}.json").write_text(json.dumps(batch))


def _flush_queue():
    if not QUEUE_DIR.exists():
        return
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            _post(json.loads(f.read_text()))
            f.unlink()
        except Exception:
            return  # link still down — leave the rest queued


def send(point):
    batch = {"boat_id": BOAT_ID, "points": [point]}
    try:
        _post(batch)
        _flush_queue()              # opportunistically drain backlog
        print(f"[uplink] sent {sorted(point)} ", flush=True)
    except Exception as exc:
        _enqueue(batch)             # cloud link down -> store and forward
        print(f"[uplink] link down, queued ({exc})", flush=True)


async def flusher(window, last_pos, loop):
    while True:
        await asyncio.sleep(AGG_SECONDS)
        point = build_point(window, last_pos)
        window.clear()
        if point:
            point["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            await loop.run_in_executor(None, send, point)


async def run():
    print(f"[uplink] CAN_IFACE={CAN_IFACE} signalk={SIGNALK_WS} -> {VPS_URL}", flush=True)
    window = defaultdict(list)
    last_pos = {}
    loop = asyncio.get_running_loop()
    asyncio.create_task(flusher(window, last_pos, loop))
    while True:
        try:
            async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[uplink] connected to Signal K", flush=True)
                async for msg in ws:
                    parse_delta(msg, window, last_pos)
        except Exception as exc:
            print(f"[uplink] Signal K WS error ({exc}); retrying in 3s", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
