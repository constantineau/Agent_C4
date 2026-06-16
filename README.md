# Agent_C4 — SR33 AI Navigator

LLM-powered navigator / coach / long-term data archive for the SR33 racing yacht.
Boat NMEA 2000 → Raspberry Pi (Signal K) → 15-s telemetry over Starlink → cloud VPS
(TimescaleDB + Claude-API agent) → mobile web chat for the crew.

See **CLAUDE.md** for the full operational guide and the project brief
(Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`) for the why.

## Quick start (dev stack — no boat needed)

```bash
cp .env.example .env                              # fill ANTHROPIC_API_KEY for live agent
docker compose -f compose.dev.yml up -d --build   # Timescale + ingestion + agent + web
bash vps/db/seed/seed_dev.sh                       # placeholder polars/waypoints + fake telemetry
```

- Web chat:        http://localhost:8090  (password gate is a Phase-0 stub)
- Ingestion docs:  http://localhost:8101/docs
- Agent docs:      http://localhost:8102/docs
- Live readout:    `curl -s localhost:8102/conditions | python3 -m json.tool`

Without an `ANTHROPIC_API_KEY` the agent runs a deterministic, tool-grounded fallback so
the full pipeline works with no LLM. Add the key to switch on the real Claude tool-use loop.

## Layout

```
pi/        onboard Pi: Signal K config, uplink service (systemd), CAN_IFACE switch
vps/
  ingestion/  FastAPI token-auth ingest -> TimescaleDB
  agent/      Claude tool-use loop + SQL-backed tools + WebSocket chat
  web/        mobile-first chat app (nginx)
  db/         schema/migrations + dev seed
shared/    units + agent tool contracts
deploy/    deploy_prod.sh, push_pi.sh
compose.{dev,prod}.yml
```

## Status — Phase 0 (scaffold)

Repo + dev compose stack + schema + functional ingestion + SQL tools + web app +
fake-data seed. Agent's Claude loop is wired but gated on an API key (Phase 4). See the
phased plan and open items in CLAUDE.md.
