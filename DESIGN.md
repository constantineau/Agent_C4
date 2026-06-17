# Agent_C4 — Product Design Description

**Product:** SR33 AI Navigator
**Vessel:** SR33 sailing yacht — distance racing (Bayview Mackinac; Port Huron → Mackinac Island)
**Status:** Phases 0–4 built & bench-verified (cloud pipeline, Pi bench, full-res archive,
real Claude agent); true wind via signalk-derived-data; helm fatigue index added. Phase 5 web
app and Phase 6 alerting/summarizer still ahead.
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
| Boat-speed gospel | `vps/agent/knowledge/` | **built** | SR33 "C4" ORC Speed Guide; verbatim cert (`C4_boatspeed_gospel.md`) + distilled Best-Performance polar with per-row optimal **sail** + per-TWS **sail plan** (crossovers), loaded into the agent's cached context; agent advises sail changes/peels; `build_speed_guide.py` regenerates it |
| Polars (real data) | `vps/db/seed/polars_sr33.sql` | **built** | 126 real ORC polar points (TWS 4–24); replaces the synthetic placeholder |
| Agent — chat loop | `vps/agent/app/agent.py` | **built (key-gated)** | Claude tool-use loop runs when `ANTHROPIC_API_KEY` is set; otherwise a deterministic tool-grounded fallback |
| Agent — WebSocket | `vps/agent/app/main.py` | **built** | shared crew thread; `/conditions` REST mirror |
| Web app | `vps/web/` | **built** | mobile-first chat: instrument strip, quick actions, night mode; password gate is a Phase-0 stub |
| Dev/prod compose | `compose.{dev,prod}.yml` | **built** | isolated stacks, separate DBs (`sr33_dev`/`sr33_prod`) and ports |
| Fake-data seed | `vps/db/seed/` | **built** | posts realistic 15-s aggregates through the ingestion API + placeholder polars/waypoints/AIS |
| Pi bench (vcan0) | `pi/bench/` | **built** | virtual CAN on the VPS (persistent `vcan0.service`): setup, canplayer replay, cangen smoke traffic |
| Signal K | `compose.pi.yml` + `pi/signalk/` | **built** | official image, host-net, SocketCAN provider bound to `$CAN_IFACE`, port 3010; settings rendered from template at start |
| Pi uplink | `pi/uplink/uplink.py` | **built** | WebSocket subscribe → SI→units map → 15-s aggregates (circular mean for compass) → ingestion; disk-backed store-and-forward |
| signalk-derived-data | `pi/signalk/` | **planned** | true wind (TWS/TWA/TWD), VMG, current set/drift — follow-up so those channels populate |
| Alerting / summarizer | `vps/agent/` | **planned** | Phase 6 |
| Forecast tool | `vps/agent/app/tools.py` | **stub** | `fetch_forecast` returns "not wired" pending §9 GRIB source |
| Deploy scripts | `deploy/` | **built (untested)** | `deploy_prod.sh`, `push_pi.sh` (Tailscale) |

---

## 4. Data model

**Time-series (TimescaleDB hypertables)**
- `telemetry_raw` — **the primary live store (collect-everything paradigm).** One row per
  `(time, source, path, value)` — every Signal K path from *every* source, including
  redundant sensors, stored verbatim in SI with full provenance. New sensors/paths need no
  schema change. This is what the agent reasons over (cross-checking sources).
- `telemetry` — legacy wide single-value-per-channel table (kept; superseded by `telemetry_raw`).
- `ais_targets` — one row per target observation: position, SOG/COG, range, bearing, CPA, TCPA.

**Metadata adds**
- `source_notes` — curated reliability per sensor (`high`/`medium`/`needs-calibration`/
  `unreliable` + note); the agent reads it so it knows which sources may be uncalibrated.
- `source_priority` — preferred source order **per quantity** (rank 1 = lead, e.g. Orca for
  heel/true-wind, masthead for apparent wind, dedicated GPS for position). All sources are
  still kept; this only picks the default + automatic-failover order.

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

**Tools (all implemented against the DB):** `get_current_conditions` (multi-source — every
quantity from every reporting source, with per-source freshness + disagreement flag),
`get_sources` (active sensors + curated reliability), `get_fatigue` (helm fatigue index — see
below), `get_sail_advice` (sail-range + crossovers), `get_navigator` (next mark/ETA/laylines),
`get_tactics` (lifted-headed/favored-side/leverage), `get_route` (isochrone weather routing),
`get_history` (per-channel or raw path, optionally one source), `get_polar_target`,
`get_ais_targets`, `get_route_status`, `fetch_forecast` (Open-Meteo wind), `log_note`.

**Helm fatigue index (`get_fatigue`):** a 0–100 score that detects a tiring driver and
recommends a crew rotation, since a tired helm both *wanders* (more steering variance) and
sails *slower* than the boat's potential. `vps/agent/app/fatigue.py` blends heading instability,
steering-reversal rate, heel instability, AWA wander (de-trended by TWD so a shifty breeze isn't
mistaken for the driver), and boatspeed deficit vs. polar — each scored as a recent 8-min window
against the boat's own ~40-min trailing baseline. **Anonymous current-helm:** no driver identity;
baselining against the boat's own recent steering auto-normalises for conditions and skill and
needs no crew input — it measures *degradation within a stint*. A weighted composite with
per-component floors and maneuver exclusion (tacks/gybes dropped) yields levels
`fresh`/`watch`/`rotate_soon`/`rotate_now`; the agent leads with the index + level and relays the
rotation call. v1 isn't wind-strength normalised beyond the baseline (a fast breeze-build can read
high) and its thresholds are meant to be tuned against the Phase-2 full-resolution race archive.

**Grounding rules (system prompt):** never invent telemetry; always report data freshness
and caveat stale answers; be VHF-brief (crew read on a phone, at night, often wet).
**Sensor skepticism:** sources are redundant by design — cross-check them, flag
disagreement/stale/uncalibrated readings, prefer reliable sources, never present one number
as truth when sources conflict. The agent is the crew's sanity-check on the instruments.

**Fallback:** with no API key, a deterministic responder keyword-routes to the right tool
and formats the result — so the whole pipeline is demoable with no LLM and no boat.

**Planned (Phase 6):** a 30–60-min summarizer job and a conservative alerting loop
(sustained wind shift, polar % depressed > N min, AIS CPA inside guard radius, stale
telemetry). Alerts are deliberately rare so the crew listens.

---

## 6. Web interface

**iPad-landscape navigator companion** (Phase 5) — deliberately NOT an instrument repeater
(the boat already has instruments); the big numbers live behind an all-channels submenu.
Vanilla JS over nginx (`/api/*` + `/ws` proxy), no build step, offline-friendly. Elements:
**automatic day/night** from GPS-derived sunrise/sunset (manual override; night = red-on-black);
a **sail-range dial** (point-of-sail gauge with the J1/A2/A3/S2 zones, crossover/peel markers,
live TWA needle, crew "what's hoisted" selector flagging wrong sail); a **schematic course
plot** (boat/marks/legs/laylines/wind/track, north-up/course-up, no chart tiles); a
**Navigator** panel (next mark, ETA, leg type, layline call); a **tactical** read (lifted/headed,
favored side, leverage); and **weather routing** (isochrone optimal route on the polars through
an Open-Meteo wind forecast — ETA, tacks, recommended first tack, route overlay). A
**Race/Practice toggle** gates the tactical + routing layers in the UI for RRS 41. The
**helm fatigue index** shows in the top bar. One shared crew thread + chat to the agent. Single
shared boat password (server-side + TLS is still a client-side stub, lands with Phase 7).

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

Domain name · VPS specs confirm · ~~Anthropic API key~~ (done) · ~~SR33 polar data~~ (done —
real ORC Speed Guide) · race route waypoints (placeholder) · Starlink/Tailscale on the Pi ·
Pi local archive (SQLite default) · crew scale + optional Grafana · GRIB/forecast source ·
boat-install date.

---

## 10. Roadmap (phased; each phase has an exit test)

| Phase | Deliverable | Exit test | State |
|-------|-------------|-----------|-------|
| 0 | Repo + dev stack + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; data loads | ✅ done |
| 1 | Pi base + CAN bench + Signal K | sample N2K flows; Signal K dashboard populated | ✅ done — SK+uplink containerized; verified SK→uplink→DB→agent on the bench |
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
