#!/usr/bin/env python3
"""SR33 telemetry uplink (Pi -> VPS) — collect-everything paradigm.

Subscribes to the Signal K WebSocket delta stream and forwards EVERY (source, path)
reading to the VPS, including redundant sources (heading/heel/wind/position from multiple
devices). The uplink is deliberately dumb: it does not pick winners, average across
sources, or convert units — it preserves provenance (Signal K `$source`) and the raw SI
value so the agent can cross-check sources and judge reliability itself.

Per aggregation window it sends the latest value seen for each (source, path). Object
values (position {lat,lon}, attitude {roll,pitch,yaw}) are flattened into dotted numeric
paths. A disk-backed queue replays batches if the cloud link (Starlink) drops.

Identical on bench and boat; the only difference is CAN_IFACE (vcan0 vs can0), upstream of
Signal K.
"""
import asyncio
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets

CAN_IFACE = os.environ.get("CAN_IFACE", "vcan0")
SIGNALK_WS = os.environ.get(
    "SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all"
)
VPS_URL = os.environ.get("VPS_URL", "http://localhost:8101")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AGG_SECONDS = int(os.environ.get("AGG_SECONDS", "15"))
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "/var/lib/sr33/queue"))


def record(window, source, path, value):
    """Store the latest reading for a (source, path) in the current window."""
    if isinstance(value, bool):
        window[(source, path)] = ("str", str(value).lower())
    elif isinstance(value, (int, float)):
        window[(source, path)] = ("num", float(value))
    elif isinstance(value, dict):
        # flatten objects (position, attitude, …) into dotted numeric sub-paths
        for k, sub in value.items():
            if isinstance(sub, (int, float)) and not isinstance(sub, bool):
                window[(source, f"{path}.{k}")] = ("num", float(sub))
    elif isinstance(value, str) and value:
        window[(source, path)] = ("str", value)


def parse_delta(msg, window):
    try:
        data = json.loads(msg)
    except ValueError:
        return
    for upd in data.get("updates", []):
        source = upd.get("$source") or (upd.get("source") or {}).get("label") or "unknown"
        for v in upd.get("values", []):
            path = v.get("path")
            if path:
                record(window, source, path, v.get("value"))


# --- cloud POST with store-and-forward ---------------------------------------
def _post(batch):
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest/raw", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def _enqueue(batch):
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = batch["readings"][0]["time"].replace(":", "").replace("-", "")
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


def send(readings):
    batch = {"boat_id": BOAT_ID, "readings": readings}
    try:
        _post(batch)
        _flush_queue()
        srcs = len({r["source"] for r in readings})
        print(f"[uplink] sent {len(readings)} readings from {srcs} source(s)", flush=True)
    except Exception as exc:
        _enqueue(batch)
        print(f"[uplink] link down, queued {len(readings)} readings ({exc})", flush=True)


async def flusher(window, loop):
    while True:
        await asyncio.sleep(AGG_SECONDS)
        if not window:
            continue
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        readings = []
        for (source, path), (kind, val) in list(window.items()):
            r = {"time": now, "source": source, "path": path}
            r["value" if kind == "num" else "str_value"] = val
            readings.append(r)
        window.clear()
        await loop.run_in_executor(None, send, readings)


async def run():
    print(f"[uplink] CAN_IFACE={CAN_IFACE} signalk={SIGNALK_WS} -> {VPS_URL} (collect-all)",
          flush=True)
    window = {}
    loop = asyncio.get_running_loop()
    asyncio.create_task(flusher(window, loop))
    while True:
        try:
            async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[uplink] connected to Signal K", flush=True)
                async for msg in ws:
                    parse_delta(msg, window)
        except Exception as exc:
            print(f"[uplink] Signal K WS error ({exc}); retrying in 3s", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
