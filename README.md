# Agent_C4 — SR33 AI Navigator

LLM-powered navigator / coach / long-term data archive for the SR33 racing yacht.
Boat NMEA 2000 → Raspberry Pi (Signal K) → 15-s telemetry over Starlink → cloud VPS
(TimescaleDB + Claude-API agent) → mobile web chat for the crew.

- **DESIGN.md** — product design description: architecture + what's built vs. planned today.
- **CLAUDE.md** — operational runbook (ports, commands, deploy, conventions).
- Project brief (Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`) — the long-form why.

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

## The Pi software (Signal K + uplink) — runs on the VPS bench and the boat

The Pi stack is Docker too, so it's identical on the VPS bench and the real Pi — the only
difference is `CAN_IFACE` (`vcan0` bench / `can0` boat). Signal K decodes NMEA 2000 → the
uplink subscribes over WebSocket, builds 15-s aggregates, and POSTs to the ingestion API
(disk-backed store-and-forward survives link loss). Signal K runs on **:3010** (`:3000` is
taken on this VM).

```bash
# Bench, no boat log yet — feed Signal K its built-in sample N2K data:
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build
docker logs -f sr33-pi-uplink-1            # watch 15-s aggregates POST to the cloud

# Bench, replaying a real recorded log onto vcan0:
bash pi/bench/replay.sh pi/logs/candump-<date>.log
docker compose -f compose.pi.yml up -d --build

# On the actual Pi:
CAN_IFACE=can0 VPS_URL=https://nav.example.com docker compose -f compose.pi.yml up -d
```

`vcan0` on the VPS is provided by a persistent `vcan0.service`. See `pi/bench/README.md`
for the CAN bench (setup/replay/generate) and `pi/README.md` for the onboard stack.

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

## Status

- **Phase 0 — done.** Repo + dev compose stack + TimescaleDB schema + functional ingestion +
  SQL-backed agent tools + WebSocket chat + mobile web app + fake-data seed. Verified
  end-to-end (481 fake points → DB → live agent answers).
- **Phase 1 — done (core).** Pi stack containerized: Signal K (SocketCAN on `$CAN_IFACE`,
  port 3010) → uplink (WebSocket subscribe → 15-s aggregates → ingestion, with
  store-and-forward). Verified end-to-end on the bench with sample N2K data:
  Signal K → uplink → TimescaleDB → live agent answers. Follow-up: `signalk-derived-data`
  plugin for true wind (TWS/TWA/TWD) + record a real boat log for `canplayer` replay.

Agent's Claude tool-use loop is wired but gated on an `ANTHROPIC_API_KEY` (set it to go
live; Phase 4). See **DESIGN.md** for built-vs-planned and **CLAUDE.md** for the phased plan
and open items.
