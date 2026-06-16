# Signal K configuration (placeholder — Phase 1)

Signal K server config and plugin settings live here once Phase 1 starts. Planned:

- `settings.json` — server settings; CAN provider bound to `$CAN_IFACE` (vcan0 bench / can0 boat).
- `signalk-derived-data` plugin enabled (true wind, VMG, current set/drift).
- A `--sample-n2k-data` provider for bench runs with no CAN hardware.

Bench bring-up sketch:
```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
canplayer -I recorded.candump vcan0=can0   # replay a recorded boat log onto vcan0
```
