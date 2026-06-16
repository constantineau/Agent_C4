#!/usr/bin/env bash
# Replay a recorded NMEA 2000 candump log onto the bench vcan0, exactly as it was
# captured on the boat's can0. This is the gold-standard bench fixture (brief §8):
# the first dockside visit records ~1h of real traffic with:
#     candump -l can0                      # writes candump-<date>.log
# then on the VPS bench you replay it for development:
#     bash pi/bench/replay.sh logs/candump-2026-07-01.log
#
# canplayer remaps the recorded interface (can0) onto vcan0 so timing is preserved.
set -euo pipefail
LOG="${1:?usage: replay.sh <candump-log> [iface]}"
IFACE="${2:-vcan0}"

ip link show "$IFACE" >/dev/null 2>&1 || { echo "no $IFACE — run pi/bench/setup_vcan.sh first" >&2; exit 1; }
echo "replaying $LOG onto $IFACE (Ctrl-C to stop)…"
exec canplayer -I "$LOG" "${IFACE}=can0"
