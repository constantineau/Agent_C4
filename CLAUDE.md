# Agent_C4 — SR33 AI Navigator

LLM-powered navigator/coach/strategy-lab for the SR33 racing yacht "C4" (sail CAN100).
Boat NMEA 2000 → Raspberry Pi (Signal K) → telemetry to this VPS (TimescaleDB + Claude
agent) → crew web apps; a cloud **C4 Performance Lab** does pre-race strategy + post-race
learning; an onboard deterministic engine + a Jetson-Orin LLM copilot work the race itself.

This file is the **operational reference** — how the system works today and how to run it.
- `DESIGN.md` — product design: architecture + built-vs-planned.
- `docs/HISTORY.md` — the chronological development record (what shipped when, and the
  decisions that shaped it). Session narratives that used to live here are outlined there.
- `docs/` — per-arc design docs (playbook v2, strategy synthesis, RRS-41 memo, model-skill,
  retro study, optimizer UI study…). Some are explicitly marked superseded — kept as history.
- Per-component READMEs: `vps/lab/README.md` (the Lab, detailed), `pi/*/README.md` (onboard).
- Project brief: Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`.

## Architecture — three tiers (driven by RRS 41)

The 2026 Bayview Mackinac NOR §2.1(d) makes customized advice arriving from off the boat
while racing prohibited outside help; the boat's own computer crunching its own sensors is
Expedition-class and legal (full memo: `docs/RRS41_COMPLIANCE.md`). So the deterministic
computation is separated from the LLM across three tiers:

- **Tier 1 — onboard deterministic engine (Pi 4):** Signal K decodes N2K; a full-res local
  archive + an uplink push 15-s aggregates to the cloud; the deterministic modules
  (navigator/routing/tactics/sails/fatigue/AIS/fleet/deviation/drift/selector/reoptimize/
  strategy/matcher — plain physics, no LLM) run here, legal in-race. The iPad talks to the
  Pi over boat-local Wi-Fi in race mode; the Pi and the Orin also share a DIRECT ethernet
  link (10.10.10.1 ↔ 10.10.10.2, plugged 2026-07-08) so the engine↔copilot leg has no Wi-Fi
  dependency at all.
- **Tier 2 — onboard LLM copilot (Jetson Orin Nano 8GB):** Qwen2.5-7B via Ollama narrates
  the engine's facts and **condition-matches** the live picture against the pre-authored
  playbook. **It never originates strategy** (product decision 2026-07-06,
  `docs/PLAYBOOK_V2.md` §7) — the engine does the math; off-book verdicts are the
  deterministic engine's call; ungrounded LLM output is dropped.
- **Tier 3 — cloud (this VPS):** nginx → ingestion → TimescaleDB → agent (Opus tool-use,
  alerting, summarizer) → crew web app. Between races it is the **C4 Performance Lab**
  (strategy studio → signed playbook; debrief → human-approved learning). In a race the
  cloud is **race-gated, fail-closed** and the boat doesn't use it.

```
  BOAT (in-race, legal)                         CLOUD (between races / practice)
  NMEA2000 → Pi4: SignalK + archive + uplink ──telemetry push──► ingestion → TimescaleDB
              │  + ONBOARD ENGINE (T1, no LLM)                         │  → agent (Opus, RACE-GATED)
              │  + Orin LLM copilot (T2)        ◄──playbook (frozen────┤  → alerting/summarizer → web
   iPad ──boat-local Wi-Fi──┘  Pi═══ethernet═══Orin     at the gun)        ▼
   public data IN: GRIB + NOAA/GLOS buoys (avail. to all)         C4 PERFORMANCE LAB (T3):
                                                                  studio→playbook · learning→polars
```

**Standing decisions (all binding):**
- **Bright line:** all frontier/cloud work pre-start, frozen at the gun; in-race runs
  onboard on own data + common public data; never phone the cloud mid-race for a route.
- **Homework pattern:** everything the boat needs in-race (playbook, obstacles, fleet
  roster, forecast fingerprint, venue stats, course) is compiled ashore and loaded frozen.
- **Human-in-the-loop learning:** Lab proposals never mutate the boat model; a person
  approves every polar/helm/wave-coefficient change.
- **Collect everything:** every sensor reading, per source, raw SI; readers cross-check.
- **Glass box:** rationale is pre-authored and surfaced (Strategy card, briefings) — never
  an unexplained verdict.

## Repo layout (monorepo)

```
pi/                 Signal K config, systemd units, uplink + full-res archiver, bench tools
pi/engine/          onboard deterministic engine service (Tier 1) — no LLM, :8200
pi/console/         onboard race console + crew dashboard (iPad, served from the Pi, :8091)
pi/orin/            onboard LLM copilot (Tier 2) — Ollama+Qwen2.5-7B :11434; copilot/ svc :8300
vps/ingestion/      FastAPI ingestion API (token-auth → TimescaleDB)
vps/agent/          Claude tool-use agent + the shared deterministic engine modules
vps/web/            crew web app (nginx static, no build step)
vps/lab/            C4 Performance Lab (browser prep/debrief app + optimizer), :8103
vps/db/             schema + migrations + seeds (polars, crossovers, sail polars)
shared/             units, tool contracts, race_def (RaceDefinition), boat_profile, windphrase
deploy/             deploy_prod.sh (cloud), init_tls.sh (one-time TLS issuance)
compose.dev.yml     dev cloud stack · compose.prod.yml prod (managed, leave alone)
compose.pi.yml      Pi stack (boat or bench) · compose.pi.sample.yml bench sample-data overlay
```

**Isolation:** two compose projects, separate ports and DBs (`sr33_prod` vs `sr33_dev`);
develop on `dev`, ff-merge to `main`, deploy `main`. The only bench↔boat difference is
`CAN_IFACE` (`vcan0` vs `can0`).

## Dev environments — ports & commands

| Service | Host port | | Service | Host port |
|---|---|---|---|---|
| TimescaleDB | **5433** | | Signal K (host net) | **3010** |
| ingestion | **8101** | | onboard engine | **8200** |
| agent | **8102** | | onboard console + dashboard | **8091** |
| lab | **8103** | | copilot (on the Orin) | **8300** |
| web | **8090** | | Ollama (on the Orin) | **11434** |

```bash
cd ~/Agent_C4
docker compose -f compose.dev.yml up -d --build          # cloud dev stack
python3 vps/db/seed/fake_telemetry.py                    # fake raw readings → /ingest/raw → telemetry_raw
python3 vps/db/seed/ais_inject.py                        # deterministic AIS scenario (own ship + closing target)
# web http://localhost:8090 (pw sr33-dev) · lab http://localhost:8103 (pw lab-dev; standing container CAN100)
```

**Bench Pi stack is DOWN by default** (policy 2026-07-08 — its archiver records the sample
replay ~2 GB/day). Bring it up only when a test needs the engine/console/archiver:

```bash
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build   # bench w/ sample N2K
docker compose -f compose.pi.yml -f compose.pi.sample.yml down            # take it back down
# real boat:  CAN_IFACE=can0 docker compose -f compose.pi.yml up -d
```

**Deploys.** Cloud prod: `deploy/deploy_prod.sh` (gated on a prod `.env` + domain; TLS via
`deploy/init_tls.sh`, nginx+certbot scaffolding is built). **Pi:** ssh over Tailscale
(`sr33-pi@100.79.180.102`) → `cd ~/Agent_C4 && git pull && docker compose -f compose.pi.yml
up -d --build <changed services>`. **Orin:** `agent-c4@100.70.110.72` → git pull +
`systemctl restart sr33-orin-copilot` (runtime as-built: `pi/orin/DEPLOYMENT.md`).
Tailscale SSH may require a fresh browser check-mode approval (~12 h validity).

**Migrations auto-run only on first DB init.** Apply to a running DB by hand:
`docker compose -f compose.dev.yml exec -T timescaledb psql -U sr33 -d sr33_dev < vps/db/migrations/<file>.sql`
(001 init · 002 telemetry_raw · 003 source_priority · 004 alerts · 005 race_mode+audit ·
006 drops the legacy wide `telemetry` table).

## Data paradigm — collect everything, per source

The uplink forwards **every** Signal K `(source, path)` reading verbatim to `/ingest/raw`
→ **`telemetry_raw(time, boat_id, source, path, value)`** (raw SI). AIS targets go to
`/ingest/ais` → `ais_targets` (best-effort, not queued — stale positions must not replay).
`source_priority` (003) ranks a preferred source per channel with automatic failover
(preferred stale >45 s → next rank, flagged `fell_back`); all sources stay visible. The
agent is prompted to sensor skepticism: cross-check redundant sources, flag disagreement/
staleness, never trust a lone value. `source_notes` carries curated per-device reliability.

The Pi also keeps its own **full-res archive** (a second, independent Signal K subscriber →
crash-safe SQLite on the `sk_archive` volume; `pi/archiver/backfill.py` pushes it to the
cloud post-passage, resumable). Link outage loses nothing — the boat is the source of truth.

## Cloud agent (Tier 3)

`vps/agent/app/agent.py` runs the real Claude tool-use loop (`ANTHROPIC_MODEL`, currently
`claude-opus-4-8`) over **17 tools** (`shared/tool_contracts.py` is canonical): conditions/
sources/history (multi-source instrument reads), sail advice + crossovers, navigator,
tactics, route (isochrone on live/forecast wind), polar target + polar analysis (archive
mining), AIS targets, fleet (handicap tactics), alerts, summaries, forecast, route status,
fatigue, log note. A deterministic no-LLM fallback answers every tool intent when no API
key is set. The boat-speed gospel (ORC Speed Guide) is cached system context; regenerate
its four artifacts after a cert update: `python3 vps/agent/knowledge/build_speed_guide.py`.

- **Alerting** (`alerts.py`, migration 004): debounced rules — closing AIS (CPA/TCPA),
  persistent shift, polar deficit, stale telemetry, shoaling, helm `rotate_now` — evaluated
  every ~15 s, **raise slow / clear fast**, pushed live over `/ws`; cleared rows retained as
  debrief history. `ALERT_*` env tunables.
- **Summaries/debrief** (`summarizer.py`): on-demand only — `POST /summary` (~20 min) /
  `POST /debrief` (~120), stored in `agent_summaries`, LLM narrative w/ deterministic
  template fallback.
- **Polar mining** (`polar_tool.py`, cloud-only): archive → observed p90 STW vs ORC target
  by TWS/TWA bin → `GET /polar-analysis`. >100% usually = current or a soft rating.
- **Helm fatigue** (`fatigue.py`): anonymous multi-signal composite (heading stability,
  reversal rate, heel, TWD-detrended AWA wander, speed deficit) vs the boat's own trailing
  baseline; maneuver-aware; `fresh/watch/rotate_soon/rotate_now`; `FATIGUE_*` tunables.
- **Web auth** (`auth.py`): one shared boat password → stateless signed bearer
  (HMAC-SHA256, `AUTH_TTL_HOURS` 720); middleware gates all REST but `/health`+`/auth`;
  `/ws` checks `?token=`. Dev pw `sr33-dev`. TLS: nginx+certbot webroot scaffolding with an
  entrypoint template selector — prod starts HTTP-first and flips HTTPS once the cert lands.
- **Race-mode gate** (`race_mode.py`, migration 005): server-side, **fail-closed** (missing
  state ⇒ RACING; `RACE_MODE_DEFAULT` dev=practice, prod omits ⇒ race). `GATED_TOOLS`
  (tactics, route, polar analysis/target, sail, fatigue, navigator, route-status, fleet)
  are withheld from the LLM loop AND refused at dispatch while racing; the advice REST
  endpoints 403 `{withheld}`; own-data/safety/verbatim-forecast/recall stay allowed.
  `audit_log` records every mode change and refusal. This is the cloud stopgap — the real
  in-race surface is the onboard engine.

## Onboard Tier 1 — engine, console, crew dashboard

**Data-access seam:** engine modules read via `datasource.active()` (`DATA_SOURCE=cloud|
onboard`). `CloudSource` = TimescaleDB; `OnboardSource` (`datasource_onboard.py`) = the
full-res SQLite archive + an in-process Signal K live cache + polars parsed from the
committed seed + a local marks/kv store on the `engine_state` volume. Same interface, raw
SI, byte-identical module behavior. The onboard image ships no psycopg.

**Engine service** (`pi/engine/`, **:8200**, no LLM, no auth, no race gate — the boat's own
computer is legal in-race). Endpoints:
`/health · /conditions[/full] · /sources · /series · /fatigue · /sail · /sails/state
(GET+POST — the crew-set sail CONFIGURATION: a SET of flying sails (C0 alone · C0+J2 ·
kite+staysail), main reef, out-of-service gear; every change appends to the onboard
/sails/log with a timestamp) · /session + POST /session/start|end (the RACE LOG — see
below) · /course · POST /course/practice · POST /course/load (RaceDefinition course →
marks) · /navigator · /tactics · /forecast · /route · /ais · POST /fleet/load · /fleet ·
POST /playbook/load (freeze the signed bundle aboard; clears trigger/matcher state) ·
/deviation · /drift · /selector · /reoptimize · /strategy · /plays · /buoys`.

**Race log (sessions)** — the owner's record switch, fully standalone (no Lab prep, no
RaceDefinition, no cloud): the dashboard's ⏺ LOG button one-tap starts/ends a session
(name defaults to the date; a loaded playbook's race_id is picked up automatically). The
archiver records everything all the time, but only SESSION windows are kept long-term and
backfilled — outside them, readings are pruned after `ARCHIVE_RETAIN_DAYS` (14; bench
overlay 0) and never leave the boat, so day sails/deliveries don't accumulate anywhere.
`backfill.py` defaults to session mode (+ pushes the sail log as `crew.sail.state` and a
`crew.session` marker per closed window; `--all` restores everything-mode); the cloud
agent serves them back (`GET /racelog/sessions`, `GET /racelog/track`) and the Lab
Debrief's "boat's own log" track source builds the debrief track from them — full-res,
with the sail changes riding along. Prune is fail-safe: engine store unreadable ⇒ nothing
deleted. The dashboard **CURRENT SAILS bar** (chips + R1 reef) is the crew's logging surface — and
the per-config polar-development input: boat-log debriefs attribute every fix to the active
configuration (track.config_at), the learning archive stores performance bins per config, and
`GET /api/learning/config-polars` + the Learnings "Observed by sail configuration" card grow
observed curves for combinations the crossover chart doesn't rate (C0+J2, kite+staysail…).

The **executor stack** (all deterministic, Schmitt-hysteretic — consider/commit bands,
raise slow / clear fast):
- `deviation.py` — live position vs the frozen recommended track: XTE + side, along-track
  %, time-behind-plan, VMC vs plan pace. `DEV_*` tunables.
- `drift.py` — live Open-Meteo re-sampled at the bundle's frozen `forecast_fingerprint`
  (same-source, no cross-model bias): signed TWD/TWS drift. `DRIFT_*` tunables.
- `selector.py` — unifies shift + deviation + drift over the frozen variants into ONE call:
  **HOLD / SWITCH → variant / OFF-SCRIPT** + confidence (concordance raises it).
- `reoptimize.py` — the off-script fallback: a fresh route onboard (own polars + Open-Meteo
  + the bundle's frozen island/zone obstacles), explicitly flagged off-book, with a
  hoistable sail plan; on-demand + cached (never on every poll).
- `strategy.py` — the cross-signal synthesis: concordance (strong/split/weak) over
  shift/drift/deviation/fleet-lean + one grounded recommendation; chains a compact
  re-route offer on an off-book verdict; works with no playbook (tactics-only read).
- `matcher.py` — the Playbook-v2 **play matcher**: every play's predicates vs live signals
  (deviation/drift/tactics/fatigue/TWS/sail state/**polar_pct** — a windowed ~10-min mean
  of STW vs the polar target so a tack can't reset a sustain/**current_leg** — the
  navigator's next-mark index). Arm-slow (per-play `sustain_min`) / clear-fast;
  `applicability.legs` gating (hard for pace plays — leg N arrives at course marks[N];
  advisory never gates; unknown leg fails open); buoy **corroborators** raise confidence
  but never gate. `MATCHER_*` tunables.
- `buoys.py` — live NDBC observations; the up-course buoy as a leading indicator.

**Onboard console** (`pi/console/`, **:8091**): nginx serving the same web app pointed only
at the engine (`/api` → :8200, no cloud, no auth, no chat; `config.js` → `SR33_ONBOARD=
true`, every panel ungated). In a race the iPad uses `http://<pi>:8091`; between races the
cloud app.

**Crew dashboard** (`pi/console/dashboard/`, at `:8091/dashboard/`): **8 higher-order tiles
on a 4×2 grid** — wind, playbook, forecast, sail, eta, ais, charge (crew energy), data —
each LLM-scorable green/yellow/red with tap-to-detail; the **Strategy strip** above them =
SYNTHESIS apex (LLM/ENGINE mode pill, OFF-BOOK badge, Tier-2 `play_matches`) → selector
banner → deviation/drift triggers → armed PLAYS (+ gear toggle) → the ⟳ off-book re-route
line. The playbook tile reads the engine `/selector` (single source of truth — the old
copilot `/adherence` fallback is retired). Copilot commentary + coach line poll the Orin;
narration is **visual-only + an audio attention tone** for safety/urgent callouts (🔔
toggle, iOS-unlock aware). Demo scenarios (SRC button: live → calm → escalated) exercise
every state without a boat.

## Onboard Tier 2 — the Orin copilot

Runtime as-built (`pi/orin/DEPLOYMENT.md`): JetPack 7.2/R39, **from-source Ollama
(cuda_v13 @ sm_87) serving `qwen2.5:7b-instruct-q4_K_M` on :11434** (OpenAI `/v1`), ~12
tok/s, 100% GPU, systemd-persistent appliance. (The original MLC-on-:9000 plan is
superseded history — `pi/orin/SETUP.md`/`models.md` are kept but marked.) The copilot only
sees the OpenAI contract, so the runtime stays swappable.

**Copilot service** (`pi/orin/copilot/`, **:8300**; reached from the iPad via the console's
`/copilot/*` proxy, which rides the **direct Pi↔Orin ethernet** — `COPILOT_UPSTREAM` defaults
to `http://10.10.10.2:8300/`, the bench overlay overrides to the Orin's Tailscale IP): `GET /health · GET /tools · POST
/brief · POST /narrate · POST /narrate/reset · GET /coach · GET /adherence · GET /snapshot
· POST /strategy · POST /dashboard · POST /detail`. Guardrails are structural: a closed set
of read-only engine-fact tools; every factor/recommendation must be `grounded_in` a tool
actually used or it is dropped; caveats computed by the engine; deterministic fallback on
any LLM trouble. The **auto-coach** timer (30 s) drives the narration engine and holds the
latest callouts (safety/collision top priority, rounding prep 15/10/5 min, playbook branch,
sail change-down, deviation/drift, fleet rival, plays) — show-once dedup, priority-sorted.
`POST /strategy` phrases the engine digest + **ranks `play_matches`** against the play
library narratives (validated ids only). Engine addressing is boat-local-first with
`ENGINE_URL_FALLBACK`. Exit test: `python3 -m copilot.bench_copilot [--llm]`.

## C4 Performance Lab (Tier 3, `vps/lab/`, :8103)

The between-races strategy studio + debrief surface (detail: `vps/lab/README.md`; shared
team login `LAB_PASSWORD`). Hash-routed tabs: PREP (Races, Course & Marks, Rules/Safety/
Checklists, Fleet, Learnings, Gameplan, Lock-in & Deploy) · RACE (Monitor) · DEBRIEF.

- **Lab-0 ingestion:** NOR/SI/SER (auto-discover / URL / PDF) → Opus extraction → a draft
  **RaceDefinition** (`shared/race_def.py`: courses/marks/gates, comprehensive requirements
  checklists, rules_profile, fleet, tracker) → human review (editable form, geocoding,
  approve/sign-off) → save. Coordinates only when stated, never guessed.
- **Optimizer:** multi-model GRIB wind field (GFS/NAM/HRRR/GEFS/ECMWF(+ENS)/ICON/GEM via
  key-free sources, lag-aware freshest-cycle pick, cycle fallback, coverage gate,
  crash-isolated cfgrib parse) blended with **venue model-skill weights** (measured
  forecast-vs-observed accuracy, METAR+NDBC, deep GRIB to 2005 — `docs/MODEL_SKILL_
  WEIGHTING.md`); **currents** (NOAA LMHOFS: drift + wind-over-water correction); **waves**
  (NOAA GLWU → conservative realized-speed model + per-boat calibrated coefficients);
  **obstacles** (GSHHG full-res coastline backstop + NOAA ENC draft-aware shoals + race
  islands/zones + island rounding-side barriers); a sail-aware isochrone (per-sail polars,
  peel cost/hysteresis, VMG gate, cone prune, cumulative tack cost, layline commit,
  mark-position prune, adaptive endgame, monotone gate — `ROUTE_*` env knobs, all
  A/B-able) → route + per-leg sail plan/reefs + confidence + briefing; Gameplan map
  cockpit (Leaflet, wind/current/wave overlays, per-model route fan, isochrones, laylines).
- **Playbook:** per-model fan → side variants (v1) + the **v2 play library**
  (`docs/PLAYBOOK_V2.md`): external scenario fan (rotations/pressure/timing/sea-state
  through the SAME blended field, boundary bisection, fan-depth tiers) + internal plays
  (pace re-routes per mark, gear-loss re-runs, sail-guidance, low-maneuver,
  rejoin-vs-continue) + corridor verdict + venue stats frozen from the retro archive →
  Fable-primary/Opus-fallback synthesis (`ANTHROPIC_MODEL_CHAIN`) as a background job →
  **signed** (`sign_bundle`, sha256 canonical) → frozen aboard via `/playbook/load`.
- **Lab-4 learning loop:** Debrief ingests the real track (GPX or the YB AllPositions3
  binary), scores it vs the oracle re-route (regret, XTE, helm % — wave- and
  current-corrected `helm_pct`), archives every debrief, and **proposes** helm/polar/
  wave-coefficient refinements a human approves or rejects. Multi-race trend view.
- **Fleet retro study** (`docs/RETRO_STUDY.md`): whole-fleet backtest on own-ORC polars vs
  real tracks (2025: execution beat geometry — the Playbook-v2 threshold source).
- **Fleet auto-import:** YB entry list / regatta websites (YachtScoring API, iframe-follow)
  + the ORC public cert DB → reviewed draft roster with corrected-time handicaps.

## Verification & tests

- Agent/engine unit tests: `PYTHONPATH=vps/agent:. python3 vps/agent/test_<x>.py`
  (matcher, deviation, drift, selector, strategy, reoptimize, fleet, buoys…).
- Lab tests need the baked image: `docker cp vps/lab/test_<x>.py sr33-dev-lab-1:/srv/ &&
  docker exec sr33-dev-lab-1 sh -c "cd /srv && python3 test_<x>.py"`.
- Copilot exit test: `python3 -m copilot.bench_copilot` (pure) / `--llm` (on the Orin).
- Playwright harness: reuse DreamCRM's `playwright-core` (CJS default import
  `import pkg from '…/index.js'`) + the chromium-1228 binary, `--no-sandbox`; dashboard at
  `:8091/dashboard/`; use `domcontentloaded` (the dashboard polls forever, `networkidle`
  never settles).

## Gotchas

- **Every image BAKES its source** — rebuild after ANY change: `docker compose -f
  compose.dev.yml up -d --build <lab|agent|web|ingestion>` / `-f compose.pi.yml -f
  compose.pi.sample.yml up -d --build <engine|console>`.
- **Bench archive timestamps:** the sample stack replays a 2014 log, so archive-window
  reads (fatigue/tactics history) look empty on the bench even though live reads work;
  `/sources` merges the live cache. Real-boat timestamps are current.
- **`extract(epoch …)` returns `Decimal`** in psycopg — cast `::float8` in SQL or it
  poisons float math (the 6.4 alerts bug; fatigue/tactics guard it).
- **Long Lab jobs** (synthesis fan ~10 min, retro batch) run as background jobs with status
  polling — the nginx gateway 504s past ~300 s. Live optimize is ~60–70 s, then cached.
- **Passwords:** web dev `sr33-dev`; lab compose default `lab-dev`, the standing
  lab.racertracer.net container uses `CAN100`.
- **`.env` safety:** copy it outside the tree before any risky branch op
  (`cp .env ~/agentc4.env.bak`) — a `git rm --cached` + branch switch once overwrote it.
- **Don't `compose down` the standing lab container** without restarting it
  (lab.racertracer.net rides on it).
- The optimizer falls back to a constant wind where the field has no coverage — trust the
  `wind_coverage`/`degraded` flags, not a pretty route.

## Database safety

Never run destructive DB ops or migrations against `sr33_prod` without explicit go-ahead.
Dev DB (`sr33_dev`) is disposable. `.env` is gitignored — keep it that way.

## Open items still owed (don't guess)

Domain name + prod `.env` → first cloud prod deploy + 48-h soak · confirm RRS-41 posture
with the OA/RC in writing + re-check the SIs (~July 2026) before race use · plug the N2K
cable into the Pi (bus not yet physically connected) · record a real `candump -l can0`
dockside as the gold-standard replay fixture · the Phase-6 exit test proper (alert
false-positive rate over 2 real practice sails) awaits real sailing.
