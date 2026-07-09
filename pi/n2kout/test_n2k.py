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
check("flags: true brg + rhumbline", d[5] == 0b01000000)
eta_t, eta_d = struct.unpack("<IH", d[6:12])
check("ETA date = days since epoch", eta_d == 1782900000 // 86400)
check("ETA time = seconds-in-day / 1e-4", eta_t == round((1782900000 % 86400) / 0.0001))
check("origin/dest wp numbers", struct.unpack("<II", d[16:24]) == (2, 3))
lat, lon = struct.unpack("<ii", d[24:32])
check("dest lat/lon 1e-7 deg", lat == 455000000 and lon == -822500000)
check("closing velocity n/a (signed max)", struct.unpack("<h", d[32:34])[0] == n2k.NA_I16)

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

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
