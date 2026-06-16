#!/usr/bin/env bash
# Push the pi/ tree to the boat computer over Tailscale + SSH and restart the uplink.
# The Pi is a deploy target, not a dev host (brief §8).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

PI_HOST="${PI_HOST:-sr33-pi}"        # Tailscale hostname or user@host
PI_DEST="${PI_DEST:-/opt/sr33/pi}"

echo "==> rsync pi/ -> ${PI_HOST}:${PI_DEST}"
rsync -az --delete pi/ "${PI_HOST}:${PI_DEST}/"

echo "==> install/refresh systemd unit + restart uplink"
ssh "$PI_HOST" '
  sudo cp /opt/sr33/pi/systemd/sr33-uplink.service /etc/systemd/system/ &&
  sudo systemctl daemon-reload &&
  sudo systemctl enable --now sr33-uplink &&
  sudo systemctl restart sr33-uplink &&
  systemctl --no-pager status sr33-uplink | head -n 5
'
echo "==> done"
