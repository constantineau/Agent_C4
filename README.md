# Agent_C4 — SR33 AI Navigator

LLM-powered navigator / coach / long-term data archive for the SR33 racing yacht. Boat NMEA 2000 →
Raspberry Pi (Signal K + onboard engine) → iPad crew navigator over boat-local Wi-Fi in a race; full
telemetry pushed over Starlink to a cloud VPS (TimescaleDB + Claude/Opus agent) for between-races
analysis and the practice/cruising/debrief product.

> **A three-tier architecture (pivot 2026-06-17)**, driven by racing-rules compliance — RRS 41 forbids
> customized tactical advice computed *off-boat while racing*, so the boat's own computer does the
> in-race work:
> - **Tier 1 — onboard deterministic engine** on the Pi (routing/tactics/sails/polars/nav/fatigue —
>   Expedition-class, legal in-race, no LLM);
> - **Tier 2 — optional onboard LLM copilot** (Jetson Orin Nano, Qwen2.5-7B) — narrate + bounded
>   decision support;
> - **Tier 3 — cloud frontier Opus 4.8 = the C4 Performance Lab** — between-races strategy studio
>   (→ a pre-loaded playbook) + write-back learning; race-gated in a race.
>
> See `docs/RRS41_COMPLIANCE.md` (the why) and `docs/ONBOARD_ENGINE_SCOPING.md` (the Phase 9 build).
> **Shipped:** 9.0 data-access abstraction, 9.1 onboard engine (`pi/engine`), 9.2 server-side race
> gate + iPad onboard console (`pi/console`), and the C4 Performance Lab (`vps/lab`) with **Lab-0 race
> ingestion** (NOR/SI/SER → a reviewable RaceDefinition). Next: Course & Marks review + wiring, then
> Lab-1 (multi-model optimizer). 9.4 Orin LLM is on hold (no hardware yet).

- **DESIGN.md** — product design description: architecture + what's built vs. planned today.
- **CLAUDE.md** — operational runbook (ports, commands, deploy, conventions).
- Project brief (Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`) — the long-form why.

## Quick start (dev stack — no boat needed)

```bash
cp .env.example .env                              # fill ANTHROPIC_API_KEY for live agent + Lab ingestion
docker compose -f compose.dev.yml up -d --build   # Timescale + ingestion + agent + web + lab
bash vps/db/seed/seed_dev.sh                       # placeholder polars/waypoints + fake telemetry
```

- Web chat (iPad):     http://localhost:8090   (server-side shared password; dev pw `sr33-dev`)
- C4 Performance Lab:  http://localhost:8103   (prep/debrief; dev pw `lab-dev`)
- Ingestion docs:      http://localhost:8101/docs
- Agent docs:          http://localhost:8102/docs
- Live readout:        `curl -s localhost:8102/conditions | python3 -m json.tool`
- Helm fatigue:        `curl -s localhost:8102/fatigue | python3 -m json.tool`

The **onboard tier** runs in the Pi stack (below): the deterministic engine on **:8200** and the
race-day onboard console on **:8091**.

Without an `ANTHROPIC_API_KEY` the agent runs a deterministic, tool-grounded fallback so
the full pipeline works with no LLM. Add the key to switch on the real Claude tool-use loop.

The agent also computes a **helm fatigue index** (0–100): it watches steering quality
(heading/heel/apparent-wind variance, steering reversals) and boatspeed vs. polar against the
boat's own recent baseline, and recommends a crew rotation as the driver tires. It shows on the
web instrument strip, at `GET /fatigue`, and via the `get_fatigue` agent tool. See CLAUDE.md
("Helm fatigue index") and DESIGN.md §5.

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
  agent/      Claude tool-use loop + SQL-backed tools + helm fatigue index + WebSocket chat
  web/        iPad navigator UI (nginx): day/night, sail dial, course plot, tactics, routing
  db/         schema/migrations + dev seed
shared/    units + agent tool contracts
deploy/    deploy_prod.sh, push_pi.sh
compose.{dev,prod}.yml
```

## Status

- **Phases 0–6 — done & bench-verified.** Cloud pipeline (ingestion → TimescaleDB → Claude
  tool-use agent → WebSocket chat), Pi stack (Signal K + uplink + full-res archive, store-and-forward,
  true wind via `signalk-derived-data`), the iPad navigator UI (day/night, sail dial, course plot,
  navigator, tactics, routing), and alerting + on-demand summarizer/debrief + polar mining.
- **Phase 7 — started.** Server-side shared-password web auth + TLS scaffolding (bench-verified);
  remaining: prod deploy + domain/TLS + 48-h soak + the RRS 41 review (done — see below).
- **Phase 9 — the three-tier pivot, in progress.**
  - **9.2 server-side race gate — ✅ shipped & bench-verified.** In a race the cloud agent + advice
    endpoints withhold tactical/routing/polar/sail/fatigue/navigation (RRS 41) and log every refusal;
    safety + own-instrument data + verbatim common data still flow. Fail-closed.
  - **9.0/9.1 onboard engine — next.** Relocate the deterministic engine to the Pi (legal in-race, no LLM).
  - **9.4 Orin Nano LLM copilot** (HW arriving) and the **C4 Performance Lab** (Lab-1→4: GRIB/buoy
    ingestion, strategy studio → playbook, onboard executor, write-back learning) follow.
  - Design in `docs/ONBOARD_ENGINE_SCOPING.md`; the *why* in `docs/RRS41_COMPLIANCE.md`.

The agent runs the real Claude tool-use loop when `ANTHROPIC_API_KEY` is set, else a deterministic
tool-grounded fallback. See **DESIGN.md** for built-vs-planned and **CLAUDE.md** for the phased plan
and open items.
