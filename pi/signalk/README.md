# Signal K configuration

Signal K decodes the NMEA 2000 bus into normalized JSON (WS + REST) on **:3010** (:3000 is
DreamCRM on this VM). Config lives on the `sk_config` named volume; these files seed it.

- **`settings.template.json`** — server settings. `signalk-init` (alpine) renders it to
  `settings.json` on the volume, substituting `__CAN_IFACE__` with `$CAN_IFACE` (vcan0 bench /
  can0 boat — the single portability switch), then chowns the volume to uid 1000.
- **`derived-data.json`** — enable config for the `signalk-derived-data` plugin (id
  `derived-data`). The `signalk-derived` init service installs the plugin into the config
  volume (idempotent; network needed only on first boot with a fresh volume) and copies this
  file into `plugin-config-data/` if absent. Enables true wind (`environment.wind.speedTrue`,
  `angleTrueWater`, `directionTrue`), ground wind, and VMG (`performance.velocityMadeGood`).
  Output carries `$source` `derived-data`.
- Bench runs add `-f compose.pi.sample.yml` for the `--sample-n2k-data` provider (no CAN
  hardware / no recorded log needed).

Bench bring-up:
```bash
# from repo root:
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build
# replay a recorded boat log instead of sample data:
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
canplayer -I recorded.candump vcan0=can0
```

Inspect plugins / true wind:
```bash
curl -s localhost:3010/skServer/plugins | python3 -m json.tool | grep -i derived
curl -s localhost:3010/signalk/v1/api/vessels/self/environment/wind/speedTrue/value
```
