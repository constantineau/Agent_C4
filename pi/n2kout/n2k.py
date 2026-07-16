"""NMEA 2000 route TRANSMITTER — put the gameplan on the Garmin 943's chart, live, from a button.

The GPSMAP 943 officially RECEIVES 129284 (Navigation Data) and 129285 (Navigation - Route/WP
information) — the external-navigator pattern PC nav packages (Expedition et al.) use to push
their active route onto an MFD. This module is the boat's own computer talking to the boat's own
display over its own bus: RRS-41 clean, same legal footing as the rest of Tier 1.

Pure stdlib: Linux SocketCAN (AF_CAN raw) + hand-packed PGNs against the canboat field
definitions (canboat.json, fetched 2026-07-10):

  60928  ISO Address Claim          — minimal claim at startup so the bus sees a legit device
  129283 Cross Track Error         — single frame; tells the plotter navigation is ACTIVE
  129285 Navigation Route/WP info  — fast-packet; the route: names + positions. A fast packet
                                      caps at 223 bytes, so long routes go out CHUNKED via the
                                      startRps/nItems fields (~10 waypoints per message)
  129284 Navigation Data           — fast-packet; the active leg: destination waypoint,
                                      plan ETA; unknown live fields ride as NMEA "not available"

Conventions honored: little-endian fields; reserved bits all-1s; unsigned n/a = all-1s; signed
n/a = max positive; STRING_LAU = [total_len][1=ASCII][chars]. Nothing here reads the bus.

DOCKSIDE VERIFICATION REQUIRED: the encodings round-trip through canboatjs on the bench (vcan0),
but how the 943 *renders* an external route (line on the chart vs data fields only) must be
eyeballed once the N2K cable is plugged. Until then the service simply idles.
"""
from __future__ import annotations

import socket
import struct
import threading

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
    """29-bit extended id. PDU2 (PF>=240) is broadcast; PDU1 carries a destination byte."""
    pf = (pgn >> 8) & 0xFF
    if pf >= 240:
        return (prio << 26) | (pgn << 8) | sa
    return (prio << 26) | (pf << 16) | (dest << 8) | sa


def encode_60928(unique: int = 100, mfg: int = 2046, sa: int = DEFAULT_SA):
    """ISO Address Claim: NAME = unique(21) | mfg(11) | instances | function | class | flags.
    Device class 60 (Navigation), function 130 (route-source-ish) — cosmetic; the claim's job
    is simply to make SA 35 a well-behaved bus citizen."""
    name = (unique & 0x1FFFFF) | ((mfg & 0x7FF) << 21)
    data = struct.pack("<I", name)
    data += bytes([0x00,            # device instance
                   130,             # device function
                   (60 << 1),       # class 60 (navigation), low bit reserved
                   0x80 | (4 << 4)])  # arbitrary-address-capable | industry group 4 (marine)
    return 60928, 6, data


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


def encode_129283(xte_m: float | None = 0.0, sid: int = 0):
    """XTE, single frame. Mode=autonomous, navigation NOT terminated — the 'we are navigating'
    heartbeat that makes a plotter treat the 129284/129285 stream as an active leg."""
    xte = NA_I32 if xte_m is None else max(-(2**31) + 1, min(2**31 - 2, round(xte_m * 100)))
    data = bytes([sid, 0x00 | (0b11 << 4)]) + struct.pack("<i", xte) + b"\xff\xff"
    return 129283, 3, data


def encode_129284(dest_lat, dest_lon, dest_wp: int = 1, origin_wp: int = 0,
                  eta_epoch: float | None = None, dtw_m: float | None = None,
                  brg_deg: float | None = None, sid: int = 0):
    """Navigation Data (34-byte fast packet): the active leg's destination + plan ETA. Live
    fields the caller doesn't know ride as 'not available' — honest, and the plotter tolerates it."""
    dtw = NA_U32 if dtw_m is None else round(dtw_m * 100)
    flags = 0b00 | (0b00 << 2) | (0b00 << 4) | (0b01 << 6)   # true brg · not crossed · not arrived · rhumbline
    if eta_epoch is None:
        eta_t, eta_d = NA_U32, NA_U16
    else:
        days, secs = divmod(float(eta_epoch), 86400.0)
        eta_t, eta_d = round(secs / 0.0001), int(days)
    brg = NA_U16 if brg_deg is None else round((float(brg_deg) % 360) * 3.141592653589793 / 180 / 0.0001)
    data = (bytes([sid]) + struct.pack("<I", dtw) + bytes([flags])
            + struct.pack("<IH", eta_t, eta_d)
            + struct.pack("<HH", brg, brg)
            + struct.pack("<II", origin_wp, dest_wp)
            + struct.pack("<ii", _i32_1e7(dest_lat), _i32_1e7(dest_lon))
            + struct.pack("<h", NA_I16))
    return 129284, 3, data


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
    """Slice a full route into 129285 messages: [(pgn, prio, data), ...]."""
    rows = [(i, w.get("name") or f"WP{i + 1:02d}", w.get("lat"), w.get("lon"))
            for i, w in enumerate(wpts)]
    out = []
    for k in range(0, len(rows), per_msg):
        out.append(encode_129285(route_name, rows[k:k + per_msg], start_rps=k))
    return out


class N2kSender:
    """SocketCAN writer with NMEA-2000 fast-packet framing. One instance per interface."""

    def __init__(self, iface: str = "can0", sa: int = DEFAULT_SA):
        self.iface, self.sa = iface, sa
        self._sock = None
        self._seq = {}                       # pgn -> fast-packet sequence counter
        self._lock = threading.Lock()

    def open(self):
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((self.iface,))
        self._sock = s

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None

    def _frame(self, cid: int, payload: bytes):
        self._sock.send(struct.pack("<IB3x8s", cid | CAN_EFF_FLAG, len(payload),
                                    payload.ljust(8, b"\xff")))

    def send(self, pgn: int, prio: int, data: bytes, single: bool | None = None):
        """Send one PGN — single-frame when it fits and the PGN is single-frame; else fast-packet."""
        with self._lock:
            cid = can_id(pgn, self.sa, prio)
            if single is None:
                single = len(data) <= 8 and pgn in (129283, 60928)
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
