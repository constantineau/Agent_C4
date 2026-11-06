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
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets

try:
    from shared import clock_trust
except ImportError:                     # pragma: no cover - image without shared/
    clock_trust = None

_MS_TO_KN = 1.943844

CAN_IFACE = os.environ.get("CAN_IFACE", "vcan0")
SIGNALK_WS = os.environ.get(
    "SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all"
)
VPS_URL = os.environ.get("VPS_URL", "http://localhost:8101")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AGG_SECONDS = int(os.environ.get("AGG_SECONDS", "15"))
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "/var/lib/sr33/queue"))
# Sits inside QUEUE_DIR but out of its `*.json` glob, so quarantined files stay on the
# boat for inspection without ever being retried.
QUARANTINE_DIR = QUEUE_DIR / "quarantine"
# Batches collected before the wall clock was trusted (#184-9, #240): stamped with the monotonic
# clock and this boot's id instead of a date, and moved into the queue with a real date once the
# clock is trusted. Also out of the queue's glob, so nothing undated is ever posted.
UNDATED_DIR = QUEUE_DIR / "undated"
# 408 Request Timeout and 429 Too Many Requests say "try again later", not "this payload is
# bad": quarantining them parked good telemetry for ever (#184-7).
_RETRY_LATER = {408, 429}
# One sender at a time: two overlapping sends would each read the same queued file and post it
# twice (the file is only removed after its POST returns).
_SEND_LOCK = threading.Lock()
# Batches kept in RAM after a failed hand-off (disk full / I/O error) before the flusher stops
# cutting new ones: 240 x 15 s = an hour.
RETRY_MAX = int(os.environ.get("UPLINK_RETRY_MAX", "240"))
CLOCK = clock_trust.ClockTrust(QUEUE_DIR / "clock-trusted") if clock_trust else None


def clock_trusted():
    return CLOCK is None or CLOCK.trusted


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


def _mmsi_from_context(ctx):
    """Pull the numeric MMSI out of an AIS vessel context urn, else None.

    e.g. 'vessels.urn:mrn:imo:mmsi:366123456' -> 366123456. Own ship is a uuid context,
    so it (correctly) returns None and is never mistaken for a target."""
    if ctx and "mmsi:" in ctx:
        tail = ctx.split("mmsi:")[-1].strip()
        return int(tail) if tail.isdigit() else None
    return None


# Per-MMSI name cache so every flushed row carries the name even when this window only
# saw a position update (AIS names arrive rarely; the cloud takes latest-row-per-mmsi).
_ais_names = {}


def record_ais(ais_window, mmsi, path, value):
    """Accumulate the fields we need from another vessel's delta, in kn / deg true."""
    t = ais_window.setdefault(mmsi, {})
    if path in ("name", "") and isinstance(value, str) and value:
        t["name"] = value
        _ais_names[mmsi] = value
    elif path == "navigation.position" and isinstance(value, dict):
        if isinstance(value.get("latitude"), (int, float)):
            t["lat"] = float(value["latitude"])
        if isinstance(value.get("longitude"), (int, float)):
            t["lon"] = float(value["longitude"])
    elif path == "navigation.speedOverGround" and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        t["sog"] = round(float(value) * _MS_TO_KN, 2)
    elif path == "navigation.courseOverGroundTrue" and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        t["cog"] = round(math.degrees(float(value)) % 360, 1)


def parse_delta(msg, window, ais_window, state):
    try:
        data = json.loads(msg)
    except ValueError:
        return
    # The hello frame names the self context; remember it so we can tell own-ship deltas
    # from other vessels heard on AIS (subscribe=all delivers both).
    if "self" in data and "updates" not in data:
        state["self"] = data["self"]
        return
    ctx = data.get("context")
    mmsi = _mmsi_from_context(ctx)
    is_other = bool(ctx) and ctx != state.get("self") and mmsi is not None
    for upd in data.get("updates", []):
        source = upd.get("$source") or (upd.get("source") or {}).get("label") or "unknown"
        for v in upd.get("values", []):
            path = v.get("path")
            if path is None:
                continue
            if is_other:
                record_ais(ais_window, mmsi, path, v.get("value"))
            else:
                record(window, source, path, v.get("value"))
                if path == "navigation.datetime" and state.get("clock") is not None:
                    state["clock"].note_gnss(v.get("value"))


# --- cloud POST with store-and-forward ---------------------------------------
def _post(batch):
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest/raw", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def _write_atomic(d, name, batch):
    # Write to a temp name and rename into place: rename is atomic, so a power cut can
    # never leave a truncated `.json` in the queue. 17 zero-byte files got in this way
    # during the 2026-07-19 outage, and one of them then blocked the queue for six weeks.
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps(batch))
    os.replace(tmp, d / name)


def _enqueue(batch):
    stamp = batch["readings"][0]["time"].replace(":", "").replace("-", "")
    name, n = f"{stamp}.json", 0
    while (QUEUE_DIR / name).exists():     # a re-dated batch can land on a queued second
        n += 1
        name = f"{stamp}-{n}.json"
    _write_atomic(QUEUE_DIR, name, batch)


def hold(readings, mono=None):
    """Keep a batch collected before the clock was trusted, undated (#240: collection goes
    on, timestamped publishing waits). It is dated from the monotonic clock once trust comes."""
    mono = time.monotonic() if mono is None else mono
    batch = {"boat_id": BOAT_ID, "readings": readings, "mono": mono,
             "boot": clock_trust.boot_id() if clock_trust else ""}
    _write_atomic(UNDATED_DIR, f"{int(mono * 1e9):020d}.json", batch)
    print(f"[uplink] clock not trusted yet, held {len(readings)} readings undated", flush=True)


_other_boot_warned = set()


def _date_held():
    """Once the clock is trusted: give every batch held in this boot its real time (now minus
    the monotonic time since it was held) and move it into the queue, oldest first. A batch
    held in an EARLIER boot cannot be dated (the monotonic clock restarted); it is left where
    it is, untouched, and said once.

    Two steps, each atomic, so a crash or a failed step can never queue a batch twice: the held
    file is first rewritten in place as the dated batch, then MOVED (one rename) into the queue
    under a name fixed by its held name. A held file found already dated is just moved."""
    if not UNDATED_DIR.exists():
        return
    boot = clock_trust.boot_id() if clock_trust else ""
    for f in sorted(UNDATED_DIR.glob("*.json")):
        try:
            batch = json.loads(f.read_text())
            dated = batch["readings"][0]["time"]
            mono = None if dated else float(batch["mono"])
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            _quarantine(f, f"unreadable held batch ({exc})")
            continue
        if dated:
            _promote(f, dated)
            continue
        if not boot or batch.get("boot") != boot:
            if f.name not in _other_boot_warned:
                _other_boot_warned.add(f.name)
                print(f"[uplink] held batch {f.name} is from an earlier boot; it cannot be "
                      f"dated, left in {UNDATED_DIR}", flush=True)
            continue
        t = time.time() - max(0.0, time.monotonic() - mono)
        stamp = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in batch["readings"]:
            r["time"] = stamp
        _write_atomic(UNDATED_DIR, f.name,
                      {"boat_id": batch.get("boat_id", BOAT_ID), "readings": batch["readings"]})
        _promote(f, stamp)


def _promote(f, stamp):
    """Move a dated held file into the queue: one rename, same filesystem."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    os.replace(f, QUEUE_DIR / f"{stamp.replace(':', '').replace('-', '')}-h{f.stem}.json")


def _quarantine(f, why):
    """Set aside a spool file that can never be sent, so the queue behind it can drain."""
    try:
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(f, QUARANTINE_DIR / f.name)
    except OSError:
        return  # leave it; next pass will try again rather than lose it silently
    print(f"[uplink] quarantined {f.name}: {why}", flush=True)


def _flush_queue():
    """Replay queued batches oldest-first.

    Two failure modes have to be told apart, because treating them alike is what stalled
    the queue for six weeks: a *transient* failure (link down, server 5xx) means stop and
    keep everything for later, but a *permanent* one (file corrupt on disk, server
    rejects the payload as malformed) will fail identically on every future pass. A
    permanent failure has to be moved aside or it blocks every newer batch behind it
    forever.
    """
    if not QUEUE_DIR.exists():
        return
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            batch = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            _quarantine(f, f"unreadable ({exc})")
            continue
        try:
            _post(batch)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in _RETRY_LATER:
                _quarantine(f, f"rejected HTTP {exc.code}")
                continue
            return  # 408/429/5xx — try again later; keep the rest queued
        except Exception:
            return  # link still down — leave the rest queued
        f.unlink(missing_ok=True)


def send(readings):
    with _SEND_LOCK:
        _send(readings)


def drain_held():
    """Date and queue the held batches, then try the queue, with no live batch to send."""
    with _SEND_LOCK:
        _date_held()
        _flush_queue()


def _send(readings):
    try:
        _date_held()
    except Exception as exc:          # never let the held batches stop the live one
        print(f"[uplink] could not date held batches ({exc})", flush=True)
    batch = {"boat_id": BOAT_ID, "readings": readings}
    try:
        _post(batch)
        _flush_queue()
        srcs = len({r["source"] for r in readings})
        print(f"[uplink] sent {len(readings)} readings from {srcs} source(s)", flush=True)
    except Exception as exc:
        _enqueue(batch)
        print(f"[uplink] link down, queued {len(readings)} readings ({exc})", flush=True)


def _post_ais(targets):
    body = json.dumps({"boat_id": BOAT_ID, "targets": targets}).encode()
    req = urllib.request.Request(
        f"{VPS_URL}/ingest/ais", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
    )
    urllib.request.urlopen(req, timeout=20).read()


def send_ais(targets):
    # AIS is real-time collision context: a target's position is only useful fresh, so on a
    # link drop we DROP rather than queue (replaying stale positions later would be wrong).
    try:
        _post_ais(targets)
        print(f"[uplink] sent {len(targets)} AIS target(s)", flush=True)
    except Exception as exc:
        print(f"[uplink] AIS post failed, dropping {len(targets)} (real-time): {exc}",
              flush=True)


async def flusher(window, loop, retry=None):
    # A batch whose hand-off raised (disk full or an I/O error while holding or queueing it)
    # is kept here and tried again first on the next tick, so the task never dies and the batch
    # is not lost. Held batches keep their original monotonic stamp.
    retry = [] if retry is None else retry
    while True:
        await asyncio.sleep(AGG_SECONDS)
        while retry:
            fn, args = retry[0]
            try:
                await loop.run_in_executor(None, fn, *args)
            except Exception as exc:
                print(f"[uplink] still cannot hand off {len(retry)} batch(es) ({exc})",
                      flush=True)
                break
            retry.pop(0)
        if not window:
            # A quiet bus: still deliver batches held before the clock was trusted.
            if not retry and UNDATED_DIR.exists() and any(UNDATED_DIR.glob("*.json")) \
                    and clock_trusted():
                try:
                    await loop.run_in_executor(None, drain_held)
                except Exception as exc:
                    print(f"[uplink] could not drain held batches ({exc})", flush=True)
            continue
        if len(retry) >= RETRY_MAX:
            # The disk has refused every hand-off for a long while. Stop cutting new batches
            # (RAM would grow until the process is killed and every pending batch is lost);
            # the window keeps the latest value of every reading until a hand-off works.
            continue
        trusted = clock_trusted()
        now = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if trusted else None)
        readings = []
        for (source, path), (kind, val) in list(window.items()):
            r = {"time": now, "source": source, "path": path}
            r["value" if kind == "num" else "str_value"] = val
            readings.append(r)
        window.clear()
        job = (send, (readings,)) if trusted else (hold, (readings, time.monotonic()))
        if retry:
            retry.append(job)           # keep the order behind the ones still failing
            continue
        try:
            await loop.run_in_executor(None, job[0], *job[1])
        except Exception as exc:
            print(f"[uplink] could not hand off {len(readings)} readings ({exc}); "
                  f"will retry", flush=True)
            retry.append(job)


async def ais_flusher(ais_window, loop):
    while True:
        await asyncio.sleep(AGG_SECONDS)
        if not ais_window:
            continue
        if not clock_trusted():
            # AIS is real-time only and already dropped rather than queued on a link failure;
            # a target held until the clock is trusted would be stale by then, and one sent
            # with an untrusted date would be wrong. Same rule: not sent.
            ais_window.clear()
            continue
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        targets = []
        for mmsi, t in list(ais_window.items()):
            if t.get("lat") is None or t.get("lon") is None:
                continue  # no position yet — nothing the cloud can place
            targets.append({"time": now, "mmsi": mmsi,
                            "name": t.get("name") or _ais_names.get(mmsi),
                            "lat": t["lat"], "lon": t["lon"],
                            "sog": t.get("sog"), "cog": t.get("cog")})
        ais_window.clear()
        if targets:
            await loop.run_in_executor(None, send_ais, targets)


async def run():
    print(f"[uplink] CAN_IFACE={CAN_IFACE} signalk={SIGNALK_WS} -> {VPS_URL} (collect-all)",
          flush=True)
    window = {}
    ais_window = {}
    state = {"self": None, "clock": CLOCK}
    loop = asyncio.get_running_loop()
    asyncio.create_task(flusher(window, loop))
    asyncio.create_task(ais_flusher(ais_window, loop))
    while True:
        try:
            async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                print("[uplink] connected to Signal K", flush=True)
                async for msg in ws:
                    parse_delta(msg, window, ais_window, state)
        except Exception as exc:
            print(f"[uplink] Signal K WS error ({exc}); retrying in 3s", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
