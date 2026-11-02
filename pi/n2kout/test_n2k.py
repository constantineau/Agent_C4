"""N2K encoder — unit test: CAN id construction, field packing against the canboat definitions,
STRING_LAU, fast-packet framing, route chunking, and the 223-byte cap. Pure (no socket).

Run:  python3 pi/n2kout/test_n2k.py
"""
import struct
import sys

sys.path.insert(0, "pi/n2kout")
import n2k  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


print("GOLDEN wire ids — every PGN we transmit, at SA 35 and its own priority:")
# Worked out by hand from the ISO 11783 / NMEA 2000 id layout, NOT with can_id():
#   bits 28-26 priority · 25 EDP · 24 DP · 23-16 PF · 15-8 PS (PDU1: dest, PDU2: PGN low byte)
#   · 7-0 source address. Cross-checked by decoding these ids with canboatjs (signalk-server
#   v2.27.0 image), pi/n2kout/canboat_roundtrip.sh.
GOLDEN = [
    # (what, pgn, prio, dest, wire id)
    ("60928 address claim, to global", 60928, 6, 0xFF, 0x18EEFF23),   # DP0 PF EE PS FF
    ("59392 NAK, to the requester (SA 7)", 59392, 6, 0x07, 0x18E80723),  # DP0 PF E8 PS 07
    ("126464 PGN list (PDU1, DP=1)", 126464, 6, 0xFF, 0x19EEFF23),     # DP1 PF EE PS FF
    ("126993 heartbeat", 126993, 7, 0xFF, 0x1DF01123),                 # 7<<26 | 1F011 << 8
    ("126996 product info", 126996, 6, 0xFF, 0x19F01423),
    ("129283 XTE", 129283, 3, 0xFF, 0x0DF90323),
    ("129284 navigation data", 129284, 3, 0xFF, 0x0DF90423),
    ("129285 route/WP list", 129285, 6, 0xFF, 0x19F90523),
]
for what, pgn, prio, dest, wire in GOLDEN:
    got = n2k.can_id(pgn, 35, prio, dest)
    check(f"{what}: {wire:08X} [{got:08X}]", got == wire)
    check(f"  …decodes back to PGN {pgn}", n2k.pgn_of(wire) == pgn)
check("126464 is NOT sent as 18EEFF23 (that's an address claim)",
      n2k.can_id(126464, 35, 6) != 0x18EEFF23 and n2k.pgn_of(0x18EEFF23) == 60928)
check("'cannot claim' from the null address 254: 18EEFFFE", n2k.can_id(60928, 254, 6) == 0x18EEFFFE)

print("GOLDEN wire ids — through the encoders and the sender (what really goes out):")
wire_frames = []


class WireSock:
    def send(self, raw):
        wire_frames.append(struct.unpack("<I", raw[:4])[0] & 0x1FFFFFFF)


tx = n2k.N2kSender("fake")
tx._sock = WireSock()
for (pgn, prio, data), dest, want in (
        (n2k.encode_60928(), 0xFF, 0x18EEFF23),
        (n2k.encode_59392_nak(126998), 0x07, 0x18E80723),
        (n2k.encode_126464(0, n2k.TX_PGNS), 0xFF, 0x19EEFF23),
        (n2k.encode_126464(1, n2k.RX_PGNS), 0xFF, 0x19EEFF23),
        (n2k.encode_126993(), 0xFF, 0x1DF01123),
        (n2k.encode_126996(), 0xFF, 0x19F01423),
        (n2k.encode_129283(1.0), 0xFF, 0x0DF90323),
        (n2k.encode_129284(45.0, -82.0), 0xFF, 0x0DF90423),
        (n2k.encode_129285("C4", [(0, "C4-01", 45.0, -82.0)]), 0xFF, 0x19F90523)):
    wire_frames.clear()
    tx.send(pgn, prio, data, dest=dest)
    check(f"{pgn}: every frame on the wire is {want:08X}",
          wire_frames and all(f == want for f in wire_frames))

print("can ids:")
check("PDU2 broadcast id (129285, prio 6, sa 35)",
      n2k.can_id(129285, 35, 6) == (6 << 26) | (129285 << 8) | 35)
check("PDU1 id carries the destination (60928 → global)",
      n2k.can_id(60928, 35, 6) == (6 << 26) | (0xEE << 16) | (0xFF << 8) | 35)

print("129283 XTE:")
pgn, prio, d = n2k.encode_129283(-123.45, sid=7)
check("single frame, 8 bytes, prio 3", pgn == 129283 and prio == 3 and len(d) == 8)
check("mode autonomous + reserved 1s + nav NOT terminated", d[1] == 0x30)
check("xte scaled 0.01 m signed", struct.unpack("<i", d[2:6])[0] == -12345)

print("129284 nav data:")
pgn, prio, d = n2k.encode_129284(45.5, -82.25, dest_wp=3, origin_wp=2, eta_epoch=1782900000)
check("34 bytes (canboat length)", len(d) == 34)
check("unknown DTW = n/a all-1s", struct.unpack("<I", d[1:5])[0] == n2k.NA_U32)
check("flags: true brg + great circle, not crossed, not arrived", d[5] == 0)
eta_t, eta_d = struct.unpack("<IH", d[6:12])
check("ETA date = days since epoch", eta_d == 1782900000 // 86400)
check("ETA time = seconds-in-day / 1e-4", eta_t == round((1782900000 % 86400) / 0.0001))
check("origin/dest wp numbers", struct.unpack("<II", d[16:24]) == (2, 3))
lat, lon = struct.unpack("<ii", d[24:32])
check("dest lat/lon 1e-7 deg", lat == 455000000 and lon == -822500000)
check("closing velocity n/a (signed max)", struct.unpack("<h", d[32:34])[0] == n2k.NA_I16)

pgn, prio, d = n2k.encode_129284(45.5, -82.25, dest_wp=3, origin_wp=2, dtw_m=1234.56,
                                 brg_deg=90.0, brg_origin_deg=180.0, arrived=True,
                                 perp_crossed=True, closing_mps=-2.5)
check("live DTW 0.01 m", struct.unpack("<I", d[1:5])[0] == 123456)
check("flags: perpendicular + arrival set", d[5] == (1 << 2) | (1 << 4))
check("bearings: origin→dest first, then position→dest (rad 1e-4)",
      struct.unpack("<HH", d[12:16]) == (round(3.14159265 / 0.0001), round(1.57079633 / 0.0001)))
check("closing velocity signed 0.01 m/s", struct.unpack("<h", d[32:34])[0] == -250)

print("XTE never fake, navigation terminated:")
pgn, prio, d = n2k.encode_129283()
check("default XTE is n/a, not 0", struct.unpack("<i", d[2:6])[0] == n2k.NA_I32)
pgn, prio, d = n2k.encode_129283(None, terminated=True)
check("terminated: bits 6-7 = 1 (Yes), mode autonomous, reserved 1s", d[1] == 0x70)
pgn, prio, d = n2k.encode_129284_null()
check("null 129284: DTW, wp numbers, lat/lon all n/a",
      struct.unpack("<I", d[1:5])[0] == n2k.NA_U32
      and struct.unpack("<II", d[16:24]) == (n2k.NA_U32, n2k.NA_U32)
      and struct.unpack("<ii", d[24:32]) == (n2k.NA_I32, n2k.NA_I32))

print("device identity:")
name = n2k.make_name(unique=100, mfg=2046)
check("NAME fields: class 60 Navigation, function 190, marine, AAC",
      (name >> 49) & 0x7F == 60 and (name >> 40) & 0xFF == 190
      and (name >> 60) & 0x7 == 4 and name >> 63 == 1
      and name & 0x1FFFFF == 100 and (name >> 21) & 0x7FF == 2046)
pgn, prio, d = n2k.encode_60928(name)
check("60928 carries the NAME LE (byte 5 function, byte 6 class<<1, byte 7 AAC|marine)",
      pgn == 60928 and d[5] == 190 and d[6] == 120 and d[7] == 0xC0
      and n2k.decode_name(d) == name)
pgn, prio, d = n2k.encode_126464(0, (129283, 129284))
check("126464: function + 3-byte PGNs", d == bytes([0]) + (129283).to_bytes(3, "little")
      + (129284).to_bytes(3, "little"))
pgn, prio, d = n2k.encode_126993(60, seq=5, controller_state=2)
check("126993: 8 bytes, interval 6000, seq 5, bus-off state, equipment ok",
      len(d) == 8 and struct.unpack("<H", d[0:2])[0] == 6000 and d[2] == 5
      and d[3] & 3 == 2 and (d[3] >> 4) & 3 == 0)
pgn, prio, d = n2k.encode_59392_nak(126998)
check("59392 NAK: control 1, PGN in the last 3 bytes", pgn == 59392 and d[0] == 1
      and int.from_bytes(d[5:8], "little") == 126998)
check("59392 is PDU1: the CAN id carries the destination",
      (n2k.can_id(59392, 35, 6, dest=7) >> 8) & 0xFF == 7)

print("129285 route list:")
pgn, prio, d = n2k.encode_129285("C4 left", [(0, "C4-01", 43.02, -82.4),
                                             (1, "C4-02", 43.5, -82.55)])
check("header: startRps 0 · nItems = rows in THIS message · db 1 · route 1",
      struct.unpack("<HHHH", d[0:8]) == (0, 2, 1, 1))
check("direction fwd + reserved 1s", d[8] == 0b11100000)
check("route name as STRING_LAU ascii", d[9] == 2 + len("C4 left") and d[10] == 1
      and d[11:18] == b"C4 left")
i = 9 + d[9] + 1                       # skip name LAU + reserved9
wid = struct.unpack("<H", d[i:i + 2])[0]
check("first wp id 0 + LAU name", wid == 0 and d[i + 2] == 7 and d[i + 4:i + 9] == b"C4-01")
lat = struct.unpack("<i", d[i + 2 + d[i + 2]:i + 2 + d[i + 2] + 4])[0]
check("first wp lat 1e-7", lat == 430200000)

print("chunking:")
wpts = [{"name": f"C4-{i:02d}", "lat": 43 + i * 0.05, "lon": -82.4} for i in range(43)]
msgs = n2k.chunk_route("C4 left", wpts, per_msg=10)
check("43 wpts → 5 chunks", len(msgs) == 5)
starts = [struct.unpack("<H", m[2][0:2])[0] for m in msgs]
counts = [struct.unpack("<H", m[2][2:4])[0] for m in msgs]
check("chunks carry startRps 0,10,… and per-message nItems 10,10,10,10,3",
      starts == [0, 10, 20, 30, 40] and counts == [10, 10, 10, 10, 3])
check("every chunk under the 223-byte fast-packet cap", all(len(m[2]) <= 223 for m in msgs))
try:
    n2k.encode_129285("x", [(i, f"WP{i}", 43.0, -82.0) for i in range(40)])
    check("oversize chunk raises", False)
except ValueError:
    check("oversize chunk raises", True)

long = [{"name": "X" * 16, "lat": 43.0, "lon": -82.0} for _ in range(20)]
msgs = n2k.chunk_route("C4 a long name", long, per_msg=10)
check("16-char names pack fewer per message, all under 223 B, all rows sent",
      all(len(m[2]) <= 223 for m in msgs)
      and sum(struct.unpack("<H", m[2][2:4])[0] for m in msgs) == 20)

print("fast-packet framing:")
frames = []


class FakeSock:
    def send(self, raw):
        frames.append(struct.unpack("<IB3x8s", raw))


s = n2k.N2kSender("fake")
s._sock = FakeSock()
pgn, prio, d = n2k.encode_129284(45.5, -82.25)
n = s.send(pgn, prio, d)
check("34B → 5 frames (6 + 4×7)", n == 5 and len(frames) == 5)
cid, dlc, f0 = frames[0]
check("frame ids: EFF flag + PGN in the id", (cid & 0x80000000) and ((cid >> 8) & 0x1FFFF) == 129284)
check("frame0: (seq|0) + total length + 6 data bytes", (f0[0] & 0x1F) == 0 and f0[1] == 34
      and f0[2:8] == d[0:6])
check("frame1 counter + 7 data bytes", (frames[1][2][0] & 0x1F) == 1
      and frames[1][2][1:8] == d[6:13])
reassembled = f0[2:8] + b"".join(fr[2][1:8] for fr in frames[1:])
check("frames reassemble to the payload", reassembled[:34] == d)
seq0 = (f0[0] >> 5) & 0x7
frames.clear()
s.send(pgn, prio, d)
check("sequence counter advances per send", ((frames[0][2][0] >> 5) & 0x7) == (seq0 + 1) % 8)

print("send errors are counted, not raised:")


class BadSock:
    def send(self, raw):
        raise OSError(105, "No buffer space available")

    def close(self):
        pass


s2 = n2k.N2kSender("fake")
s2._sock = BadSock()
n = s2.send(*n2k.encode_129283(1.0))
check("failed send → 0 frames, 1 error, socket dropped for a reopen",
      n == 0 and s2.send_errors == 1 and s2._sock is None and "buffer" in s2.last_error)
s2.sa = 254
s2._sock = FakeSock()
check("no address (claim lost) → nothing sent", s2.send(*n2k.encode_129283(1.0)) == 0)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
