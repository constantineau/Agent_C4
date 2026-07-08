# pi/ — onboard boat computer (Raspberry Pi 4 + PICAN-M)

The Pi is a **deploy target, not a dev host.** Claude Code edits this directory on the VPS;
deploy over Tailscale SSH: on the Pi, `cd ~/Agent_C4 && git pull && docker compose -f
compose.pi.yml up -d --build` (the whole stack runs as compose containers).

**▶ Bringing up a fresh Pi from a blank SD card?** Follow **[`SETUP.md`](SETUP.md)** — OS choice,
headless flash, Docker/Tailscale, PICAN-M `can0` bring-up (`systemd/sr33-can0.service`), and
deploying the stack.

Stack (brief §4):

| Layer        | Component                         | Role |
|--------------|-----------------------------------|------|
| OS           | Raspberry Pi OS Lite (64-bit)     | headless |
| CAN          | SocketCAN (`can0`) + can-utils    | raw N2K frames |
| Dev CAN      | **`vcan0`** + `canplayer`         | replay recorded N2K logs on the bench — no boat |
| Data server  | Signal K server                   | N2K → normalized JSON (WS + REST) |
| Derived data | signalk-derived-data (auto-enabled)| true wind (TWS/TWA/TWD), VMG — `$source` `derived-data` |
| Local archive| `archiver/archiver.py` → SQLite   | full-resolution onboard log (every delta); `backfill.py` → VPS post-passage |
| Uplink       | `uplink/uplink.py` (systemd)      | 15-s aggregates → VPS; disk-backed queue replays on link loss |
| Onboard engine | `engine/` (Tier 1, 9.1) → :8200 | the deterministic modules run here from the boat's own data (`OnboardSource`) — no LLM, legal in-race |
| Race console | `console/` (9.2) → :8091          | the iPad app served from the Pi, pointed only at the engine over boat-local Wi-Fi (no cloud) |
| Remote admin | Tailscale                         | SSH through CGNAT |

**Portability rule:** the ONLY bench↔boat difference is `CAN_IFACE` (`vcan0` vs `can0`).
Set it once (env / config); everything else is identical.

**Phase 9 — the three-tier pivot (built):** the Pi is now also the **onboard deterministic engine**
host (`engine/`, 9.1) — `routing/tactics/sails/polars/nav/fatigue` run here from the boat's own data
(the boat's own gear → legal in-race under RRS 41, no LLM, :8200), and in race mode the iPad is served
from the Pi (`console/`, 9.2, :8091) and talks only to the engine. An optional **Jetson Orin Nano**
companion (Tier 2, 9.4) would add a local LLM (Qwen2.5-7B) for in-race chat — **on hold (no hardware
yet)**. The cloud stays the between-races prep/debrief/learning **C4 Performance Lab** (`vps/lab`). See
`pi/engine/README.md`, `docs/RRS41_COMPLIANCE.md`, and `docs/ONBOARD_ENGINE_SCOPING.md`.

## Phase status
- **Phase 1 — built.** Signal K (`compose.pi.yml` + `signalk/settings.template.json`) and
  `uplink/uplink.py` are containerized and verified end-to-end on the bench with sample
  N2K data (Signal K → uplink → ingestion → TimescaleDB → agent). Run with:
  `docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build` (from repo root).
- **uplink** subscribes to the Signal K WS delta stream, maps SK paths (SI) → our channels
  (kn/deg/m), builds 15-s aggregates (circular mean for compass headings), and POSTs to the
  ingestion API with a disk-backed store-and-forward queue (on the `sk_queue` named volume,
  so it survives a reboot mid-outage).
- **Phase 3 — store-and-forward verified.** Forced-outage bench test: the cloud endpoint was
  stopped, batches queued to `sk_queue`, the uplink container was restarted mid-outage (queue
  persisted), then the link was restored — the queue drained and every outage-window reading
  landed in the cloud `telemetry_raw` with original timestamps. No loss.
- **True wind / VMG** come from `signalk-derived-data`, installed + enabled into the config
  volume automatically by the `signalk-derived` init service (config `signalk/derived-data.json`;
  output `$source` is `derived-data`). On the boat this is one of several true-wind sources
  (Orca Core also publishes heel-compensated true wind) — collect-everything keeps them all.
- **Phase 2 — built.** `archiver/` is a second, independent Signal K subscriber that records
  **every** delta at full resolution to a durable local SQLite DB (`sk_archive` volume, WAL +
  `synchronous=FULL`); schema mirrors the cloud `telemetry_raw`. Verified on the bench:
  full-res capture, survives a full stack down/up (reboot), and `backfill.py` lands the log in
  the cloud `telemetry_raw` (resumable via a `sync_state` cursor). See `archiver/README.md`.
- **Follow-ups:** record an hour of `candump -l can0` dockside as the gold-standard `canplayer`
  fixture (replaces the canned sample for a true day-length archive/backfill soak).

## Bench safety
During development the PICAN-M's 12 V terminals stay **disconnected** — power the Pi from
USB-C only. **Never power from both sides at once.**
