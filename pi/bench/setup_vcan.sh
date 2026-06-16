#!/usr/bin/env bash
# Bench setup: create the virtual CAN interface used to develop the Pi software on the
# VPS with no boat hardware. This is the "vcan0" side of the brief's portability rule
# (bench = vcan0, boat = can0 — a single CAN_IFACE switch everywhere else).
#
# Idempotent. Needs sudo (loads the vcan kernel module + creates a netdev).
#   bash pi/bench/setup_vcan.sh                # create vcan0
#   IFACE=vcan1 bash pi/bench/setup_vcan.sh    # alternate name
#
# NOTE: this is session-only — the interface is gone after a reboot. To persist it,
# enable a boot-time unit (see pi/bench/README.md); requires explicit admin opt-in.
set -euo pipefail
IFACE="${IFACE:-vcan0}"

sudo modprobe vcan
if ip link show "$IFACE" >/dev/null 2>&1; then
  echo "$IFACE already exists"
else
  sudo ip link add dev "$IFACE" type vcan
  echo "created $IFACE"
fi
sudo ip link set up "$IFACE"
ip -brief link show "$IFACE"
