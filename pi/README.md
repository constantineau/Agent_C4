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
| Derived data | signalk-derived-data (auto-enabled)| true wind (TWS/TWA/TWD), VMG — `$source` `derived-data` |
| Local archive| `archiver/archiver.py` → SQLite   | full-resolution onboard log (every delta); `backfill.py` → VPS post-passage |
| Uplink       | `uplink/uplink.py` (systemd)      | 15-s aggregates → VPS; disk-backed queue replays on link loss |
| Remote admin | Tailscale                         | SSH through CGNAT |

**Portability rule:** the ONLY bench↔boat difference is `CAN_IFACE` (`vcan0` vs `can0`).
Set it once (env / config); everything else is identical.

**Coming (Phase 9 — the three-tier pivot):** the Pi also becomes the **onboard deterministic engine**
host — `routing/tactics/sails/polars/nav/fatigue` run here (the boat's own gear → legal in-race under
RRS 41, no LLM), and the iPad talks to the Pi in race mode. An optional **Jetson Orin Nano** companion
adds a local LLM (Qwen2.5-7B) for in-race chat. The cloud stays the between-races prep/debrief/learning
"C4 Performance Lab." See `docs/RRS41_COMPLIANCE.md` and `docs/ONBOARD_ENGINE_SCOPING.md`.

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
