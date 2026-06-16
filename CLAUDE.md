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
pi/                 Signal K config, vcan/systemd units, uplink service
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
docker compose -f compose.pi.yml down

# bench replaying a recorded log:  bash pi/bench/replay.sh pi/logs/<log>  then compose.pi.yml up
# on the Pi:  CAN_IFACE=can0 VPS_URL=https://nav... docker compose -f compose.pi.yml up -d
```

Notes: true wind (TWS/TWA/TWD) needs the `signalk-derived-data` plugin (not yet enabled —
those channels are null until then). Signal K port 3010 avoids DreamCRM's :3000 on this VM.

## Phased build (each phase has a clear exit test)

| Phase | Deliverable | Exit test |
|-------|-------------|-----------|
| **0** | Repo + dev compose + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; fake data loads |
| 1 | Pi base + PICAN-M + vcan0 + Signal K | sample N2K flows; Signal K dashboard populated |
| 2 | Pi local archive | day-length replay captured at full res; survives reboot |
| 3 | Ingestion + uplink store-and-forward | telemetry on VPS; forced 30-min outage backfills cleanly |
| 4 | Agent core + SQL tools | accurate answers on conditions/perf/AIS vs live dev data |
| 5 | Web app (strip, quick actions, night mode, shared pw) | full practice sail used without instruction |
| 6 | Alerting + summarizer + polar tooling | acceptable alert false-positive rate over 2 practice sails |
| 7 | Prod stack + deploy + rules review + soak | NOR compliance determined; 48-h unattended soak passes |

**Current status:** Phase 0 in progress (scaffold + dev stack + schema + functional
ingestion/tools stubs + fake-data seed). Agent's Claude tool-use loop is stubbed (Phase 4).

## Database safety

Never run destructive DB ops or migrations against `sr33_prod` without explicit go-ahead.
Dev DB (`sr33_dev`) is disposable. Keep local `.env` out of git (it's gitignored); copy it
aside before any risky branch operation.

## Open items still owed (brief §9 — don't guess)

domain name · VPS specs confirm · **Anthropic API key** · SR33 polar data · race route
waypoints · Starlink/Tailscale on Pi · Pi archive (SQLite default) · crew scale + Grafana? ·
GRIB source · boat-install date.

## Racing-rules caveat (RRS 41 / Bayview Mackinac NOR)

Real-time shore tactical/routing advice may be prohibited "outside help." Confirm with the
race committee before race use. Passive collection + practice/delivery/debrief use is fine;
an all-onboard fallback (agent on the Pi, no shore loop) is feasible if required.
