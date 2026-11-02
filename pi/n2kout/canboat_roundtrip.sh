#!/usr/bin/env bash
# Round-trip every PGN n2kout transmits through canboatjs (the decoder Signal K uses), from the
# exact frames the sender puts on the wire: CAN id + fast-packet framing + fields. Checks the
# decoded PGN, source, destination and priority, and prints the decoded fields to eyeball.
# Not part of `make ci` (needs the signalk-server image, 1.7 GB); run it after touching n2k.py:
#   bash pi/n2kout/canboat_roundtrip.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
SK_IMAGE=${SK_IMAGE:-cr.signalk.io/signalk/signalk-server:v2.27.0}
N2K_IMAGE=${N2K_IMAGE:-sr33-ci-n2kout-base}
docker image inspect "$N2K_IMAGE" >/dev/null 2>&1 \
  || docker build -q -t "$N2K_IMAGE" -f pi/n2kout/Dockerfile . >/dev/null

# 1. the frames, as candump log lines, one "#expect pgn src dst prio" header per message
frames=$(docker run --rm --network none -v "$PWD/pi/n2kout:/x:ro" -w /x "$N2K_IMAGE" python3 -c '
import struct, n2k
out = []
class Sock:
    def send(self, raw):
        cid, dlc, data = struct.unpack("<IB3x8s", raw)
        out.append("(0.0) can0 %08X#%s" % (cid & 0x1FFFFFFF, data[:dlc].hex().upper()))
tx = n2k.N2kSender("x"); tx._sock = Sock()
msgs = [(n2k.encode_60928(), 255), (n2k.encode_59392_nak(126998), 7),
        (n2k.encode_126464(0, n2k.TX_PGNS), 255), (n2k.encode_126464(1, n2k.RX_PGNS), 255),
        (n2k.encode_126993(60, 1, 0), 255), (n2k.encode_126996(), 255),
        (n2k.encode_129283(-12.34), 255), (n2k.encode_129283(None, terminated=True), 255),
        (n2k.encode_129284(45.5, -82.25, dest_wp=3, origin_wp=2, eta_epoch=1782900000,
                           dtw_m=1234.5, brg_deg=90, brg_origin_deg=80, closing_mps=2.5), 255),
        (n2k.encode_129284_null(), 255),
        (n2k.encode_129285("C4 left", [(0, "C4-01", 43.02, -82.4), (1, "C4-02", 43.5, -82.55)]), 255)]
for (pgn, prio, data), dest in msgs:
    out.append("#expect %d 35 %d %d" % (pgn, dest, prio))
    tx.send(pgn, prio, data, dest=dest)
print("\n".join(out))
')

# 2. decode with canboatjs, compare
printf '%s\n' "$frames" | docker run --rm -i --network none --entrypoint node "$SK_IMAGE" -e '
const { FromPgn } = require("/home/node/signalk/node_modules/@canboat/canboatjs");
const lines = require("fs").readFileSync(0, "utf8").trim().split("\n");
const p = new FromPgn({ returnNulls: true });
let expect = null, bad = 0, n = 0;
for (const l of lines) {
  if (l.startsWith("#expect")) { expect = l.split(" ").slice(1).map(Number); continue; }
  const m = p.parseString(l);
  if (!m) continue;                                  // a fast-packet frame mid-message
  n++;
  const [pgn, src, dst, prio] = expect;
  const ok = m.pgn === pgn && m.src === src && m.dst === dst && m.prio === prio;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${pgn} -> pgn ${m.pgn} src ${m.src} dst ${m.dst} prio ${m.prio}  ${m.description || ""}`);
  console.log("       " + JSON.stringify(m.fields));
}
console.log(`${n} messages decoded, ${bad} wrong`);
process.exit(bad || n !== lines.filter((l) => l.startsWith("#expect")).length ? 1 : 0);
'
