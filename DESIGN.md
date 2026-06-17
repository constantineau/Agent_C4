# Agent_C4 — Product Design Description

**Product:** SR33 AI Navigator
**Vessel:** SR33 sailing yacht — distance racing (Bayview Mackinac; Port Huron → Mackinac Island)
**Status:** Phases 0–6 built & bench-verified (cloud pipeline, Pi bench + full-res archive, real
Claude agent, iPad navigator, alerting/summarizer/polar mining); Phase 7 started (server-side web
auth + TLS scaffolding). **Pivot (2026-06-17): a three-tier architecture** driven by RRS 41 — an
onboard deterministic engine for legal in-race use, an optional onboard LLM copilot, and cloud
frontier Opus 4.8 as the between-races C4 Performance Lab. **Phase 9 in progress:** **9.0 data-access
abstraction ✅, 9.1 onboard engine service ✅ (`pi/engine`), 9.2 server-side race gate ✅ + iPad onboard
console ✅ (`pi/console`)**, and the **C4 Performance Lab (`vps/lab`) is live — Lab-0 race ingestion ✅**
(NOR/SI/SER → a structured, reviewable RaceDefinition; verified on the real 2026 Bayview Mackinac NOR).
Next: Course&Marks review + wiring the RaceDefinition through, then Lab-1 (multi-model optimizer). 9.4
Orin LLM is on hold (no hardware yet). See §2, §8, §10, and `docs/ONBOARD_ENGINE_SCOPING.md`.
**Last updated:** 2026-06-17

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

**A three-tier design** driven by racing-rules compliance (RRS 41 forbids *customized tactical advice
computed off-boat while racing* — see §8). The dividing principle: **separate the deterministic
*computation* from the *LLM*.** The boat's own computer crunching the boat's own sensors is
Expedition-class and legal in-race; only customized advice arriving from *off the boat* is "outside
help." So:

- **Tier 1 — Onboard deterministic engine (Pi 4):** routing/tactics/sails/polars/nav/fatigue, plain
  physics on the boat's own sensors + the published course. No LLM. Legal in-race.
- **Tier 2 — Onboard LLM copilot (Jetson Orin Nano, optional):** Qwen2.5-7B narrates the engine's
  facts and does *bounded* decision support; never computes the numbers or invents strategy.
- **Tier 3 — Cloud frontier Opus 4.8 (the C4 Performance Lab):** *between races* it runs the
  strategy studio (→ a pre-loaded playbook) and the learning loop (→ refined polars), and serves the
  practice/cruising/debrief product. In a race it is **race-gated** (9.2) and the boat doesn't use it.

```
 ═══════════════════════ ON THE BOAT ═══════════════════════   in-race: LEGAL
 (own instruments + onboard computation + common public data)

   NMEA 2000  ──►  Pi 4 + PICAN-M ──► Signal K ──► full-res archive (SQLite)
   wind·STW·GPS·                          │               │
   AIS·hdg·depth·heel                     │               └─► uplink: 15-s aggregates
   (Orca Core unchanged)                  │                   + post-passage backfill ──┐
                                          ▼                                             │
                  ┌── TIER 1: ONBOARD ENGINE (9.0/9.1) — no LLM, Expedition-class       │
                  │   routing · tactics · sails · polars · nav · fatigue                │
                  │        │  facts + playbook                                          │
                  │        ▼                                                            │
                  │   TIER 2: Orin Nano LLM copilot (9.4) — Qwen2.5-7B                  │
                  │   narrate + bounded decision support                               │
                  │        ▲                                                            │
                  │   crew iPad ── boat-local Wi-Fi, NO WAN in race mode                │
                  │                                                                     │
                  │   public data IN: GRIB updates + NOAA/GLOS buoys (avail. to all)    │
                  └── PLAYBOOK loaded pre-start, FROZEN at the gun ◄──────────┐         │
                                                                              │         │
 ═══════════════════════ CLOUD (VPS) ═══════════════════════                 │ load    │ telemetry
 between races · practice · cruising · debrief                               │ (pre-   │ push
                                                                              │  start) │ (HTTPS)
   ingestion ──► TimescaleDB ──► cloud agent (Opus tool-use)  ◄───────────────┼─────────┘
                     │            • RACE-GATED (9.2): in a race withholds      │
                     │              tactical/routing/polar/sail/fatigue/nav    │
                     │            • alerting · summarizer · WebSocket ──► web   │
                     ▼                                                          │
   TIER 3: C4 PERFORMANCE LAB (Opus 4.8)                                       │
     • strategy studio: multi-scenario routing ──► PLAYBOOK ───────────────────┘
       (variants + decision tree + rationale, glass-box)
     • learning loop: archive ──► refined polars / crossovers / calibration
```

**Design principles**

- **Compute customized advice onboard while racing.** The boat's own gear is not an "outside source";
  the cloud's customized advice is. So the in-race tiers (1 & 2) run on the boat, and the cloud (tier 3)
  is used only between races (or is race-gated). This is the whole reason for the three-tier split.
- **Push-only from the boat.** Starlink is carrier-grade NAT — no inbound. All boat→cloud traffic is
  boat-initiated; remote admin uses Tailscale to traverse CGNAT.
- **The boat is the source of truth.** Full-resolution data is logged locally on the Pi; the cloud gets
  15-s aggregates live and full logs after each passage. A Starlink outage loses nothing (disk-backed
  store-and-forward queue). In a race the iPad talks only to the Pi/Orin over boat-local Wi-Fi.
- **The homework pattern (frozen at the gun).** Everything the frontier model touches happens
  *pre-start*; the playbook + refined polars are loaded onboard and frozen. In-race the boat selects
  among variants and re-optimizes *onboard* on common public data (GRIB/buoys) — never a fresh cloud call.
- **The LLM never sees raw NMEA.** Agents read facts through tools (cloud: SQL over TimescaleDB;
  onboard: the engine's structured outputs).
- **One CAN_IFACE switch.** The only bench↔boat difference is the CAN interface name — `vcan0` (bench,
  on the VPS) vs `can0` (boat). Everything else is identical.

See `docs/RRS41_COMPLIANCE.md` (the legal reasoning) and `docs/ONBOARD_ENGINE_SCOPING.md` (the Phase 9
build plan: onboard engine 9.0/9.1, race gate 9.2 ✅, Orin LLM 9.4, the C4 Performance Lab Lab-1→4).

---

## 3. Components — built vs. planned

| Component | Where | Status | Notes |
|-----------|-------|--------|-------|
| TimescaleDB schema | `vps/db/` | **built** | `telemetry_raw` (collect-everything) + legacy `telemetry` + `ais_targets` hypertables; `polars`, `waypoints`, `race_info`, `crew_notes`, `agent_summaries`, `source_notes`/`source_priority`, `alerts` (004), `app_state`+`audit_log` (005) |
| Ingestion API | `vps/ingestion/` | **built** | FastAPI bearer-token `/ingest` + `/ingest/raw` + `/ingest/ais`; writes batches |
| Agent — tools | `vps/agent/app/tools.py` + `shared/tool_contracts.py` | **built** | **16 tools** (conditions/sources/history/sail/navigator/tactics/route/polar-target/ais/alerts/summaries/polar-analysis/fatigue/forecast/route-status/log_note) |
| Boat-speed gospel | `vps/agent/knowledge/` | **built** | SR33 "C4" ORC Speed Guide; verbatim cert + distilled Best-Performance polar with per-row optimal **sail** + per-TWS **sail plan** (crossovers), cached in agent context; `build_speed_guide.py` regenerates |
| Polars (real data) | `vps/db/seed/polars_sr33.sql` | **built** | 126 real ORC polar points (TWS 4–24) |
| Agent — chat loop | `vps/agent/app/agent.py` | **built (key-gated)** | real Claude tool-use loop (Opus 4.8) when `ANTHROPIC_API_KEY` set, else deterministic fallback; **race-gated (9.2)** |
| Agent — WebSocket + REST | `vps/agent/app/main.py` | **built** | shared crew thread + live alert push (`Hub`); full REST surface; `GET/POST /mode` |
| Web app — iPad navigator | `vps/web/` | **built** | iPad-landscape companion: auto day/night, sail dial, course plot, navigator, tactics, routing, fatigue chip, alert banner, all-channels view |
| Helm fatigue index | `vps/agent/app/fatigue.py` | **built** | 0–100 anonymous-helm index + rotation call |
| Live AIS + CPA/TCPA | `vps/agent/app/ais.py` | **built** | cloud-side collision geometry (6.0) |
| Alerting | `vps/agent/app/alerts.py` | **built** | debounced rules + WebSocket push (6.1) |
| Summarizer / debrief | `vps/agent/app/summarizer.py` | **built** | on-demand window reports (6.2) |
| Polar mining | `vps/agent/app/polar_tool.py` | **built** | observed-vs-ORC % of polar (6.3, read-only) |
| Forecast + routing | `vps/agent/app/weather.py` + `routing.py` | **built** | Open-Meteo point forecast + isochrone routing on polars (5.4) |
| Race-mode gate | `vps/agent/app/race_mode.py` | **built (9.2)** | server-side fail-closed RRS 41 gate; `app_state` flag + `audit_log` |
| Web auth + TLS | `vps/agent/app/auth.py` + `vps/web/` | **built / scaffolded** | shared-password bearer (Phase 7); TLS nginx+certbot scaffolding awaiting a domain |
| Pi stack | `pi/` + `compose.pi.yml` | **built** | Signal K (SocketCAN, :3010, `$CAN_IFACE`) + uplink (15-s aggregates, store-and-forward) + full-res archiver (SQLite) + backfill; `signalk-derived-data` true wind/VMG auto-enabled |
| Dev/prod compose | `compose.{dev,prod}.yml` | **built** | isolated stacks, separate DBs (`sr33_dev`/`sr33_prod`) + ports |
| Data-access abstraction | `vps/agent/app/datasource.py` | **built (9.0)** | engine modules read via `datasource.active()` — `CloudSource` (Timescale) or `OnboardSource` (Pi SQLite archive + SK live) |
| **Tier 1 — Onboard engine** | `pi/engine/` | **built (9.0/9.1)** | the deterministic modules served onboard from the boat's own data (`OnboardSource`), no LLM, port 8200; bench-verified |
| Onboard race console | `pi/console/` | **built (9.2)** | the iPad app served from the Pi, pointed only at the engine over boat-local Wi-Fi (no cloud/auth/chat), port 8091 |
| **Tier 2 — Orin Nano LLM** | Jetson Orin Nano | **planned (9.4) — HW on hold** | Qwen2.5-7B copilot: narrate + bounded decision support |
| **Tier 3 — C4 Performance Lab** | `vps/lab/` | **Lab-0 built; Lab-1→4 planned** | browser prep/debrief app + race ingestion (NOR/SI/SER → RaceDefinition; dual-input + Opus extraction + review, port 8103). Lab-1→4: optimizer → playbook → onboard executor → judge loop |
| RaceDefinition schema | `shared/race_def.py` | **built (Lab-0)** | course/marks/gates/finish + comprehensive `requirements` checklist (race-time items → iPad) + `rules_profile` + fleet; validator |
| Deploy scripts | `deploy/` | **built (untested)** | `deploy_prod.sh`, `push_pi.sh` (Tailscale), `init_tls.sh` |

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

**Built (Phase 6):** an on-demand summarizer/debrief (`summarizer.py`), a conservative debounced
alerting loop with live WebSocket push (`alerts.py` — sustained wind shift, polar deficit, AIS CPA
inside guard, stale telemetry, shoaling, helm `rotate_now`), and observed-vs-ORC polar mining
(`polar_tool.py`). Alerts are deliberately rare so the crew listens.

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
**Race/Practice toggle** is the authoritative server-side RRS-41 gate (9.2). The
**helm fatigue index** shows in the top bar. One shared crew thread + chat to the agent.
Server-side shared-password auth is **built** (Phase 7; TLS scaffolding awaits a domain). In race
mode the iPad is served from the Pi (`pi/console`) and talks only to the onboard engine — no cloud.

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

**Reviewed 2026-06-17 — full memo `docs/RRS41_COMPLIANCE.md`; build plan
`docs/ONBOARD_ENGINE_SCOPING.md`.** The 2026 NOR **§2.1(d) changes RRS 41(c)**: information available
to all boats is allowed even at cost, *but that "shall not include private forecast or tactical advice
or information customized for a particular boat … while underway."* So **any customized
tactical/routing/polar/sail/fatigue advice computed off-boat and delivered while racing is prohibited
outside help** — and making the service or its outputs public does **not** cure it (memo §3). The memo
rebuts three loopholes: publishing per-boat advice (still "customized for a particular boat *or group
of boats*"), and the "Claude is available to all boats" framing (*"available to all"* is about the
**product**, not the **provider**; and "customized for a particular boat" is an independent, unbeatable
prong; orchestrator location is cosmetic). Allowed in-race: passive collection, the boat's **own**
instrument readout, **safety** alerts (AIS/depth/stale), all-boats info verbatim.

**The fix = separate the deterministic engine from the LLM → the three-tier architecture (§2, memo §4):**
- **(1) Onboard deterministic engine (Pi 4)** — routing/tactics/sails/polars/nav/fatigue on the boat's
  own sensors. Expedition-class, legal in-race, **no LLM needed** (~80% of the value). The iPad talks
  to the Pi in race mode.
- **(2) Onboard LLM (optional, Jetson Orin Nano 8GB)** — Qwen2.5-7B for in-race NL chat, single-shot
  narration over the engine's facts (no tactical invention).
- **(3) Cloud frontier Opus 4.8 (between races only)** — prep, debrief, and the **C4 Performance Lab**:
  write-back learning (refined polars/crossovers/calibration/fatigue) loaded onboard before the start,
  frozen at the gun, never re-derived mid-race.

**Minimum-now: BUILT (Phase 9.2).** The server-side, fail-closed Race-mode gate now enforces this on
the agent (`vps/agent/app/race_mode.py`): in a race the cloud agent + the advice REST endpoints
withhold tactical/routing/polar/sail/fatigue/navigation with an RRS-41 refusal, allow only safety +
own-instrument data + verbatim common data, and log every refusal to `audit_log`. The full fix is the
onboard engine (Phase 9.0/9.1), where the boat's own gear isn't an "outside source".
**Confirm with the OA/RC in writing and re-check the SIs (~July 2026) before race use.** Practice,
deliveries, and debriefs are unrestricted.

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
| 1 | Pi base + CAN bench + Signal K | sample N2K flows; Signal K dashboard populated | ✅ done — SK+uplink containerized; SK→uplink→DB→agent on the bench |
| 2 | Pi local archive | full-res replay; survives reboot; backfill lands in cloud | ✅ done |
| 3 | Ingestion + uplink store-and-forward | forced outage backfills cleanly | ✅ done — survives reboot mid-outage, no loss |
| 4 | Agent core + SQL tools (live LLM) | accurate answers vs live dev data | ✅ done — real Claude tool-use loop + boat-speed gospel + source skepticism/failover |
| 5 | iPad navigator UI | full practice sail used without instruction | ✅ done — day/night, sail dial, course plot, navigator, tactics, routing |
| 6 | Alerting + summarizer + polar tooling | acceptable false-positive rate over 2 sails | ✅ bench-complete; 2-sail false-positive gate awaits real sailing |
| 7 | Prod + deploy + rules review + soak | NOR compliance determined; 48-h soak passes | 🔶 started — server-side web auth + TLS scaffolding done; rules review done (§8); prod deploy/soak gated on domain + prod `.env` |
| **9** | **Onboard + C4 Performance Lab track (the three-tier pivot)** | onboard engine renders nav/sail/tactics on the Pi; race mode reaches no cloud; a sail → refined polars loaded back onboard | 🔶 in progress — **9.0 ✅, 9.1 onboard engine ✅, 9.2 race gate + onboard console ✅; C4 Performance Lab Lab-0 ingestion ✅.** Next: Course&Marks review + wiring → Lab-1. 9.4 Orin on hold (no HW). See `docs/ONBOARD_ENGINE_SCOPING.md` |

(Phase 8 was an interim "navigation & optimization" wishlist — real marks/GRIB/current/rounding-planner — now folded into the Phase 9 onboard track and the C4 Performance Lab.)

**Phase 9 sub-steps:** 9.0 data-access abstraction ✅ → 9.1 onboard engine service (Pi) ✅ → **9.2
server-side fail-closed race gate ✅ + iPad onboard console ✅** → 9.4 Orin Nano LLM copilot (HW on hold)
→ **Lab-0 race ingestion ✅** (NOR/SI/SER → RaceDefinition) → Lab-1 (GRIB+buoy+single-scenario routing)
→ Lab-2 (multi-scenario + branching playbook) → Lab-3 (onboard executor + iPad Strategy card) → Lab-4
(post-race judge loop). 9.3 = the C4 Performance Lab learning loop (hoisted-sail logging, polar
write-back).

## 11. Future work

Folded into the **Phase 9 onboard track + C4 Performance Lab** (§8, §10): empirical polar generation from
logs (write-back), automated post-race debrief reports, the onboard deterministic engine + optional
onboard LLM for rules-compliant in-race coaching, refined sail crossovers/calibration. Still loose:
real marks via N2K PGN 129284/129285 + course import; true GRIB routing (spatially-varying wind);
current/tide + buoy obs; start-line strategy; engine/battery PGN capture for deliveries; a second
interface (Telegram) and a shore-crew read-only tracker.
