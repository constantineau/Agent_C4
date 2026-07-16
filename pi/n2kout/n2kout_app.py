"""n2kout — the Pi's NMEA-2000 route BROADCASTER service (:8210, host network, CAN_IFACE).

The one Tier-1 service that WRITES to the bus, and only when the crew asks: the iPad's
"show on GPS" button → engine `POST /gps/show` (assembles the waypoints from the frozen
bundle) → this service broadcasts the route to the Garmin 943 (129285 route list chunks
every ~5 s; 129283 XTE + 129284 nav data at ~1 Hz; one ISO address claim up front).
`POST /stop` goes silent immediately. Passive by default — no broadcast until asked, so
the service is safe to run with the bus unplugged (bench vcan0 / pre-cable boat alike).

No reads, no steering, no autopilot coupling: pixels on the crew's own chartplotter.
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

from fastapi import FastAPI
from pydantic import BaseModel

import n2k

CAN_IFACE = os.environ.get("CAN_IFACE", "can0")
ROUTE_EVERY_S = float(os.environ.get("N2KOUT_ROUTE_EVERY_S", "5"))
NAV_EVERY_S = float(os.environ.get("N2KOUT_NAV_EVERY_S", "1"))

app = FastAPI(title="Agent_C4 n2kout", version="0.1.0")


class RouteBody(BaseModel):
    name: str = "C4 gameplan"
    waypoints: list[dict]                 # [{name, lat, lon, t?}] — t = plan ETA epoch
    dest_index: int = 1                   # the leg's destination waypoint (0-based)


class _Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = threading.Event()
        self.state = {"broadcasting": False, "iface": CAN_IFACE, "route": None,
                      "n_waypoints": 0, "frames_sent": 0, "started_at": None, "last_error": None}

    def start(self, body: RouteBody):
        with self.lock:
            self._stop_locked()
            self.stop_evt = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(body, self.stop_evt),
                                           daemon=True)
            self.state.update({"broadcasting": True, "route": body.name,
                               "n_waypoints": len(body.waypoints), "frames_sent": 0,
                               "started_at": round(time.time()), "last_error": None})
            self.thread.start()

    def stop(self):
        with self.lock:
            self._stop_locked()

    def _stop_locked(self):
        if self.thread and self.thread.is_alive():
            self.stop_evt.set()
            self.thread.join(timeout=3)
        self.thread = None
        self.state.update({"broadcasting": False})

    def _run(self, body: RouteBody, stop_evt: threading.Event):
        sender = n2k.N2kSender(CAN_IFACE)
        try:
            sender.open()
        except OSError as e:                       # no CAN interface (cable/module absent)
            self.state.update({"broadcasting": False,
                               "last_error": f"CAN open failed on {CAN_IFACE}: {e}"})
            return
        try:
            wpts = [w for w in body.waypoints
                    if w.get("lat") is not None and w.get("lon") is not None]
            di = max(0, min(len(wpts) - 1, body.dest_index))
            dest = wpts[di] if wpts else {}
            route_msgs = n2k.chunk_route(body.name, wpts)
            pgn, prio, data = n2k.encode_60928()
            self.state["frames_sent"] += sender.send(pgn, prio, data)
            last_route = 0.0
            while not stop_evt.is_set():
                now = time.time()
                pgn, prio, data = n2k.encode_129283(0.0)
                self.state["frames_sent"] += sender.send(pgn, prio, data)
                pgn, prio, data = n2k.encode_129284(
                    dest.get("lat"), dest.get("lon"), dest_wp=di,
                    origin_wp=max(0, di - 1), eta_epoch=dest.get("t"))
                self.state["frames_sent"] += sender.send(pgn, prio, data)
                if now - last_route >= ROUTE_EVERY_S:
                    last_route = now
                    for (pgn, prio, data) in route_msgs:
                        self.state["frames_sent"] += sender.send(pgn, prio, data)
                stop_evt.wait(NAV_EVERY_S)
        except Exception as e:                     # any bus trouble → stop cleanly, say why
            self.state["last_error"] = f"{type(e).__name__}: {e}"
        finally:
            sender.close()
            self.state["broadcasting"] = False


class _ClaimResponder(threading.Thread):
    """Answer ISO Requests (59904) for our Address Claim (60928) — the one RX this service does.

    Found live on the real bus (2026-07-16): the Garmin 943 enumerates any source it sees
    traffic from with a directed `EA` request (payload 00 EE 00 = "send your 60928") and it
    KEEPS asking — a source that never identifies itself is not a valid bus device, and the
    plotter discards its PGNs. A one-shot claim at broadcast start isn't enough; the request
    can come at any time (plotter boots later, device-list refresh). So: a standing listener,
    filtered to PF 0xEA only, that replies with the claim when asked (directed to us, or the
    global enumeration). Still no steering, no other reads — one 8-byte identity frame on
    request. Known limitation (documented, fixed bus): we don't contend for the address if
    another device ever claims SA 35."""

    def __init__(self, iface: str, sa: int = n2k.DEFAULT_SA):
        super().__init__(daemon=True, name="n2k-claim-responder")
        self.iface, self.sa = iface, sa
        self.state = {"running": False, "claims_answered": 0, "error": None}

    def run(self):
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            # kernel-side filter: only extended-id frames with PF 0xEA (ISO Request) wake us
            flt = struct.pack("<II", n2k.CAN_EFF_FLAG | (0xEA << 16),
                              n2k.CAN_EFF_FLAG | (0xFF << 16))
            s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
            s.bind((self.iface,))
        except OSError as e:                    # no CAN interface — idle, same as the broadcaster
            self.state["error"] = f"claim responder disabled: {e}"
            return
        pgn, prio, data = n2k.encode_60928(sa=self.sa)
        claim = struct.pack("<IB3x8s", n2k.can_id(pgn, self.sa, prio) | n2k.CAN_EFF_FLAG,
                            len(data), data.ljust(8, b"\xff"))
        self.state["running"] = True
        while True:
            try:
                frame = s.recv(16)
                cid, dlc = struct.unpack("<IB3x", frame[:8])
                dest = (cid >> 8) & 0xFF
                if dest in (self.sa, 0xFF) and frame[8:8 + dlc][:3] == b"\x00\xee\x00":
                    s.send(claim)
                    self.state["claims_answered"] += 1
            except OSError as e:
                self.state.update({"running": False, "error": str(e)})
                return


BC = _Broadcaster()
RESPONDER = _ClaimResponder(CAN_IFACE)
RESPONDER.start()


@app.get("/health")
def health():
    return {"status": "ok", "service": "n2kout", **BC.state,
            "claim_responder": dict(RESPONDER.state)}


@app.get("/status")
def status():
    return {**BC.state, "claim_responder": dict(RESPONDER.state)}


@app.post("/route")
def route(body: RouteBody):
    if not body.waypoints or len([w for w in body.waypoints if w.get("lat") is not None]) < 2:
        return {"broadcasting": False, "detail": "need at least 2 waypoints with positions"}
    BC.start(body)
    time.sleep(0.3)                                # let the thread hit the bus (or fail) once
    return dict(BC.state)


@app.post("/stop")
def stop():
    BC.stop()
    return dict(BC.state)
