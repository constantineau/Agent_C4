# pi/ — onboard boat computer (Raspberry Pi 4 + PICAN-M)

The Pi is a **deploy target, not a dev host.** Claude Code edits this directory on the VPS;
`deploy/push_pi.sh` rsyncs it to the Pi over Tailscale + SSH and restarts services there.

Stack (brief §4):

| Layer        | Component                         | Role |
|--------------|-----------------------------------|------|
| OS           | Raspberry Pi OS Lite (64-bit)     | headless |
| CAN          | SocketCAN (`can0`) + can-utils    | raw N2K frames |
| Dev CAN      | **`vcan0`** + `canplayer`         | replay recorded N2K logs on the bench — no boat |
| Data server  | Signal K server                   | N2K → normalized JSON (WS + REST) |
| Derived data | signalk-derived-data              | true wind, VMG, current set/drift |
| Local archive| SQLite on the Pi (default)        | full-resolution onboard log |
| Uplink       | `uplink/uplink.py` (systemd)      | 15-s aggregates → VPS; disk-backed queue replays on link loss |
| Remote admin | Tailscale                         | SSH through CGNAT |

**Portability rule:** the ONLY bench↔boat difference is `CAN_IFACE` (`vcan0` vs `can0`).
Set it once (env / config); everything else is identical.

## Phase status
- **Phase 1 — built.** Signal K (`compose.pi.yml` + `signalk/settings.template.json`) and
  `uplink/uplink.py` are containerized and verified end-to-end on the bench with sample
  N2K data (Signal K → uplink → ingestion → TimescaleDB → agent). Run with:
  `docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build` (from repo root).
- **uplink** subscribes to the Signal K WS delta stream, maps SK paths (SI) → our channels
  (kn/deg/m), builds 15-s aggregates (circular mean for compass headings), and POSTs to the
  ingestion API with a disk-backed store-and-forward queue.
- **Follow-ups:** enable `signalk-derived-data` for true wind (TWS/TWA/TWD), VMG, current;
  record an hour of `candump -l can0` dockside as the gold-standard `canplayer` fixture
  (Phase 2 local archive, Phase 3 outage backfill testing).

## Bench safety
During development the PICAN-M's 12 V terminals stay **disconnected** — power the Pi from
USB-C only. **Never power from both sides at once.**
