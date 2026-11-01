"""n2kout — the Pi's NMEA-2000 route BROADCASTER service (:8210 on localhost, host network).

The one Tier-1 service that WRITES to the bus. Two jobs:

1. Be a proper bus device, always (the bus thread, `_BusThread`): claim an address at startup
   and defend it (or move) on a conflict, answer ISO requests for our address claim, product
   information and PGN lists (NAK anything else asked of us directly), send a heartbeat every
   60 s, and keep track of the other devices it hears. The Garmin 943 ignores a source that
   doesn't identify itself (found live 2026-07-16).
2. Broadcast a route, only when the crew asks (`_Broadcaster`): the iPad's "show on GPS" →
   engine `POST /gps/show` → `POST /route` here. Every second, the active waypoint, XTE,
   distance, bearings and ETA are recomputed from the boat's latest position (`navcalc`), read
   from the engine's `GET /gps/fix`. Without a fresh fix nothing live is sent (no 129283/129284)
   — the route list (129285) keeps going, because it is the plan's geometry, not a claim about
   where the boat is. The broadcast ends on `POST /stop`, on expiry (ROUTE_TTL_S, default 6 h)
   and when the engine reports another course than the one the route was sent for; each end
   sends "navigation terminated" (129283) and an all-n/a 129284.

No steering, no autopilot coupling: pixels on the crew's own chartplotter.
"""
from __future__ import annotations

import json
import math
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import n2k
import navcalc

CAN_IFACE = os.environ.get("CAN_IFACE", "can0")
ROUTE_EVERY_S = float(os.environ.get("N2KOUT_ROUTE_EVERY_S", "5"))
NAV_EVERY_S = float(os.environ.get("N2KOUT_NAV_EVERY_S", "1"))
# the engine's own-ship fix (source-policy chosen, with its receive time) + the course token
FIX_URL = os.environ.get("N2KOUT_FIX_URL", "http://127.0.0.1:8200/gps/fix")
FIX_TIMEOUT_S = float(os.environ.get("N2KOUT_FIX_TIMEOUT_S", "0.5"))
# older than this → no live nav sent. 10 s: GPS writes at 1-10 Hz; a few missed polls are fine
FIX_MAX_AGE_S = float(os.environ.get("N2KOUT_FIX_MAX_AGE_S", "10"))
ROUTE_TTL_S = float(os.environ.get("N2KOUT_ROUTE_TTL_S", str(6 * 3600)))
HEARTBEAT_S = 60.0
MAX_WPTS = 250                    # Garmin's per-route limit (to confirm dockside)
TERMINATE_REPEATS = 3
PEER_WINDOW_S = 300.0             # "other devices heard" = in the last 5 min
ERROR_WINDOW_S = 60.0             # a send error this recent makes the link "errors"

BUS = n2k.N2kSender(CAN_IFACE)


# --- POST /route: strict input ------------------------------------------------------------

def _finite(v):
    if not math.isfinite(v):
        raise ValueError("must be a finite number")
    return v


class Waypoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=32)
    lat: float = Field(strict=True, ge=-90, le=90)
    lon: float = Field(strict=True, ge=-180, le=180)
    t: float | None = Field(default=None, strict=True)     # plan time (kept, not sent)

    _fin = field_validator("lat", "lon")(_finite)

    @field_validator("t")
    @classmethod
    def _t_finite(cls, v):
        return None if v is None else _finite(v)

    @field_validator("name")
    @classmethod
    def _ascii(cls, v):
        if v is not None and not v.isascii():
            raise ValueError("name must be ASCII")
        return v


class RouteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="C4 gameplan", min_length=1, max_length=32)
    waypoints: list[Waypoint] = Field(min_length=2, max_length=MAX_WPTS)
    dest_index: int = Field(default=1, strict=True, ge=0)
    # the engine's course token at send time: a different one on the fix feed ends the route
    course: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _ascii(cls, v):
        if not v.isascii():
            raise ValueError("name must be ASCII")
        return v

    @model_validator(mode="after")
    def _dest_in_range(self):
        if self.dest_index >= len(self.waypoints):
            raise ValueError("dest_index is past the last waypoint")
        return self


# --- the engine's fix feed ------------------------------------------------------------------

def fetch_fix():
    """{"fix": {lat, lon, t} | None, "course": token} from the engine, or None (unreachable)."""
    try:
        with urllib.request.urlopen(FIX_URL, timeout=FIX_TIMEOUT_S) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def fresh_fix(feed, now):
    """(lat, lon, t) when the feed holds a sane fix no older than FIX_MAX_AGE_S, else None."""
    fix = (feed or {}).get("fix") if isinstance(feed, dict) else None
    if not isinstance(fix, dict):
        return None
    try:
        lat, lon, t = float(fix["lat"]), float(fix["lon"]), float(fix["t"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (lat, lon, t)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if now - t > FIX_MAX_AGE_S or t - now > 5:        # stale, or from the future (bad clock)
        return None
    return lat, lon, t


# --- the broadcaster --------------------------------------------------------------------------

class _Broadcaster:
    def __init__(self, sender=BUS, fix_source=fetch_fix, clock=time.time):
        self.sender, self.fix_source, self.clock = sender, fix_source, clock
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = threading.Event()
        self.end_reason = None
        self.state = {"broadcasting": False, "iface": CAN_IFACE, "route": None,
                      "n_waypoints": 0, "started_at": None, "expires_at": None,
                      "nav_live": False, "nav": None, "ended": None, "last_error": None}

    def start(self, body: RouteBody):
        route_msgs = n2k.chunk_route(body.name, [w.model_dump() for w in body.waypoints])
        with self.lock:
            self._stop_locked("replaced")
            self.stop_evt = threading.Event()
            self.end_reason = None
            now = self.clock()
            self.state.update({"broadcasting": True, "route": body.name,
                               "n_waypoints": len(body.waypoints), "started_at": round(now),
                               "expires_at": round(now + ROUTE_TTL_S), "nav_live": False,
                               "nav": None, "ended": None, "last_error": None})
            self.thread = threading.Thread(target=self._run, args=(body, route_msgs,
                                                                   self.stop_evt, now),
                                           daemon=True, name="n2k-broadcast")
            self.thread.start()

    def stop(self, reason="cleared"):
        with self.lock:
            self._stop_locked(reason)

    def _stop_locked(self, reason):
        if self.thread and self.thread.is_alive():
            self.end_reason = reason
            self.stop_evt.set()
            self.thread.join(timeout=5)
        self.thread = None
        self.state["broadcasting"] = False

    def _send(self, msg):
        pgn, prio, data = msg
        return self.sender.send(pgn, prio, data)

    def _terminate(self):
        for k in range(TERMINATE_REPEATS):
            self._send(n2k.encode_129283(None, terminated=True))
            self._send(n2k.encode_129284_null())
            if k < TERMINATE_REPEATS - 1:
                time.sleep(0.2)

    def cycle(self, body, tracker, sid):
        """One broadcast second: → None to keep going, or the reason it ended."""
        now = self.clock()
        if now >= self.state["expires_at"]:
            return "expired"
        feed = self.fix_source()
        if (body.course is not None and isinstance(feed, dict) and "course" in feed
                and feed["course"] != body.course):
            return "course changed"
        fix = fresh_fix(feed, now)
        if fix is None:
            self.state.update({"nav_live": False, "nav": None})
            return None
        nav = tracker.update(*fix)
        self._send(n2k.encode_129283(nav["xte_m"], sid=sid))
        self._send(n2k.encode_129284(
            nav["dest"][0], nav["dest"][1], dest_wp=nav["dest_index"],
            origin_wp=nav["origin_index"], eta_epoch=nav["eta_epoch"], dtw_m=nav["dtw_m"],
            brg_deg=nav["brg_deg"], brg_origin_deg=nav["brg_leg_deg"], sid=sid,
            perp_crossed=nav["perp_crossed"], arrived=nav["arrived"],
            closing_mps=nav["closing_mps"]))
        self.state.update({"nav_live": True, "nav": {
            "dest_index": nav["dest_index"], "dest": body.waypoints[nav["dest_index"]].name,
            "dtw_nm": round(nav["dtw_m"] / navcalc.NM_M, 3),
            "brg_deg": round(nav["brg_deg"], 1),
            "xte_m": None if nav["xte_m"] is None else round(nav["xte_m"], 1),
            "eta": None if nav["eta_epoch"] is None else round(nav["eta_epoch"]),
            "arrived": nav["arrived"], "fix_age_s": round(now - fix[2], 1)}})
        return None

    def _run(self, body, route_msgs, stop_evt, started):
        tracker = navcalc.LegTracker([w.model_dump() for w in body.waypoints], body.dest_index)
        last_route, sid, reason = 0.0, 0, None
        try:
            while not stop_evt.is_set():
                reason = self.cycle(body, tracker, sid)
                if reason:
                    break
                sid = (sid + 1) % 253
                now = self.clock()
                if now - last_route >= ROUTE_EVERY_S:
                    last_route = now
                    for msg in route_msgs:
                        self._send(msg)
                stop_evt.wait(NAV_EVERY_S)
        except Exception as e:  # noqa: BLE001 — a bug must still end with "terminated"
            reason = "error"
            self.state["last_error"] = f"{type(e).__name__}: {e}"
        finally:
            self._terminate()
            self.state.update({"broadcasting": False, "nav_live": False, "nav": None,
                               "ended": reason or self.end_reason or "cleared"})


# --- the bus thread: identity, requests, heartbeat, diagnostics -----------------------------

PF_REQUEST, PF_CLAIM = 0xEA, 0xEE


def rx_filters():
    """Kernel-side filters: ISO requests (PF 0xEA), address claims (PF 0xEE), heartbeats
    (126993) and product info (126996) — the traffic every certified device makes, so the
    peer list fills within a minute without waking Python for every instrument frame."""
    pf = n2k.CAN_EFF_FLAG | (0xFF << 16)
    full = n2k.CAN_EFF_FLAG | (0x3FFFF << 8)
    return b"".join(struct.pack("<II", i, m) for i, m in (
        (n2k.CAN_EFF_FLAG | (PF_REQUEST << 16), pf),
        (n2k.CAN_EFF_FLAG | (PF_CLAIM << 16), pf),
        (n2k.CAN_EFF_FLAG | (126993 << 8), full),
        (n2k.CAN_EFF_FLAG | (126996 << 8), full)))


def open_rx(iface):
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, rx_filters())
        s.bind((iface,))
        s.settimeout(1.0)
    except OSError:
        s.close()
        raise
    return s


class _BusThread(threading.Thread):
    """Answer ISO Requests (59904), claim/defend our address, heartbeat — the one RX this
    service does. Restarts itself after any socket error (backoff 1 → 30 s)."""

    def __init__(self, iface: str, sender=BUS, open_rx=open_rx, name=n2k.NAME, clock=time.time,
                 link_state=lambda: None):
        super().__init__(daemon=True, name="n2k-bus")
        self.iface, self.sender, self.open_rx, self.name_, self.clock = (
            iface, sender, open_rx, name, clock)
        self.stop_evt = threading.Event()
        self.state = {"running": False, "answered": {}, "naks": 0, "error": None,
                      "restarts": 0, "conflicts": 0, "address": sender.sa}
        self.peers = {}                   # sa -> {"last": epoch, "name": NAME or None}
        self.requests_to_us = 0
        self._hb_seq = 0
        self._last_hb = 0.0
        self.link_state = link_state      # → the kernel's CAN state, for the heartbeat

    # -- frame handling (pure: tests feed frames straight in) --
    def claim(self):
        self.sender.send(*n2k.encode_60928(self.name_))

    def heartbeat(self):
        self.sender.send(*n2k.encode_126993(
            HEARTBEAT_S, self._hb_seq, n2k.CONTROLLER_STATE.get(self.link_state(), 3)))
        self._hb_seq = (self._hb_seq + 1) % 253
        self._last_hb = self.clock()

    def _answer(self, req, requester, directed):
        sa = self.sender.sa
        if req == 60928:
            self.claim()
        elif req == 126996:
            self.sender.send(*n2k.encode_126996())
        elif req == 126464:
            self.sender.send(*n2k.encode_126464(0, n2k.TX_PGNS))
            self.sender.send(*n2k.encode_126464(1, n2k.RX_PGNS))
        elif req == 126993:
            self.heartbeat()
        elif directed and requester != sa:
            pgn, prio, data = n2k.encode_59392_nak(req)
            self.sender.send(pgn, prio, data, dest=requester)
            self.state["naks"] += 1
            return
        else:
            return
        k = str(req)
        self.state["answered"][k] = self.state["answered"].get(k, 0) + 1

    def _move(self):
        """We lost the contest for our address: take the first free one in 128-247 (the
        self-configurable range) and claim it; none free → "cannot claim" from the null
        address 254, after which the sender refuses to transmit."""
        new = next((a for a in range(128, 248) if a not in self.peers), 254)
        if new == 254:
            self.sender.send(*n2k.encode_60928(self.name_), src=254)
        self.sender.sa = new
        self.state["address"] = new
        if new != 254:
            self.claim()

    def handle(self, cid: int, data: bytes):
        cid &= 0x1FFFFFFF
        sa = cid & 0xFF
        pf = (cid >> 16) & 0xFF
        ps = (cid >> 8) & 0xFF
        now = self.clock()
        me = self.sender.sa
        if sa not in (me, 254):
            self.peers.setdefault(sa, {"name": None})["last"] = now
        if pf == PF_REQUEST and len(data) >= 3:
            if ps == me:
                self.requests_to_us += 1
            if ps in (me, 0xFF):
                self._answer(int.from_bytes(data[:3], "little"), sa, directed=(ps == me))
        elif pf == PF_CLAIM and len(data) >= 8:
            theirs = n2k.decode_name(data)
            if sa != 254:
                self.peers.setdefault(sa, {})["name"] = theirs
                self.peers[sa]["last"] = now
            if sa == me and theirs != self.name_:
                self.state["conflicts"] += 1
                if self.name_ < theirs:                # lower NAME wins the address
                    self.claim()                       # we win: defend
                else:
                    self._move()                       # we lose: move

    # -- the loop --
    def _loop_once(self):
        s = self.open_rx(self.iface)
        self._backoff = self.backoff_s                 # opened: the next failure retries fast
        try:
            self.state.update({"running": True, "error": None})
            self.claim()
            self.stop_evt.wait(0.25)       # ISO 11783-5: 250 ms after a claim before other traffic
            self.heartbeat()
            while not self.stop_evt.is_set():
                if self.clock() - self._last_hb >= HEARTBEAT_S:
                    self.heartbeat()
                try:
                    data, _anc, flags, _addr = s.recvmsg(16)
                except socket.timeout:
                    continue
                if flags & socket.MSG_DONTROUTE:       # our own frame, looped back by the host
                    continue
                if len(data) < 8:
                    continue
                cid, dlc = struct.unpack("<IB3x", data[:8])
                self.handle(cid, data[8:8 + min(dlc, 8)])
        finally:
            s.close()

    backoff_s = 1.0

    def run(self):
        self._backoff = self.backoff_s
        while not self.stop_evt.is_set():
            try:
                self._loop_once()
            except OSError as e:
                self.state.update({"running": False, "error": f"{type(e).__name__}: {e}"})
                self.state["restarts"] += 1
                self.stop_evt.wait(self._backoff)
                self._backoff = min(30.0, self._backoff * 2)
        self.state["running"] = False

    def peers_recent(self):
        now = self.clock()
        return sorted(sa for sa, p in self.peers.items()
                      if now - p.get("last", 0) <= PEER_WINDOW_S)


# --- CAN interface state (kernel) ---------------------------------------------------------

_LINK_CACHE = {"at": 0.0, "val": None}


def can_link(iface=CAN_IFACE, run=subprocess.run, ttl=5.0):
    """{"present", "up", "state", "restart_ms", "bus_off", "restarts", "error_passive"} from
    `ip -details -statistics -json link show <iface>`; cached `ttl` s. state is the kernel's
    ERROR-ACTIVE / ERROR-WARNING / ERROR-PASSIVE / BUS-OFF / STOPPED; None when unknown."""
    now = time.monotonic()
    if _LINK_CACHE["val"] is not None and now - _LINK_CACHE["at"] < ttl:
        return _LINK_CACHE["val"]
    out = {"present": False, "up": False, "state": None, "restart_ms": None,
           "bus_off": None, "restarts": None, "error_passive": None}
    try:
        r = run(["ip", "-details", "-statistics", "-json", "link", "show", iface],
                capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            link = (json.loads(r.stdout) or [{}])[0]
            info = link.get("linkinfo") or {}
            data, xs = info.get("info_data") or {}, info.get("info_xstats") or {}
            out.update({"present": True,
                        "up": "UP" in (link.get("flags") or []),
                        "state": data.get("state"), "restart_ms": data.get("restart_ms"),
                        "bus_off": xs.get("bus_off"), "restarts": xs.get("restarts"),
                        "error_passive": xs.get("error_passive")})
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError):
        pass
    _LINK_CACHE.update({"at": now, "val": out})
    return out


def link_word(link, sender, bus_alive, now):
    """The iPad's one line: "sending" / "no bus" / "errors"."""
    if not link.get("present") or not link.get("up") or link.get("state") in ("BUS-OFF",
                                                                              "STOPPED"):
        return "no bus"
    if not bus_alive and sender.last_ok_at is None:
        return "no bus"
    recent_err = sender.last_error_at is not None and now - sender.last_error_at <= ERROR_WINDOW_S
    if recent_err or link.get("state") == "ERROR-PASSIVE" or not bus_alive:
        return "errors"
    return "sending"


def diagnostics():
    now = time.time()
    link = can_link()
    return {"link": link_word(link, BUS, RESPONDER.state["running"], now),
            "address": BUS.sa, "frames_sent": BUS.frames_sent, "send_errors": BUS.send_errors,
            "last_send_error": BUS.last_error,
            "can": link,
            "peers": RESPONDER.peers_recent(),
            "requests_to_us": RESPONDER.requests_to_us}


BC = _Broadcaster()
RESPONDER = _BusThread(CAN_IFACE, link_state=lambda: can_link().get("state"))


@asynccontextmanager
async def lifespan(_app):
    RESPONDER.start()
    yield
    BC.stop("shutdown")
    RESPONDER.stop_evt.set()


app = FastAPI(title="Agent_C4 n2kout", version="0.2.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _invalid(_request, exc):
    """422 with where and why — never the input echoed back: a NaN in it can't be rendered as
    JSON, and FastAPI's default handler would turn the rejection into a 500."""
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": [str(x) for x in e.get("loc", ())], "msg": str(e.get("msg"))}
        for e in exc.errors()]})


def _status():
    return {**BC.state, "claim_responder": dict(RESPONDER.state), "bus": diagnostics()}


@app.get("/health")
def health():
    alive = RESPONDER.is_alive() and RESPONDER.state["running"]
    return {"status": "ok" if alive else "degraded", "service": "n2kout", **_status()}


@app.get("/status")
def status():
    return _status()


@app.post("/route")
def route(body: RouteBody):
    try:
        BC.start(body)                    # encodes every chunk BEFORE the old route stops
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"route can't be encoded: {e}") from e
    time.sleep(0.3)                       # let the thread hit the bus once
    return dict(BC.state)


@app.post("/stop")
def stop():
    BC.stop("cleared")
    return dict(BC.state)
