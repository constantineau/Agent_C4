"""NMEA 2000 route TRANSMITTER — put the gameplan on the Garmin 943's chart, live, from a button.

The GPSMAP 943 officially RECEIVES 129284 (Navigation Data) and 129285 (Navigation - Route/WP
information) — the external-navigator pattern PC nav packages (Expedition et al.) use to push
their active route onto an MFD. This module is the boat's own computer talking to the boat's own
display over its own bus: RRS-41 clean, same legal footing as the rest of Tier 1.

Pure stdlib: Linux SocketCAN (AF_CAN raw) + hand-packed PGNs against the canboat field
definitions (canboat.json, fetched 2026-07-10):

  60928  ISO Address Claim          — our NAME; claimed at startup, defended or moved on conflict
  59392  ISO Acknowledgement       — NAK for a directed request we can't answer
  126464 PGN List                  — what we transmit / receive (asked for during enumeration)
  126993 Heartbeat                 — every 60 s, carries our CAN controller state
  126996 Product Information       — model "SR33 C4 Navigator" (asked for during enumeration)
  129283 Cross Track Error         — single frame; LIVE signed XTE; "terminated" on clear
  129285 Navigation Route/WP info  — fast-packet; the route: names + positions. A fast packet
                                      caps at 223 bytes, so long routes go out CHUNKED via the
                                      startRps/nItems fields (~10 waypoints per message)
  129284 Navigation Data           — fast-packet; the active leg, live from own position:
                                      destination waypoint, DTW, bearings, ETA; unknown fields
                                      ride as NMEA "not available"

Conventions honored: little-endian fields; reserved bits all-1s; unsigned n/a = all-1s; signed
n/a = max positive; STRING_LAU = [total_len][1=ASCII][chars]. Nothing here reads the bus.

DOCKSIDE VERIFICATION REQUIRED: the encodings round-trip through canboatjs on the bench (vcan0),
but how the 943 *renders* an external route (line on the chart vs data fields only) must be
eyeballed once the N2K cable is plugged. Until then the service simply idles.
"""
from __future__ import annotations

import math
import socket
import struct
import threading
import time

CAN_EFF_FLAG = 0x80000000

NA_U16 = 0xFFFF
NA_U32 = 0xFFFFFFFF
NA_I16 = 0x7FFF
NA_I32 = 0x7FFFFFFF

DEFAULT_SA = 35          # an unclaimed-by-anything source address; the claim announces it


def _lau(s: str) -> bytes:
    """STRING_LAU: length byte (total incl. the 2 header bytes) + 1 (ASCII) + chars."""
    b = str(s or "")[:16].encode("ascii", "replace")
    return bytes([len(b) + 2, 1]) + b


def _i32_1e7(deg) -> int:
    return NA_I32 if deg is None else max(-(2**31) + 1, min(2**31 - 2, round(float(deg) * 1e7)))


def can_id(pgn: int, sa: int, prio: int, dest: int = 0xFF) -> int:
    """29-bit extended id: priority (3) | EDP (1) | DP (1) | PF (8) | PS (8) | SA (8).
    PDU2 (PF >= 240): PS is the PGN's group extension, broadcast. PDU1 (PF < 240): PS carries
    the destination. EDP + DP (PGN bits 16-17) ride in BOTH forms — 126464 (0x1EE00) is PDU1
    with DP=1; dropping DP turns it into 60928 (0x0EE00), an address claim."""
    pgn &= 0x3FFFF
    dp = (pgn >> 16) & 0x3                     # EDP, DP
    pf = (pgn >> 8) & 0xFF
    ps = pgn & 0xFF if pf >= 240 else dest & 0xFF
    return ((prio & 0x7) << 26) | (dp << 24) | (pf << 16) | (ps << 8) | (sa & 0xFF)


def pgn_of(cid: int) -> int:
    """The PGN a 29-bit id carries (PDU1: the destination byte is not part of it)."""
    cid &= 0x1FFFFFFF
    dp = (cid >> 24) & 0x3
    pf = (cid >> 16) & 0xFF
    ps = (cid >> 8) & 0xFF
    return (dp << 16) | (pf << 8) | (ps if pf >= 240 else 0)


# Manufacturer 2046 is the code open-source / experimental N2K devices use (not a registered
# maker). Class 60 = Navigation; function 190 = "Navigation Management" (canboat
# DEVICE_FUNCTION[60]) — the closest code to "route/nav source". (Until 2026-11 this sent 130,
# which under class 60 is "Bottom Depth".) Industry group 4 = marine.
DEV_CLASS = 60
DEV_FUNCTION = 190


def make_name(unique: int = 100, mfg: int = 2046, function: int = DEV_FUNCTION,
              dev_class: int = DEV_CLASS, industry: int = 4, instance: int = 0,
              system: int = 0, aac: bool = True) -> int:
    """The 64-bit ISO NAME (canboat 60928 field order, LSB first): unique number (21) ·
    manufacturer (11) · device instance (8) · function (8) · spare (1) · class (7) · system
    instance (4) · industry group (3) · arbitrary-address-capable (1). Lower NAME = higher
    priority in an address-claim contest."""
    return ((unique & 0x1FFFFF) | ((mfg & 0x7FF) << 21) | ((instance & 0xFF) << 32)
            | ((function & 0xFF) << 40) | ((dev_class & 0x7F) << 49) | ((system & 0xF) << 56)
            | ((industry & 0x7) << 60) | ((1 if aac else 0) << 63))


NAME = make_name()


def encode_60928(name: int = NAME, sa: int = DEFAULT_SA):
    """ISO Address Claim: our 8-byte NAME, sent to global from the address we claim (`sa` is
    only the caller's bookkeeping — the source address rides in the CAN id)."""
    return 60928, 6, struct.pack("<Q", name)


def decode_name(data: bytes) -> int:
    return struct.unpack("<Q", bytes(data[:8]).ljust(8, b"\xff"))[0]


def encode_59392_nak(pgn: int, group_function: int = 0xFF):
    """ISO Acknowledgement, control 1 = NAK: "I don't send that PGN". Sent to the requester of a
    directed 59904 we can't answer, so the plotter's enumeration doesn't wait on us."""
    data = bytes([1, group_function & 0xFF]) + b"\xff\xff\xff" + pgn.to_bytes(3, "little")
    return 59392, 6, data


# What this service sends and listens for — the 126464 PGN lists. Keep in step with the code.
TX_PGNS = (59392, 60928, 126464, 126993, 126996, 129283, 129284, 129285)
RX_PGNS = (59904, 60928)


def encode_126464(function: int, pgns):
    """PGN List (fast packet): function 0 = PGNs we transmit, 1 = PGNs we receive."""
    data = bytes([function & 0xFF]) + b"".join(int(p).to_bytes(3, "little") for p in pgns)
    return 126464, 6, data


# canboat CONTROLLER_STATE: 0 error active · 1 error passive · 2 bus off · 3 n/a
CONTROLLER_STATE = {"ERROR-ACTIVE": 0, "ERROR-WARNING": 0, "ERROR-PASSIVE": 1, "BUS-OFF": 2}


def encode_126993(interval_s: float = 60.0, seq: int = 0, controller_state: int = 3):
    """Heartbeat (single frame): the interval it repeats at (0.01 s), a 0-252 sequence counter,
    controller 1 state (from the kernel's CAN state when known), controller 2 n/a, equipment
    status 0 = operational, reserved all-1s."""
    data = struct.pack("<H", min(0xFFFC, round(interval_s * 100)))
    data += bytes([seq % 253,
                   (controller_state & 3) | (0b11 << 2) | (0b00 << 4) | (0b11 << 6)])
    data += b"\xff\xff\xff\xff"
    return 126993, 7, data


def _fix32(s: str) -> bytes:
    """STRING_FIX(32): ASCII, unused tail padded 0xFF (canboat convention)."""
    return str(s or "")[:32].encode("ascii", "replace").ljust(32, b"\xff")


def encode_126996(model: str = "SR33 C4 Navigator", sw: str = "1.0", hw: str = "Pi4/PICAN-M",
                  serial: str = "C4-000100"):
    """Product Information (134-byte fast packet) — the rung ABOVE the address claim on the
    plotter's enumeration ladder: after our 60928 the Garmins immediately request this
    (observed live: directed 59904 with payload 14 F0 01). N2K db version 2.101, product
    code arbitrary; cert level 0, LEN 1 (one 50 mA load unit) — cosmetic but honest."""
    data = struct.pack("<HH", 2101, 100)
    data += _fix32(model) + _fix32(sw) + _fix32(hw) + _fix32(serial)
    data += bytes([0, 1])          # certification level, load equivalency
    return 126996, 6, data


def encode_129283(xte_m: float | None = None, terminated: bool = False, sid: int = 0):
    """XTE, single frame. Mode autonomous. `terminated=False` is the "we are navigating" heartbeat
    that makes a plotter treat the 129284/129285 stream as an active leg; `terminated=True`
    (with XTE n/a) is the "navigation terminated" sent on Clear / expiry / course change.
    Sign (Signal K courseRhumbline.crossTrackError, which signalk-to-nmea2000 passes straight
    into this field): positive = boat RIGHT of the track, steer left. None → n/a, never 0."""
    xte = NA_I32 if xte_m is None else max(-(2**31) + 1, min(2**31 - 2, round(xte_m * 100)))
    data = bytes([sid & 0xFF, 0x00 | (0b11 << 4) | ((1 if terminated else 0) << 6)])
    data += struct.pack("<i", xte) + b"\xff\xff"
    return 129283, 3, data


def _u16_rad(deg):
    return NA_U16 if deg is None else round((float(deg) % 360) * math.pi / 180 / 0.0001)


def encode_129284(dest_lat, dest_lon, dest_wp: int | None = 1, origin_wp: int | None = 0,
                  eta_epoch: float | None = None, dtw_m: float | None = None,
                  brg_deg: float | None = None, sid: int = 0, brg_origin_deg: float | None = None,
                  perp_crossed: bool = False, arrived: bool = False,
                  closing_mps: float | None = None):
    """Navigation Data (34-byte fast packet), live from the boat's own position: distance and
    bearing (true, great circle) from the boat to the destination waypoint, the leg's own
    bearing (origin → destination), live ETA and closing velocity. Unknown fields ride as 'not
    available' — never a made-up value. `brg_deg` is position → destination; the leg bearing
    defaults to it when not given."""
    dtw = NA_U32 if dtw_m is None else max(0, min(NA_U32 - 1, round(dtw_m * 100)))
    flags = (0b00 | ((1 if perp_crossed else 0) << 2) | ((1 if arrived else 0) << 4)
             | (0b00 << 6))        # true brg · perpendicular · arrival circle · great circle
    if eta_epoch is None:
        eta_t, eta_d = NA_U32, NA_U16
    else:
        days, secs = divmod(float(eta_epoch), 86400.0)
        eta_t, eta_d = round(secs / 0.0001), int(days)
    brg_p = _u16_rad(brg_deg)
    brg_o = _u16_rad(brg_origin_deg if brg_origin_deg is not None else brg_deg)
    wcv = NA_I16 if closing_mps is None else max(-32767, min(32766, round(closing_mps * 100)))
    data = (bytes([sid & 0xFF]) + struct.pack("<I", dtw) + bytes([flags])
            + struct.pack("<IH", eta_t, eta_d)
            + struct.pack("<HH", brg_o, brg_p)
            + struct.pack("<II", NA_U32 if origin_wp is None else origin_wp,
                          NA_U32 if dest_wp is None else dest_wp)
            + struct.pack("<ii", _i32_1e7(dest_lat), _i32_1e7(dest_lon))
            + struct.pack("<h", wcv))
    return 129284, 3, data


def encode_129284_null(sid: int = 0):
    """Navigation Data with every field n/a — sent with the terminated 129283 so a plotter that
    keeps the last 129284 has nothing left to show."""
    return encode_129284(None, None, dest_wp=None, origin_wp=None, sid=sid)


def encode_129285(route_name: str, waypoints, start_rps: int = 0, n_items: int | None = None,
                  database_id: int = 1, route_id: int = 1):
    """Route/WP list (fast packet, ≤223 bytes → chunk long routes with start_rps slices).
    waypoints = [(wp_id, name, lat, lon)] for THIS chunk. nItems = the count IN THIS message
    (the canboat reference parser reads exactly nItems repeating rows — declaring the whole
    route's count here makes decoders chase a phantom row into the frame padding); startRps
    places the slice within the route."""
    n_items = len(waypoints) if n_items is None else n_items
    data = struct.pack("<HHHH", start_rps, n_items, database_id, route_id)
    data += bytes([0x00 | (0b00 << 3) | (0b111 << 5)])   # forward · no supplementary · reserved
    data += _lau(route_name)
    data += b"\xff"                                       # reserved9
    for (wid, name, lat, lon) in waypoints:
        data += struct.pack("<H", wid) + _lau(name)
        data += struct.pack("<ii", _i32_1e7(lat), _i32_1e7(lon))
    if len(data) > 223:
        raise ValueError(f"129285 chunk too large ({len(data)} > 223B) — send fewer waypoints")
    return 129285, 6, data


def chunk_route(route_name: str, wpts, per_msg: int = 10):
    """Slice a full route into 129285 messages: [(pgn, prio, data), ...]. At most `per_msg`
    waypoints per message AND never over the 223-byte cap: long names pack fewer per message.
    Raises ValueError only if a single waypoint can't fit (it always can: names cap at 16)."""
    rows = [(i, w.get("name") or f"WP{i + 1:02d}", w.get("lat"), w.get("lon"))
            for i, w in enumerate(wpts)]
    out, k = [], 0
    while k < len(rows):
        n = min(per_msg, len(rows) - k)
        while True:
            try:
                out.append(encode_129285(route_name, rows[k:k + n], start_rps=k))
                break
            except ValueError:
                if n == 1:
                    raise
                n -= 1
        k += n
    return out


SINGLE_FRAME = (59392, 59904, 60928, 126993, 129283)


class N2kSender:
    """SocketCAN writer with NMEA-2000 fast-packet framing. Thread-safe; `sa` may change (an
    address-claim contest we lose moves us). Counts frames and send errors; a failed send
    closes the socket so the next send reopens it (an interface that came back works again)."""

    def __init__(self, iface: str = "can0", sa: int = DEFAULT_SA, claim=None):
        """`claim` = (pgn, prio, data) of our address claim: the sender then STARTS with it
        pending, so nothing ordinary goes out before the claim is on the bus."""
        self.iface, self.sa = iface, sa
        self._sock = None
        self._seq = {}                       # pgn -> fast-packet sequence counter
        self.claim_pending = claim           # (pgn, prio, data) of a claim not yet on the bus
        self._claim_tried_at = None
        self._lock = threading.Lock()
        self.frames_sent = 0
        self.send_errors = 0
        self.last_error = None
        self.last_ok_at = None
        self.last_error_at = None

    def open(self):
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            s.bind((self.iface,))
        except OSError:
            s.close()
            raise
        self._sock = s

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _frame(self, cid: int, payload: bytes):
        self._sock.send(struct.pack("<IB3x8s", cid | CAN_EFF_FLAG, len(payload),
                                    payload.ljust(8, b"\xff")))

    def send(self, pgn: int, prio: int, data: bytes, single: bool | None = None,
             dest: int = 0xFF, src: int | None = None) -> int:
        """Send one PGN — single-frame when it fits and the PGN is single-frame; else fast-packet.
        Returns frames sent; 0 on error (counted, never raised). `src` overrides the source
        address for one message (the 254 "cannot claim")."""
        with self._lock:
            return self._send_counted(pgn, prio, data, single, dest, src)

    CLAIM_RETRY_S = 1.0

    def change_address(self, new_sa: int, claim):
        """Move to `new_sa` and send its address claim (`claim` = (pgn, prio, data)) in ONE hold
        of the send lock: no other thread's frame can go out from the new address before the
        claim that announces it. Until the claim has actually gone out the claim is PENDING:
        ordinary sends are refused, and the claim is retried at most once a second (by
        retry_claim(), which the bus thread calls every second, or by the next send). new_sa
        254 = "cannot claim" (sent from 254; nothing after)."""
        with self._lock:
            self.sa = new_sa
            self.claim_pending = claim
            self._claim_tried_at = None
            return self._try_claim_locked(force=True)

    def retry_claim(self):
        """Send a pending claim again if CLAIM_RETRY_S has passed. → True when none is pending."""
        with self._lock:
            return self._try_claim_locked()

    def _try_claim_locked(self, force=False):
        if self.claim_pending is None:
            return True
        now = time.monotonic()
        if not force and self._claim_tried_at is not None \
                and now - self._claim_tried_at < self.CLAIM_RETRY_S:
            return False
        self._claim_tried_at = now
        pgn, prio, data = self.claim_pending
        if self._send_counted(pgn, prio, data, None, 0xFF, self.sa, claim=True) > 0:
            self.claim_pending = None
            return True
        return False

    def _send_counted(self, pgn, prio, data, single, dest, src, claim=False):
        sa = self.sa if src is None else src
        if src is None and self.sa > 253:
            self.send_errors += 1
            self.last_error = "no address (claim lost)"
            self.last_error_at = time.time()
            return 0
        if not claim and self.claim_pending is not None and not self._try_claim_locked():
            self.send_errors += 1
            self.last_error = "address claim pending"
            self.last_error_at = time.time()
            return 0
        try:
            if self._sock is None:
                self.open()
            n = self._send_locked(pgn, prio, data, single, dest, sa)
        except OSError as e:
            self.send_errors += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self.last_error_at = time.time()
            self._close_locked()
            return 0
        self.frames_sent += n
        self.last_ok_at = time.time()
        return n

    def _send_locked(self, pgn, prio, data, single, dest, sa):
        cid = can_id(pgn, sa, prio, dest)
        if single is None:
            single = len(data) <= 8 and pgn in SINGLE_FRAME
        if single:
            self._frame(cid, data)
            return 1
        seq = self._seq.get(pgn, 0)
        self._seq[pgn] = (seq + 1) % 8
        frames = 0
        first = bytes([(seq << 5) | 0, len(data)]) + data[:6]
        self._frame(cid, first)
        frames += 1
        rest = data[6:]
        n = 1
        while rest:
            self._frame(cid, bytes([(seq << 5) | n]) + rest[:7])
            rest = rest[7:]
            n += 1
            frames += 1
        return frames
