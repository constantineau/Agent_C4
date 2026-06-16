#!/usr/bin/env python3
"""SR33 telemetry uplink (Pi -> VPS) — Phase 3 skeleton.

Final behavior (brief §4): subscribe to the Signal K WebSocket, build 15-second
aggregates of each channel, and POST batches to the VPS ingestion API. A disk-backed
queue holds batches when Starlink drops so nothing is lost; on reconnect it replays in
order. The boat is the source of truth.

This skeleton wires the config (incl. the single CAN_IFACE portability switch), the
aggregation window, and the store-and-forward queue shape. The Signal K subscription and
N2K decode are TODO — develop against vcan0 + replayed logs before touching the boat.
"""
import json
import os
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

# --- config (env-driven; the ONE bench/boat switch is CAN_IFACE) -------------
CAN_IFACE = os.environ.get("CAN_IFACE", "vcan0")          # vcan0 bench / can0 boat
SIGNALK_WS = os.environ.get("SIGNALK_WS", "ws://localhost:3000/signalk/v1/stream")
VPS_URL = os.environ.get("VPS_URL", "https://nav.example.com")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AGG_SECONDS = int(os.environ.get("AGG_SECONDS", "15"))
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "/var/lib/sr33/queue"))


def aggregate(samples: dict) -> dict:
    """Mean of each channel's samples over the window -> one telemetry point."""
    point = {}
    for channel, values in samples.items():
        if values:
            point[channel] = round(sum(values) / len(values), 3)
    return point


def enqueue(batch: dict):
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    # monotonic-ish filename from the batch's first timestamp
    name = f"{batch['points'][0]['time'].replace(':', '').replace('-', '')}.json"
    (QUEUE_DIR / name).write_text(json.dumps(batch))


def flush_queue():
    """Replay queued batches in order; stop on first failure (link still down)."""
    if not QUEUE_DIR.exists():
        return
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            _post(json.loads(f.read_text()))
            f.unlink()
        except Exception:
            return  # leave the rest queued; try again next tick


def _post(batch: dict):
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def send(point: dict):
    batch = {"boat_id": BOAT_ID, "points": [point]}
    try:
        _post(batch)
        flush_queue()           # opportunistically drain backlog
    except Exception:
        enqueue(batch)          # link down -> store and forward


def main():
    print(f"[uplink] CAN_IFACE={CAN_IFACE} signalk={SIGNALK_WS} -> {VPS_URL}")
    # TODO Phase 3: open the Signal K WS, map deltas -> channels, fill `window`.
    window = defaultdict(list)
    while True:
        time.sleep(AGG_SECONDS)
        point = aggregate(window)
        window.clear()
        if point:
            point["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            send(point)


if __name__ == "__main__":
    main()
