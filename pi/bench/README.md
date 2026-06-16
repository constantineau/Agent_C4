# Bench — developing the Pi software on the VPS (no boat)

The Pi software is developed here on the VPS using a **virtual CAN interface** in place of
the boat's real bus. This is the `vcan0` side of the brief's portability rule: bench uses
`vcan0`, the boat uses `can0`, and that single `CAN_IFACE` value is the *only* difference.

## One-time / per-boot setup

```bash
bash pi/bench/setup_vcan.sh        # modprobe vcan + create & up vcan0 (idempotent, sudo)
```

Requires the `vcan` kernel module (`linux-modules-extra-$(uname -r)`) and `can-utils`,
both installed on this VPS. Verify:

```bash
ip -brief link show vcan0          # -> vcan0  UNKNOWN  <NOARP,UP,LOWER_UP>
candump vcan0 &                    # watch frames
cansend vcan0 123#DEADBEEF         # should appear in candump
```

> **Reboot note:** `vcan0` is **session-only** — it disappears on reboot. To persist it,
> enable a boot-time unit. A `/etc/systemd/system/vcan0.service` (oneshot: `modprobe vcan`
> + `ip link add … type vcan` + `ip link set up`) plus `echo vcan >
> /etc/modules-load.d/vcan.conf` does it — this is an admin action requiring explicit
> opt-in, so it's not enabled automatically. Until then, re-run `setup_vcan.sh` after a reboot.

## Feeding the bench with data

Two sources, in order of fidelity:

1. **Replay a recorded boat log** (gold standard — brief §8). First dockside visit:
   `candump -l can0` for ~1h, commit the log under `pi/logs/` (gitignored if large), then:
   ```bash
   bash pi/bench/replay.sh pi/logs/candump-<date>.log
   ```
   `canplayer` preserves original timing and remaps `can0 → vcan0`.

2. **Smoke-test traffic** (before any real log exists) — random 29-bit frames just to
   prove the substrate/candump/Signal K provider are wired:
   ```bash
   bash pi/bench/gen_traffic.sh
   ```
   For decodable PGNs without a log, use Signal K's built-in `--sample-n2k-data` provider
   (see `pi/signalk/`) instead.

## Where this plugs in (Phase 1+)

`vcan0` → Signal K server (CAN provider bound to `$CAN_IFACE`) → `pi/uplink/uplink.py`
subscribes to the Signal K WS, builds 15-s aggregates, and POSTs to the VPS ingestion API
— the same dev stack from `compose.dev.yml`. End result: the whole boat→cloud pipeline is
exercisable on the VPS with zero hardware.
