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

## Developing the Pi software on the VPS (no boat)

The Pi software is developed here against a **virtual CAN interface** (`vcan0`) in place of
the boat's `can0` — the single `CAN_IFACE` switch from the portability rule. On this VPS
`vcan0` is provided by a persistent systemd service (`vcan0.service`); on a fresh host:

```bash
bash pi/bench/setup_vcan.sh        # create + up vcan0 (sudo; needs vcan module + can-utils)
bash pi/bench/gen_traffic.sh       # smoke-test frames, OR:
bash pi/bench/replay.sh pi/logs/candump-<date>.log   # replay a recorded boat log (can0->vcan0)
```

See `pi/bench/README.md` for the full bench workflow.

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
- **Phase 1 — in progress.** Pi bench on the VPS via `vcan0` + `can-utils` (replay/generate
  N2K frames). Next: wire Signal K into the pipeline.

Agent's Claude tool-use loop is wired but gated on an `ANTHROPIC_API_KEY` (set it to go
live; Phase 4). See **DESIGN.md** for built-vs-planned and **CLAUDE.md** for the phased plan
and open items.
