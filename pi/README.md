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
- Phase 1–3 live here and are **not built yet**. `uplink/uplink.py` is a runnable skeleton
  with the store-and-forward shape sketched; the Signal K subscription + N2K decode are TODO.
- Develop against simulated data first: `vcan0` + replayed N2K logs (or Signal K
  `--sample-n2k-data`). First dockside visit: record an hour of `candump can0` as the
  gold-standard bench fixture.

## Bench safety
During development the PICAN-M's 12 V terminals stay **disconnected** — power the Pi from
USB-C only. **Never power from both sides at once.**
