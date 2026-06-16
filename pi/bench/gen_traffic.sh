#!/usr/bin/env bash
# Smoke-test traffic generator for the bench, for use BEFORE a real boat log exists.
# Emits random extended-ID CAN frames (NMEA 2000 uses 29-bit IDs) so you can verify the
# CAN substrate, candump, and Signal K's CAN provider are wired up. This is NOT decodable
# N2K — for real PGNs either replay a recorded log (pi/bench/replay.sh) or use Signal K's
# built-in --sample-n2k-data provider.
#
#   bash pi/bench/gen_traffic.sh            # ~ a few frames/sec until Ctrl-C
#   COUNT=200 GAP=10 bash pi/bench/gen_traffic.sh
set -euo pipefail
IFACE="${IFACE:-vcan0}"
COUNT="${COUNT:-0}"     # 0 = run forever
GAP="${GAP:-200}"       # ms between frames

ip link show "$IFACE" >/dev/null 2>&1 || { echo "no $IFACE — run pi/bench/setup_vcan.sh first" >&2; exit 1; }
echo "generating frames on $IFACE (gap ${GAP}ms, count ${COUNT:-∞})…"
exec cangen "$IFACE" -e -I r -L r -g "$GAP" ${COUNT:+-n "$COUNT"}
