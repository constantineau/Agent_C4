# Agent_C4 — SR33 AI Navigator

LLM-powered navigator/coach/data-archive for the SR33 racing yacht. Boat NMEA 2000 →
Raspberry Pi (Signal K) → 15-s telemetry aggregates pushed over Starlink → this VPS
(TimescaleDB + Claude-API agent) → mobile web chat for the crew.

Full project brief: Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`.
This file is the operational summary (brief §§1–8); read the doc for full context.

## Architecture (two halves, linked over Starlink)

- **Boat:** NMEA 2000 backbone → Pi 4 + PICAN-M HAT. Signal K decodes N2K → JSON;
  full-resolution local archive; a Python uplink service POSTs 15-s aggregates to the VPS.
  Orca Core/app stay unchanged — the Pi is a silent additional listener.
- **Cloud (this VPS):** nginx+TLS → FastAPI ingestion API → TimescaleDB → agent service
  (Claude tool-use loop, alerting, summarizer) → web app.

**Design principles:** push-only from the boat (Starlink CGNAT, no inbound; admin via
Tailscale). Boat is source of truth (link outage loses nothing). The LLM never sees raw
NMEA — it reads facts through SQL-backed tools.

## Repo layout (monorepo)

```
pi/                 Signal K config, vcan/systemd units, uplink + full-res archiver
vps/ingestion/      FastAPI ingestion API (token-auth, writes batches to TimescaleDB)
vps/agent/          Claude tool-use service + alerting + summarizer + WebSocket chat
vps/web/            mobile web chat app (nginx static)
vps/db/             TimescaleDB schema + migrations + fake-data seed
shared/             data schemas, tool contracts, units
deploy/             scripts: deploy vps→prod, push pi→Pi over Tailscale
compose.dev.yml     dev stack (run by hand during sessions)
compose.prod.yml    prod stack (managed, leave alone)
```

## Isolation scheme — prod/dev on ONE box

Two Docker Compose projects, **separate ports and separate databases** (`sr33_prod` vs
`sr33_dev`). Dev work can never corrupt the production race archive. Git mirrors this:
develop on `dev`, merge to `main`, deploy `main` to prod via `deploy/`.

**Portability rule:** the ONLY difference between bench and boat is the CAN interface
name — `vcan0` (bench) vs `can0` (boat). Single config value (`CAN_IFACE`). Everything
else identical. Develop against simulated data (vcan0 + replayed N2K logs or
`--sample-n2k-data`) so the whole pipeline is testable with no boat.

## Dev stack — ports & commands

Ports chosen to avoid conflicts with the other apps on this VM (DreamCRM, racertracer):

| Service     | Container port | Host (dev) |
|-------------|----------------|------------|
| TimescaleDB | 5432           | **5433**   |
| ingestion   | 8000           | **8101**   |
| agent       | 8000           | **8102**   |
| web         | 80             | **8090**   |
| Signal K    | 3010 (host net)| **3010**   |

```bash
cd ~/Agent_C4
cp .env.example .env                       # fill ANTHROPIC_API_KEY when doing agent work
docker compose -f compose.dev.yml up -d --build
docker compose -f compose.dev.yml ps
python3 vps/db/seed/fake_telemetry.py      # POST fake 15-s aggregates through ingestion
# web app:        http://localhost:8090
# ingestion docs: http://localhost:8101/docs
# agent docs:     http://localhost:8102/docs
docker compose -f compose.dev.yml down     # (add -v to wipe the dev DB volume)
```

### Pi stack (Signal K + uplink) — bench or boat

Same `compose.pi.yml` runs on the VPS bench (`CAN_IFACE=vcan0`) and the real Pi
(`CAN_IFACE=can0`). Signal K on **:3010** (host networking, to read the host CAN iface).
`vcan0` on the VPS is a persistent systemd service (`vcan0.service`).

```bash
# bench with built-in sample N2K data (no boat/log needed):
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build
docker logs -f sr33-pi-uplink-1                 # 15-s aggregates POSTing to ingestion
docker logs -f sr33-pi-archiver-1               # full-res rows landing in onboard SQLite
docker compose -f compose.pi.yml down

# bench replaying a recorded log:  bash pi/bench/replay.sh pi/logs/<log>  then compose.pi.yml up
# on the Pi:  CAN_IFACE=can0 VPS_URL=https://nav... docker compose -f compose.pi.yml up -d
```

Notes: true wind (TWS/TWA/TWD) + VMG come from the `signalk-derived-data` plugin, which the
`signalk-derived` init service installs + enables into the config volume automatically (config
`pi/signalk/derived-data.json`; output `$source` is `derived-data`). Signal K port 3010 avoids
DreamCRM's :3000 on this VM.

**Full-resolution onboard archive (Phase 2):** the `archiver` service is a *second*,
independent Signal K subscriber that records **every** delta at full resolution to a durable
local SQLite DB (`sk_archive` named volume, WAL + `synchronous=FULL`) — schema mirrors the
cloud `telemetry_raw`. It owns its own subscription and crash-safe store, so a crashed uplink
or a dropped link never costs archived data ("link outage loses nothing"). `pi/archiver/
backfill.py` pushes the full-res log to the cloud `/ingest/raw` post-passage; it's resumable
via a `sync_state` cursor (re-runs send only new rows). See `pi/archiver/README.md`.

## Phased build (each phase has a clear exit test)

| Phase | Deliverable | Exit test |
|-------|-------------|-----------|
| **0** | Repo + dev compose + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; fake data loads |
| 1 | Pi base + PICAN-M + vcan0 + Signal K | sample N2K flows; Signal K dashboard populated |
| **2** ✅ | Pi local archive | full-res capture verified on bench; survives reboot; backfill lands in cloud `telemetry_raw` |
| **3** ✅ | Ingestion + uplink store-and-forward | forced-outage test passed: batches queue to a named volume, survive a reboot mid-outage, drain with no loss |
| 4 | Agent core + SQL tools | accurate answers on conditions/perf/AIS vs live dev data |
| 5 | Web app (strip, quick actions, night mode, shared pw) | full practice sail used without instruction |
| 6 | Alerting + summarizer + polar tooling | acceptable alert false-positive rate over 2 practice sails |
| 7 | Prod stack + deploy + rules review + soak | NOR compliance determined; 48-h unattended soak passes |

**Current status:** Phases 0–4 built and bench-verified. Phase 1 (Signal K + uplink) end-to-end;
Phase 2 (full-res onboard archive + backfill); Phase 3 uplink store-and-forward (disk-backed
queue on a named volume — forced-outage test passed, survives reboot mid-outage, drains with no
loss); Phase 4 agent runs the *real* Claude tool-use loop (`vps/agent/app/agent.py`, not stubbed)
with the boat-speed gospel + per-source skepticism + source priority/failover. True wind/VMG now
flow via the auto-enabled `signalk-derived-data` plugin. Remaining: Phase 5 web app (only a
static page so far), Phase 6 alerting/summarizer, Phase 7 prod/soak; still owed — a real
`candump -l can0` replay fixture (the canned sample stands in for now).

## Data paradigm — collect everything, per source

Live telemetry uses the **collect-everything** model: the uplink forwards *every* Signal K
`(source, path)` reading — all sensors, including redundant ones — to ingestion `/ingest/raw`,
stored in **`telemetry_raw(time, source, path, value)`**. The agent's `get_current_conditions`
returns every quantity from every source with freshness + a disagreement flag; `get_sources`
lists active sensors with curated reliability (`source_notes`). The agent is prompted to be
skeptical: cross-check redundant sources, flag disagreement/stale/uncalibrated, never trust a
lone value. Migration `002_telemetry_raw.sql`; the older wide `telemetry` table is legacy.

**Source priority + failover (migration 003):** `source_priority` ranks a preferred source
per channel (e.g. Orca for heel/true-wind, gWind masthead for apparent, 24xd GPS for
position). `get_current_conditions` adds a `preferred` reading per channel and automatically
fails over to the next rank when the preferred source is stale (>45 s) / absent, setting
`fell_back=true` so the agent announces it's on a backup. All sources stay visible — priority
only picks the lead + fallback order. Matchers are `$source` substrings (refine on real bus).

## Helm fatigue index

A 0–100 index that flags a tiring driver and recommends a crew rotation, on the principle
that a tired helm both **wanders** (more steering variance) and goes **slower** vs. the
boat's own potential. `vps/agent/app/fatigue.py` blends several "tells" — heading instability
(circular stdev), steering-reversal rate, heel instability, AWA wander **de-trended by TWD**
(so a shifty breeze isn't blamed on the driver), and boatspeed deficit vs. the polar — each
scored as a **recent window (8 min) vs. the boat's own trailing baseline (~40 min)**.

Key design choices:
- **Anonymous current-helm.** No driver identity; baselining against the boat's own recent
  steering auto-normalises for sea state, breeze, and skill, and needs zero crew input. The
  signal is *degradation within a stint*, not an absolute number.
- **Multi-signal composite with floors** so one spike can't trip it and a very tight baseline
  can't make normal wander look catastrophic; `rotate_now` (≥80) effectively needs more than
  one component elevated. Levels: `fresh` <35, `watch` <60, `rotate_soon` <80, `rotate_now`.
- **Maneuver-aware:** samples during high heading-rate turns (tacks/gybes, >8°/s) are excluded.
- Surfaced as the **Fatigue** cell on the web strip (`get_strip` adds `fatigue`/`fatigue_level`),
  the **`GET /fatigue`** endpoint, and the **`get_fatigue`** agent tool (the agent leads with the
  index + level and relays the rotation call). Cached ~20 s so the 5-s strip poll is cheap.
- **v1 limits / tuning:** not wind-strength normalised beyond the baseline (a fast breeze-build
  can read high — caveat it); thresholds/weights (`FATIGUE_*` env) are first-cut and meant to be
  tuned against **real race archives** (the Phase 2 full-res log is the training set). No stored
  target *heel* yet, so heel uses stability not error-vs-target.

## Database safety

Never run destructive DB ops or migrations against `sr33_prod` without explicit go-ahead.
Dev DB (`sr33_dev`) is disposable. Keep local `.env` out of git (it's gitignored); copy it
aside before any risky branch operation.

## Open items still owed (brief §9 — don't guess)

domain name · VPS specs confirm · ~~Anthropic API key~~ (done) · ~~SR33 polar data~~ (done —
ORC Speed Guide in `vps/agent/knowledge/`) · race route waypoints · Starlink/Tailscale on Pi ·
Pi archive (SQLite default) · crew scale + Grafana? · GRIB source · boat-install date.

**Boat-speed gospel:** the SR33 "C4" ORC Speed Guide lives in `vps/agent/knowledge/`
(`C4_boatspeed_gospel.md` = verbatim cert; `sr33_speed_guide.md` = distilled Best-Performance
polar + per-row optimal sail + per-TWS sail plan, loaded into the agent's cached system
context; `polars_sr33.sql` = real polars for the DB). The agent advises sail selection and
crossovers/peels from the sail plan. Regenerate after a cert update:
`python3 vps/agent/knowledge/build_speed_guide.py`.

## Racing-rules caveat (RRS 41 / Bayview Mackinac NOR)

Real-time shore tactical/routing advice may be prohibited "outside help." Confirm with the
race committee before race use. Passive collection + practice/delivery/debrief use is fine;
an all-onboard fallback (agent on the Pi, no shore loop) is feasible if required.
