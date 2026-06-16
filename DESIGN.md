# Agent_C4 — Product Design Description

**Product:** SR33 AI Navigator
**Vessel:** SR33 sailing yacht — distance racing (Bayview Mackinac; Port Huron → Mackinac Island)
**Status:** Phase 0 complete (cloud pipeline scaffolded & running); Phase 1 in progress (Pi bench on the VPS)
**Last updated:** 2026-06-16

This document describes *what the product is and how it is built today*. The original
project brief (Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`) holds the long-form
rationale; `CLAUDE.md` holds the operational runbook. This file is the bridge: the design
as it actually exists in the repo right now, plus what is still stubbed.

---

## 1. Purpose

Instrument the SR33's existing NMEA 2000 network and connect it, over onboard Starlink, to
a cloud-hosted LLM agent serving three roles:

1. **Navigator** — real-time, telemetry-grounded answers on conditions, performance,
   traffic, and progress to marks.
2. **Coach** — continuous comparison of boatspeed/angles against the boat's polars;
   proactive alerts for meaningful wind shifts and AIS convergence.
3. **Long-term data center** — a full-resolution archive of every race, delivery, and
   practice sail for debriefs, empirical polar development, and season-over-season analysis.

The system is **additive and non-invasive**: the existing Orca Core, Orca app, and
instruments are unchanged; the new computer is a silent extra listener on the same backbone.

---

## 2. System architecture

Two halves — **boat** and **cloud (VPS)** — joined by a one-way telemetry push and a
two-way chat channel, both over Starlink.

```
        ON THE BOAT                                  CLOUD (single VPS)
 ┌───────────────────────────┐              ┌──────────────────────────────────┐
 │ NMEA 2000 backbone         │              │ ingestion API (FastAPI)           │
 │  wind·STW·GPS·AIS·hdg·depth│              │   token-auth, writes batches      │
 │      │            │        │              │            │                      │
 │      ▼            ▼        │   Starlink   │            ▼                      │
 │  Orca Core    Pi 4 +      │  ─────────►  │  TimescaleDB (Postgres)           │
 │  (unchanged)  PICAN-M     │  telemetry   │   hypertables + metadata          │
 │               Signal K    │  push (HTTPS │            │                      │
 │               local log   │  store-&-fwd)│            ▼                      │
 │               uplink ─────┼──────────────┤  agent service (Claude tool-use)  │
 │                           │              │   SQL tools · alerts · summarizer │
 │  crew phones ◄────────────┼──────────────┤            │  WebSocket           │
 │  (web chat)               │  chat+alerts │            ▼                      │
 └───────────────────────────┘  (WebSocket) │  web app (nginx, mobile-first)    │
                                             └──────────────────────────────────┘
```

**Design principles**

- **Push-only from the boat.** Starlink is carrier-grade NAT — no inbound connections. All
  boat→cloud traffic is boat-initiated; remote admin uses Tailscale to traverse CGNAT.
- **The boat is the source of truth.** Full-resolution data is logged locally on the Pi;
  the cloud receives 15-s aggregates live and full logs after each passage. A Starlink
  outage loses nothing (disk-backed store-and-forward queue).
- **The LLM never sees raw NMEA.** The agent retrieves facts through SQL-backed tools
  against TimescaleDB.
- **One CAN_IFACE switch.** The only difference between bench and boat is the CAN interface
  name — `vcan0` (bench, on the VPS) vs `can0` (boat). Everything else is identical.

---

## 3. Components — built vs. planned

| Component | Where | Status | Notes |
|-----------|-------|--------|-------|
| TimescaleDB schema | `vps/db/` | **built** | telemetry + ais_targets hypertables; polars, waypoints, race_info, crew_notes, agent_summaries |
| Ingestion API | `vps/ingestion/` | **built** | FastAPI, bearer-token `/ingest`, `/health`; writes batches |
| Agent — SQL tools | `vps/agent/app/tools.py` | **built** | all 7 tools query the DB and return real data |
| Agent — chat loop | `vps/agent/app/agent.py` | **built (key-gated)** | Claude tool-use loop runs when `ANTHROPIC_API_KEY` is set; otherwise a deterministic tool-grounded fallback |
| Agent — WebSocket | `vps/agent/app/main.py` | **built** | shared crew thread; `/conditions` REST mirror |
| Web app | `vps/web/` | **built** | mobile-first chat: instrument strip, quick actions, night mode; password gate is a Phase-0 stub |
| Dev/prod compose | `compose.{dev,prod}.yml` | **built** | isolated stacks, separate DBs (`sr33_dev`/`sr33_prod`) and ports |
| Fake-data seed | `vps/db/seed/` | **built** | posts realistic 15-s aggregates through the ingestion API + placeholder polars/waypoints/AIS |
| Pi bench (vcan0) | `pi/bench/` | **built** | virtual CAN on the VPS: setup, canplayer replay, cangen smoke traffic |
| Pi uplink | `pi/uplink/uplink.py` | **skeleton** | aggregation + store-and-forward shape wired; Signal K subscription + N2K decode TODO (Phase 3) |
| Signal K config | `pi/signalk/` | **planned** | Phase 1 — CAN provider bound to `$CAN_IFACE` |
| Alerting / summarizer | `vps/agent/` | **planned** | Phase 6 |
| Forecast tool | `vps/agent/app/tools.py` | **stub** | `fetch_forecast` returns "not wired" pending §9 GRIB source |
| Deploy scripts | `deploy/` | **built (untested)** | `deploy_prod.sh`, `push_pi.sh` (Tailscale) |

---

## 4. Data model

**Time-series (TimescaleDB hypertables)**
- `telemetry` — one wide row per 15-s aggregate: `aws awa tws twa twd stw sog cog heading
  lat lon depth`, keyed `(boat_id, time)`. Stored in sailing units (kn, deg, m).
- `ais_targets` — one row per target observation: position, SOG/COG, range, bearing,
  CPA, TCPA.

**Metadata (plain tables)**
- `polars` — target boatspeed/VMG by `(tws, twa)` bucket.
- `waypoints` — route marks in sequence.
- `race_info` — race name/route/start.
- `crew_notes` — timeline observations (`log_note`).
- `agent_summaries` — periodic agent-written conditions/performance digests (compact
  long-term memory).

---

## 5. The agent

**Mechanism:** Claude API with tool use. Each crew message runs a bounded reasoning loop:
the model calls SQL-backed tools, then composes a grounded reply. Tool contracts live in
`shared/tool_contracts.py` (single source of truth shared by the loop and any client).

**Tools (all implemented against the DB):** `get_current_conditions`, `get_history`,
`get_polar_target`, `get_ais_targets`, `get_route_status`, `fetch_forecast` (stub),
`log_note`.

**Grounding rules (system prompt):** never invent telemetry; always report data freshness
and caveat stale answers; be VHF-brief (crew read on a phone, at night, often wet).

**Fallback:** with no API key, a deterministic responder keyword-routes to the right tool
and formats the result — so the whole pipeline is demoable with no LLM and no boat.

**Planned (Phase 6):** a 30–60-min summarizer job and a conservative alerting loop
(sustained wind shift, polar % depressed > N min, AIS CPA inside guard radius, stale
telemetry). Alerts are deliberately rare so the crew listens.

---

## 6. Web interface

Mobile-first single page over a WebSocket to the agent. Single shared boat password
(server-side, TLS — currently a Phase-0 client-side stub). One shared crew thread so every
watch sees the same history. Live instrument strip pinned to the header (STW, TWA, TWS,
polar %, data-freshness). Quick-action buttons for the five common questions. True night
mode (red-on-black). Large touch targets, high contrast.

---

## 7. Environments & deployment

One VPS, two Docker-isolated stacks with **separate databases** so dev can never corrupt
the production race archive:

- **dev** (`compose.dev.yml`, DB `sr33_dev`, ports 5433/8101/8102/8090) — run by hand
  during sessions; this is where the Pi bench (`vcan0`) and fake data feed.
- **prod** (`compose.prod.yml`, DB `sr33_prod`) — managed/auto-restart; touched only to deploy.

Git mirrors this: develop on `dev`, merge to `main`, deploy `main` via
`deploy/deploy_prod.sh`. The Pi is a deploy target, not a dev host —
`deploy/push_pi.sh` rsyncs `pi/` to the boat computer over Tailscale and restarts services.

---

## 8. Racing-rules compliance (RRS 41 / Bayview Mackinac NOR)

Real-time tactical/routing advice from a shore agent **may be prohibited outside
assistance**. Before race use: review the current NOR/SIs and, if ambiguous, ask the race
committee. Absent approval, restrict in-race use to passive collection + crew logging; use
full coaching for practice, deliveries, and debriefs. An all-onboard fallback (agent on the
Pi, no shore loop) is feasible if required. Data collection and non-racing use are unrestricted.

---

## 9. Open items (owner input needed)

Domain name · VPS specs confirm · **Anthropic API key** (flips the agent from fallback to
live) · SR33 polar data (currently synthetic placeholder) · race route waypoints
(placeholder) · Starlink/Tailscale on the Pi · Pi local archive (SQLite default) · crew
scale + optional Grafana · GRIB/forecast source · boat-install date.

---

## 10. Roadmap (phased; each phase has an exit test)

| Phase | Deliverable | Exit test | State |
|-------|-------------|-----------|-------|
| 0 | Repo + dev stack + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; data loads | ✅ done |
| 1 | Pi base + CAN bench + Signal K | sample N2K flows; Signal K dashboard populated | 🔧 bench done; Signal K next |
| 2 | Pi local archive | day-length replay at full res; survives reboot | ⬜ |
| 3 | Ingestion + uplink store-and-forward | forced 30-min outage backfills cleanly | ⬜ |
| 4 | Agent core + SQL tools (live LLM) | accurate answers vs live dev data | ⬜ (tools done; needs API key) |
| 5 | Web app polish + real auth | full practice sail used without instruction | ⬜ |
| 6 | Alerting + summarizer + polar tooling | acceptable false-positive rate over 2 sails | ⬜ |
| 7 | Prod + deploy + rules review + soak | NOR compliance determined; 48-h soak passes | ⬜ |

## 11. Future work

Empirical polar generation from logs; automated post-race debrief reports; onboard-only
agent mode for rules-restricted racing; engine/battery PGN capture for deliveries; Telegram
bot as a second interface; shore-crew read-only tracker.
