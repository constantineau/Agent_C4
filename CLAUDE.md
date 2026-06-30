# Agent_C4 — SR33 AI Navigator

LLM-powered navigator/coach/data-archive for the SR33 racing yacht. Boat NMEA 2000 →
Raspberry Pi (Signal K) → 15-s telemetry aggregates pushed over Starlink → this VPS
(TimescaleDB + Claude-API agent) → mobile web chat for the crew.

Full project brief: Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`.
This file is the operational summary (brief §§1–8); read the doc for full context.

## Architecture — three tiers (driven by RRS 41)

Compliance forces the split: the boat's own computer crunching its own sensors is Expedition-class and
legal in-race; only *customized advice arriving from off the boat* is "outside help". So **separate the
deterministic computation from the LLM** across three tiers:

- **Tier 1 — Onboard deterministic engine (Pi 4):** Signal K decodes N2K → JSON; full-res local
  archive; uplink POSTs 15-s aggregates to the VPS (Orca Core/app unchanged — the Pi is a silent extra
  listener). The deterministic modules (routing/tactics/sails/polars/nav/fatigue — plain physics, *no
  LLM*) run here so they're legal in-race. The iPad talks to the Pi over boat-local Wi-Fi in race mode.
  *(9.0 data-access abstraction ✅ — modules read via `datasource.active()`; 9.1 onboard service next.)*
- **Tier 2 — Onboard LLM copilot (Jetson Orin Nano, optional):** Qwen2.5-7B narrates the engine's facts
  + bounded decision support (never does the math, never invents strategy). *(Phase 9.4; HW ~06-18.)*
- **Tier 3 — Cloud (this VPS):** nginx+TLS → FastAPI ingestion → TimescaleDB → agent (Opus tool-use,
  alerting, summarizer) → web. Between races it's the **C4 Performance Lab** (strategy studio → playbook
  + write-back learning) and the practice/cruising/debrief product; in a race it is **race-gated (9.2)**
  and the boat doesn't use it.

```
  BOAT (in-race, legal)                         CLOUD (between races / practice)
  NMEA2000 → Pi4: SignalK + archive + uplink ──telemetry push──► ingestion → TimescaleDB
              │  + ONBOARD ENGINE (T1, no LLM)                         │  → agent (Opus, RACE-GATED 9.2)
              │  + Orin LLM copilot (T2)        ◄──playbook (frozen────┤  → alerting/summarizer → web
   iPad ──boat-local Wi-Fi──┘                       at the gun)        ▼
   public data IN: GRIB + NOAA/GLOS buoys (avail. to all)         C4 PERFORMANCE LAB (T3):
                                                                  studio→playbook · learning→polars
```

**Design principles:** customized in-race advice is computed **onboard** (the cloud is between-races or
race-gated); push-only from the boat (Starlink CGNAT, no inbound; admin via Tailscale); boat is source
of truth (link outage loses nothing); the **homework pattern** — the frontier model's work is loaded
onboard pre-start and frozen at the gun, never re-derived mid-race; the LLM never sees raw NMEA (it
reads facts through tools). Full design: `docs/RRS41_COMPLIANCE.md` + `docs/ONBOARD_ENGINE_SCOPING.md`.

## Repo layout (monorepo)

```
pi/                 Signal K config, vcan/systemd units, uplink + full-res archiver
pi/engine/          onboard deterministic engine service (Tier 1, 9.1) — no LLM, :8200
pi/console/         onboard race console (9.2) — the iPad app served from the Pi, :8091
pi/orin/            onboard LLM copilot (Tier 2, 9.4) — Orin: Ollama+Qwen2.5-7B :11434; copilot/ decision-support svc :8300
vps/ingestion/      FastAPI ingestion API (token-auth, writes batches to TimescaleDB)
vps/agent/          Claude tool-use service + alerting + summarizer + WebSocket chat
vps/web/            mobile web chat app (nginx static)
vps/lab/            C4 Performance Lab (cloud) — browser prep/debrief app + race ingestion, :8103
vps/db/             TimescaleDB schema + migrations + fake-data seed
shared/             data schemas, tool contracts, units, race_def (RaceDefinition schema)
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
| lab (C4 Performance Lab) | 8000 | **8103** |
| web         | 80             | **8090**   |
| Signal K    | 3010 (host net)| **3010**   |
| onboard engine (pi)      | 8200 (host net) | **8200** |
| onboard console (pi)     | 8091 (host net) | **8091** |

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

**Onboard engine (Phase 9.1):** the same `compose.pi.yml` also runs the `engine` service — the
in-race-legal deterministic engine on **:8200** (no LLM, no cloud), reading the boat's own data
via `OnboardSource`. It comes up with the rest of the Pi stack; quick check:
`curl -s localhost:8200/health` then `curl -s localhost:8200/conditions`. See
"Onboard engine service (Phase 9.1)" below and `pi/engine/README.md`.

## Phased build (each phase has a clear exit test)

| Phase | Deliverable | Exit test |
|-------|-------------|-----------|
| **0** ✅ | Repo + dev compose + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; fake data loads |
| **1** ✅ | Pi base + PICAN-M + vcan0 + Signal K | sample N2K flows; Signal K dashboard populated |
| **2** ✅ | Pi local archive | full-res capture verified on bench; survives reboot; backfill lands in cloud `telemetry_raw` |
| **3** ✅ | Ingestion + uplink store-and-forward | forced-outage test passed: batches queue to a named volume, survive a reboot mid-outage, drain with no loss |
| **4** ✅ | Agent core + SQL tools | accurate answers on conditions/perf/AIS vs live dev data |
| **5** ✅ | iPad nav companion: day/night, sail dial, course plot, navigator, tactics, routing | bench-verified end-to-end |
| **6** ✅ | Alerting + summarizer + polar tooling | bench-complete; 2-practice-sail false-positive gate awaits real sailing |
| 7 🔶 | Prod stack + deploy + rules review + soak | rules review done; server auth + TLS scaffolding done; prod deploy/soak gated on domain + prod `.env` |
| **9** 🔶 | Onboard + C4 Performance Lab (three-tier pivot) | **9.0 data-access abstraction ✅ · 9.1 onboard engine service ✅ · 9.2 race gate + iPad onboard console ✅ · Lab-0 race ingestion + course loader ✅ · Lab-1 multi-model GRIB optimizer ✅ · Lab-2a/2b branching playbook bundle ✅ (fan-out → variants → Opus synthesis → signed, onboard-loadable artifact) · routing-fidelity 2b per-leg sail plan + reviewable boat sail model ✅ · routing-fidelity 2c isochrone VMG-gate/cone-prune/anti-over-tack ✅ · routing-fidelity 2e finish/mark over-tack ("scramble") fixes ✅ · routing-fidelity 2f island rounding-side enforcement ✅ · routing-fidelity 2g sail-aware routing (per-sail polars + peel cost) ✅ · 9.4 Orin LLM appliance live (Ollama+Qwen2.5-7B :11434) + copilot decision-support layer ✅ (`pi/orin/copilot`) · copilot crew-facing narration ✅ + proactive auto-coach timer ✅ + collision/AIS safety callout ✅ + handicap-rival callout ✅ · PLAYBOOK-ADHERENCE dashboard tile ✅ (10-tile 5×2 grid) · handicap-aware fleet tactics ✅ (incl. verified YB/bycmack tracker source) ** — see `docs/ONBOARD_ENGINE_SCOPING.md` |

**Current status:** Phases 0–6 built and bench-verified; Phase 7 started; **Phase 9 in progress
(9.0 data-access abstraction ✅, 9.1 onboard engine service ✅ — see "Onboard engine service",
9.2 server-side race gate ✅ + iPad onboard console ✅ — see "Race-mode gate" / "Onboard race
console"; the C4 Performance Lab (`vps/lab`) is live with **Lab-0 race ingestion + course loader ✅**
and **Lab-1 the multi-model GRIB optimizer ✅** + **Lab-2a/2b the branching playbook bundle ✅** —
see "C4 Performance Lab"; the copilot crew-facing narration + the PLAYBOOK-ADHERENCE dashboard tile
are built; handicap-aware fleet tactics is built (see "Handicap-aware fleet tactics");
**9.4 Orin LLM bring-up authored** (Orin Nano in hand 2026-06-18 —
`pi/orin/` runtime/model bring-up: MLC + Qwen2.5-7B INT4 → OpenAI-compatible API, to run on the
fresh unit; the SR33 copilot service is the next 9.4 increment) — see "Onboard LLM copilot").**
Detail: Phase 1
(Signal K + uplink) end-to-end;
Phase 2 (full-res onboard archive + backfill); Phase 3 uplink store-and-forward (disk-backed
queue on a named volume — forced-outage test passed, survives reboot mid-outage, drains with no
loss); Phase 4 agent runs the *real* Claude tool-use loop (`vps/agent/app/agent.py`, not stubbed)
with the boat-speed gospel + per-source skepticism + source priority/failover. True wind/VMG now
flow via the auto-enabled `signalk-derived-data` plugin. Phase 5 ✅ the iPad crew interface is
built (day/night, sail dial, course plot + navigator, tactics, weather/isochrone routing — see
"iPad crew interface"). Phase 6 ✅ (bench-complete) — **6.0 live AIS** (see "Live AIS"), **6.1 alerting**
(see "Alerting"), **6.2 summarizer/debrief** (see "Summaries / debrief"), **6.3 polar mining**
(see "Polar mining") and **6.4 done** — the final contracts/prompt sweep (all 16 tools consistent
across dispatch/contracts/schema/prompt/fallback) + a full bench verify (closing-AIS raise→update→
clear over the WebSocket, history retention, debrief alert-integration, banner shot; caught + fixed
the `active_alerts` Decimal bug). The Phase-6 EXIT TEST proper — an acceptable alert false-positive
rate over **2 real practice sails** — still awaits real sailing (bench baseline: 0 spurious alerts
in steady reaching; AIS/polar_deficit fired only on genuine sustained conditions). **Phase 6
COMPLETE on the bench.** **Phase 7 STARTED — server-side shared-password web auth done + bench-verified
(see "Web auth"), retiring the client stub.** Remaining Phase 7: prod deploy + TLS/domain + 48-h soak
(gated on a prod `.env` w/ `sr33_prod` DB + fresh secrets, and a domain name) + RRS 41/NOR review.
Still owed — a real `candump -l can0` replay fixture (the canned sample stands in for now).

## Live AIS (Phase 6.0)

Collision-avoidance traffic follows the collect-everything model: the boat (em-trak B951)
forwards only RAW target observations (mmsi/name/lat/lon/sog/cog) and the cloud reasons. The Pi
uplink routes Signal K deltas by `context` — own-ship deltas go to `telemetry_raw` as before;
other-vessel contexts are accumulated per-MMSI and POSTed to ingestion **`/ingest/ais`** →
`ais_targets` (geometry columns left NULL). AIS is sent best-effort (NOT disk-queued like
telemetry — replaying stale positions after a link outage would be wrong). `vps/agent/app/ais.py`
computes range/bearing + **CPA/TCPA live** against own-ship's freshest fix from `telemetry_raw`
(equirectangular relative-motion model, nm) — so geometry always reflects the current situation,
not when the target was heard. `get_ais_targets` returns targets threat-sorted (closing, smallest
CPA first); the agent prompt has an AIS / COLLISION GUARD section (always allowed — safety, never
RRS 41 "outside help"). **Bench:** `python3 vps/db/seed/ais_inject.py` injects a stable synthetic
own ship + three moving targets (one deliberate closing near-miss) so CPA/TCPA are deterministic
without the teleporting Signal K sample boat; doubles as the closing-target scenario for 6.1 alerts.

**Onboard AIS + the AIS / Fleet tile.** `ais.py` is now **source-agnostic** — it reads both own-ship
state and the raw targets through `datasource.active()` (the same Phase-9.0 seam as the rest of the
engine), so the identical range/bearing/CPA/TCPA geometry runs in the **cloud** (`CloudSource`:
`ais_targets` + `telemetry_raw`) and **onboard** (`OnboardSource`: other-vessel Signal K contexts).
This also dropped `ais.py`'s direct `db.pool` import, so it ships in the no-psycopg onboard image.
Onboard, `OnboardSource._ingest_live` now keys off the Signal K `context`: own-ship deltas go to the
live cache as before (a latent own-ship/other-vessel conflation is fixed in passing), and other-vessel
contexts accumulate per-MMSI (lat/lon + sog→kn + cog→deg, mirroring the uplink) → `ais_targets()`. The
onboard engine exposes **`GET /ais`** (collision + fleet awareness — always legal in-race: own AIS
receiver, own computer). The crew dashboard's **AIS / Fleet** tile (`pi/console/dashboard`) reads it:
ok = clear / no closing traffic, watch = a target closing inside ~1.5 nm / 30 min, act = inside the
~0.5 nm / 12 min guard (env-tunable in `dashboard.js`); the face shows the nearest closing contact +
CPA/TCPA and the detail lists the threat-sorted targets. **v1 scope is AIS proximity/collision** (the
always-legal safety layer); handicap-aware **fleet** tactics (roster match → corrected-time delta,
the perflab item-6 vision) is now BUILT on top of it — see "Handicap-aware fleet tactics" below.
Verified: onboard geometry unit test (`/tmp/test_ais_onboard.py`: head-on CPA≈0, opening
target flagged, range filter, no own-ship pollution); cloud regression via `ais_inject.py` (CLOSING
TUG CPA 0.54 nm / 16.2 min sorted first — unchanged baseline); engine `/ais` serves; Playwright UI
(9-tile 3×3 grid, AIS tile clear-live / watch-demo + detail rows, 0 console errors).

## Alerting (Phase 6.1)

`vps/agent/app/alerts.py` raises conservative, **debounced** alerts on a background loop. Each
rule reports the conditions true *right now* (closing AIS with CPA/TCPA in the guard, persistent
wind shift via tactics, boatspeed well under polar, stale telemetry, depth shoaling, helm
`rotate_now`); a condition must hold continuously for its per-rule window before it RAISES, and
CLEARS as soon as it goes false — **raise slow, clear fast** (thresholds are env-tunable
`ALERT_*`, first-cut — exit test is an acceptable false-positive rate over two practice sails).
The agent lifespan runs `alerts.evaluate()` every `ALERT_EVAL_SECONDS` (15) in a threadpool; it
diffs the firing set against the DB and **pushes new/updated/cleared deltas over the existing
`/ws`** to all clients (a `Hub` fans out; each connection drains its own queue so a push and a
chat reply never race). Migration **`004_alerts.sql`** = one `alerts` table that is both live
state (`cleared_at IS NULL`) and the **debrief history** (cleared rows retained). Surfaced as
`GET /alerts`, the `get_alerts` tool (+ ALERTS prompt section + fallback route), and a
severity-colored **dismissible web banner** (`#alerts`; warn/danger via theme vars so it's
day/night aware; a fresh client gets the active set as a snapshot on connect). **Migrations
auto-run only on first DB init** — apply 004 to the running dev DB by hand:
`docker compose -f compose.dev.yml exec -T timescaledb psql -U sr33 -d sr33_dev < vps/db/migrations/004_alerts.sql`.
Bench-verified end-to-end with `ais_inject.py`: AIS + polar_deficit alerts raised after debounce,
streamed live `updated` pushes, and cleared — banner screenshot confirmed. (**Gotcha fixed in 6.4:**
`active_alerts()` did `time.time() - extract(epoch …)`, and `extract(epoch …)` returns a `Decimal`,
so `GET /alerts` 500'd whenever any alert was active — now cast `::float8` in SQL. Same Decimal
trap fatigue/tactics already guard against.)

## Summaries / debrief (Phase 6.2)

`vps/agent/app/summarizer.py` rolls up a time window of telemetry into a performance report and
stores it in the existing `agent_summaries` table. **On-demand only — no background timer** (by
decision). `compute_window(start,end)` aggregates `telemetry_raw` (boatspeed avg/max vs polar,
TWS range, TWD circular mean + oscillation, heel, SOG-integrated distance) plus every alert that
fired in the window (the 6.1 `alerts` debrief history). The narrative is written by Claude from
the metrics when `ANTHROPIC_API_KEY` is set, else a deterministic template — so it works with no
LLM. Two entry points differing only by default window + framing: `make_summary` (short recap,
`SUMMARY_MIN`=20) and `make_debrief` (fuller report, `DEBRIEF_MIN`=120). Surfaced as **`POST
/summary`**, **`POST /debrief`** (both accept `?minutes=`, both store), **`GET /summaries`** +
the `get_summaries` tool (recall newest-first) + a DEBRIEFS/SUMMARIES prompt section + fallback
route, and a **Debrief** quick button in the web chat (POSTs `/api/debrief`, drops the narrative
into the log). Caveat: aggregates span ALL sources for a path (collect-everything) so a flaky
sensor can nudge an average — fine for v1. **Note:** the summarizer reads `telemetry_raw` (Pi
uplink / sample stack), NOT the legacy `telemetry` table that `fake_telemetry.py` writes to.

## Polar mining (Phase 6.3)

`vps/agent/app/polar_tool.py` mines the telemetry ARCHIVE for what the boat ACTUALLY achieved vs
the ORC rated polar — a coaching/debrief view, NOT the live instantaneous %. It time-buckets
`telemetry_raw` (default 30 s) pivoting STW/TWS/|TWA| onto each slice, bins by TWS (ORC 2-kn grid)
and TWA (15° bins), and for each bin with enough samples takes a HIGH PERCENTILE of observed STW
(default 90th — "best achievable", rejects surf/GPS spikes a max would chase) and compares it to
the nearest ORC `target_stw`. Output: overall % of polar (sample-weighted), a roll-up by point of
sail (upwind/reaching/downwind), the weakest/strongest bins, and the full observed-vs-rated table.
Surfaced as **`GET /polar-analysis`** (`?hours=&min_samples=&point_of_sail=`), the
**`get_polar_analysis`** tool (+ POLAR ANALYSIS prompt section + a distinct fallback route — kept
separate from the live `get_polar_target` "Polar%" path), and a **Polar trend** web quick-button.
Tunables are `POLAR_*` env (look-back 168 h, bucket 30 s, min-samples 6, pctile 90). **Caveats
(in the output):** aggregates span all sources; sea-state/current/crew vary across the window;
**>100% of polar is usually current or a soft rating, not real overspeed** — e.g. the sample boat's
light-air 45° upwind bins read 157–167% because the ORC VMG-optimum angle there is ~42° and the
rated target at the binned 45° is low. Bench-verified: `GET /polar-analysis?hours=24` mined 14
bins / 1636 slices (overall 91%, upwind 96% / reaching 88%); the live LLM chained the tool into a
grounded coaching answer that correctly dismissed the >100% bins; the no-LLM fallback rendered too.

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

## iPad crew interface (Phase 5)

`vps/web` is an iPad-landscape **navigator companion** (not an instrument repeater — the boat
already has those). Vanilla JS over nginx (`/api/*` + `/ws` proxy to the agent), no build step,
offline-friendly. Pieces:
- **Auto day/night** (`sun.js`) — switches on local sunrise/sunset from GPS position + time
  (Safari has no light sensor); manual AUTO/DAY/NIGHT override. Night = red-on-black.
- **Sail-range dial** (`sail.js` + `sails.py`, `GET /sail`, `get_sail_advice`) — point-of-sail
  gauge with the J1/A2/A3/S2 zones for the current TWS, crossover ticks, live TWA needle, and a
  crew "what's hoisted" selector that flags wrong-sail / imminent peel.
- **Schematic course plot** (`plot.js`) — boat/marks/legs/laylines/wind/track on a local
  projection (no chart tiles), N↑/Crs↑. `navigator.py` (`/course`, `/navigator`, `get_navigator`)
  computes next mark/ETA/leg-type/laylines; a practice-course generator (`POST /course/practice`)
  drops a W/L course from live position+wind. Active route is shared in-process so chat matches
  the screen.
- **Tactical layer** (`tactics.py`, `/tactics`, `get_tactics`) — lifted/headed, oscillating vs
  persistent shift, favored side, leverage.
- **Weather routing** (`weather.py` Open-Meteo + `routing.py` isochrone, `/forecast`, `/route`,
  `fetch_forecast`/`get_route`) — optimal route on the polars through the forecast wind (falls
  back to live measured wind); ETA, tacks, recommended first tack, route overlay.
- **Race/Practice toggle** gates tactics + routing in the UI (RRS 41); the agent caveats them.
  **All-channels** slide-over reuses the multi-source `/conditions/full` view.

Routing is CPU-bound but cached (~25 s); the LLM may take 45–90 s when it chains several tool calls.

## Web auth (Phase 7)

The crew share ONE boat password (project decision). `vps/agent/app/auth.py` replaces the old
client-stub gate with a real server check: **`POST /auth {password}`** verifies against
`BOAT_PASSWORD` (constant-time) and issues a **stateless signed bearer token** — `"<exp>.<hmac>"`,
HMAC-SHA256 over the expiry with `AUTH_SECRET` (defaults to a value derived from `BOAT_PASSWORD`,
so changing the password rotates tokens; set a fresh random value in prod). An **HTTP middleware**
(`require_auth`) gates every REST route except `OPEN_PATHS` (`/health`, `/auth`); the **`/ws`**
handler checks the token inline (passed as `?token=` since browsers can't set headers on a WS
handshake) and closes 1008 if absent/bad. Token TTL is `AUTH_TTL_HOURS` (default 720 h / 30 days so
the crew don't re-auth mid-passage); `AUTH_ENABLED=false` disables the check (open bench only).
The web app POSTs the password to `/api/auth`, stores the token in `sessionStorage`, and routes all
calls through a single `apiFetch` helper (injects `Authorization: Bearer`, re-gates on 401) + the
WS URL `?token=`; a stored token auto-resumes on reload. Dev password is `sr33-dev` (compose
default + screenshot harness). Prod compose requires `BOAT_PASSWORD` + `AUTH_SECRET` in the `.env`.
Bench-verified end-to-end through the nginx proxy: `/health` open; data/chat/WS all 401/reject
without a token; wrong password 401; correct password connects; tamper/expiry/garbage tokens
rejected; reload auto-resumes. **Note:** this is a shared-secret bearer behind TLS — not per-user
identity; appropriate for a boat iPad, not a public app.

### TLS scaffolding (prod, awaiting a domain)

The web nginx terminates TLS in prod via a standard **nginx + certbot (webroot)** setup, built but
not yet deployed (needs a domain + DNS). The image stages two configs in `/etc/nginx/template-src/`
and an entrypoint selector **`docker-entrypoint.d/10-tls-select.sh`** (runs before the stock
envsubst step) picks one into `/etc/nginx/templates/`:
- **`default.conf.template`** (HTTP-only) — used in dev and during prod bootstrap; serves the
  `/.well-known/acme-challenge/` docroot so certbot can issue.
- **`default.ssl.conf.template`** — selected only when `TLS_ENABLED=true` AND the cert for
  `$SERVER_NAME` exists: port 80 redirects to HTTPS (+ keeps the ACME location), port 443 does
  TLS 1.2/1.3 + HSTS and proxies `/` + `/api` + `/ws`. The cert-existence check means prod web
  always starts (HTTP first) and flips to HTTPS on the next restart once the cert lands — **dev is
  unaffected** (no `TLS_ENABLED` → HTTP-only, same as before; verified, and `nginx -t` passes on
  the rendered TLS config).
`compose.prod.yml` gains a long-running **`certbot`** service (renews every 12 h) sharing
`letsencrypt` + `certbot_webroot` volumes with web, and the web service publishes 80/443.
**`deploy/init_tls.sh`** does the one-time issuance: starts web HTTP-only, runs `certbot certonly
--webroot` for `$SERVER_NAME`, then recreates web on TLS (`--staging` flag for dry runs). Prod
`.env` adds `SERVER_NAME`, `CERTBOT_EMAIL`, `TLS_ENABLED`. **Deploy caveat:** ports 80/443 must be
free on this shared VM (DreamCRM/racertracer also run here) — check before deploy, or front
everything with one shared reverse proxy.

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
context; `vps/db/seed/polars_sr33.sql` = real polars for the DB/optimizer;
`vps/db/seed/sr33_crossovers.json` = the **per-TWS sail crossover model** the Lab optimizer uses
for the per-leg sail plan, routing-fidelity 2b; `vps/db/seed/sr33_sail_polars.json` = the **per-sail
polar curves** (each sail's speed across its TWA domain, not just the envelope) the optimizer's
sail-aware routing uses to weigh hold-vs-peel, routing-fidelity 2g). The agent advises sail selection
and crossovers/peels from the sail plan. Regenerate all four after a cert update:
`python3 vps/agent/knowledge/build_speed_guide.py`.

## Racing-rules caveat (RRS 41 / Bayview Mackinac NOR) — drives the three-tier pivot

**Reviewed 2026-06-17 — full memo `docs/RRS41_COMPLIANCE.md`; onboard build plan `docs/ONBOARD_ENGINE_SCOPING.md`.**
The 2026 NOR **§2.1(d) changes RRS 41(c)**: info available to all boats is OK even at cost, but *"shall
not include private forecast or tactical advice or information customized for a particular boat … while
underway."* So **customized tactical/routing/polar/sail/fatigue advice computed off-boat and delivered
while racing is prohibited outside help** — and that's true even if it comes from a public service.
The memo rebuts three loopholes (§3): publishing per-boat advice (still "customized for a particular
boat *or group of boats*"); a public fleet feed; and the "Claude is available to all boats" framing —
*"available to all"* is about the **product**, not the **provider**, and "customized for a particular
boat" is an independent, unbeatable prong (orchestrator location is cosmetic). **Allowed in-race:**
passive collection, the boat's **own** instrument readout, **safety** alerts (AIS/depth/stale),
all-boats info verbatim. Practice/delivery/debrief is unrestricted.

**The fix = separate the deterministic engine from the LLM → a three-tier architecture (memo §4):**
- **Onboard deterministic engine (Pi 4):** `navigator/routing/tactics/sails/polar_tool/fatigue` are
  plain physics on the boat's own sensors — Expedition-class, legal in-race, **no LLM needed** (~80% of
  the value). Move them to the Pi; the iPad talks to the Pi in race mode.
- **Onboard LLM (optional, Jetson Orin Nano 8GB):** Qwen2.5-7B (~21.8 tok/s INT4/MLC) for in-race NL
  coaching over the engine's facts — single-shot narration, no tactical invention.
- **Cloud frontier Opus 4.8 (between races only):** prep, debrief, and the **C4 Performance Lab** —
  write-back learning that refines polars/crossovers/calibration/fatigue, loaded onboard *before the
  start* (frozen at the gun; never re-derived mid-race).

**Minimum-now: DONE — see "Race-mode gate" below.** **Confirm with the OA/RC in writing + re-check
the Sailing Instructions (~July 2026) before race use.**

## Race-mode gate (Phase 9.2) — server-side, fail-closed RRS 41 enforcement

Race/practice mode used to live only in the browser (`localStorage`) and gate the UI; the chat/LLM
and every REST route still answered tactical questions. **9.2 moves the gate server-side.**
`vps/agent/app/race_mode.py` is the single source of truth: an authoritative flag persisted in
`app_state` (key `race_mode`, value `race|practice`), **fail-closed** (missing/unreadable → treat as
RACING; default from `RACE_MODE_DEFAULT` — dev compose sets `practice`, prod omits it → `race`).
`GATED_TOOLS` = the customized-advice tools withheld while racing: `get_tactics`, `get_route`,
`get_polar_analysis`, `get_polar_target`, `get_sail_advice`, `get_fatigue`, `get_navigator`,
`get_route_status`. **Allowed in-race:** own instruments (`get_current_conditions`/`get_strip`/
`get_sources`/`get_history`), safety (`get_ais_targets`/`get_alerts`), common data verbatim
(`fetch_forecast`), recall (`get_summaries`), `log_note`.
- **Agent (`agent.py`):** in race mode the Claude loop gets only the allowed tools + a RACE_MODE
  directive, and `dispatch` refuses any gated tool (defense in depth); the no-LLM fallback early-returns
  the refusal on a gated intent. Refusal text is the RRS-41 message.
- **REST (`main.py`):** `GET/POST /mode` (authoritative, audited); the 7 advice endpoints + `POST
  /summary` + `POST /debrief` return **403 `{withheld,detail,mode}`** while racing via `_race_gated`.
  `/health` now reports `mode`.
- **Audit trail:** `audit_log` records every `mode_change` and every `refusal` (channel/tool) — a
  tamper-evident record for a protest committee.
- **Web:** the mode toggle is now authoritative — `POST /api/mode` + `syncMode()` on load (server wins);
  the sail dial + plot navigator skip their (now-gated) fetches in race and show a withheld note; the
  Debrief button surfaces the withheld message.
- **Migration `005_race_mode.sql`** (`app_state` + `audit_log`). Migrations auto-run only on first DB
  init — apply to a running dev DB by hand:
  `docker compose -f compose.dev.yml exec -T timescaledb psql -U sr33 -d sr33_dev < vps/db/migrations/005_race_mode.sql`.
Bench-verified through nginx: practice → all 200; race → tactics/route/navigator/sail/fatigue/
polar-analysis/debrief 403, conditions/alerts/forecast 200, chat refuses tactical Qs + answers safety,
audit_log populated; toggle round-trips. **This is the cloud STOPGAP** — the real fix is the onboard
engine (9.0/9.1), where the boat's own gear isn't an "outside source".

## Onboard engine — data-access abstraction (Phase 9.0)

The deterministic engine modules (navigator/routing/tactics/sails/fatigue + the live `get_polar_target`)
no longer query TimescaleDB directly — they read through `vps/agent/app/datasource.py` so the **same
engine code runs in the cloud or onboard the Pi**. `datasource.active()` returns the backend chosen by
env **`DATA_SOURCE`** (`cloud` | `onboard`, default `cloud`); methods return **raw SI** values + epoch
timestamps, and the unit conversions stay in the modules (so behavior is byte-identical to pre-9.0).
`CloudSource` reproduces the exact prior SQL: `latest_value`, `series`, `series_by_source`,
`best_angles`, `polars_stw`, `polar_nearest`, `marks`, `save_practice_course`. **`OnboardSource`
(SQLite archive + Signal K live) is Phase 9.1** — `active()` imports it lazily only when
`DATA_SOURCE=onboard`. `sails.py` needs no data source (it parses the speed guide); `polar_tool.py`
(archive mining via Timescale `time_bucket`) stays **cloud-only** — it's a between-races C4 Performance
Lab tool, not part of the in-race onboard engine. Bench-verified: every engine endpoint
(conditions/navigator/tactics/sail/fatigue, route on a practice course, practice-course generation)
returns real data through the abstraction; cloud path unchanged.

## Onboard race console (Phase 9.2, iPad-side)

The iPad-side of the race-mode channel separation. In race mode the iPad connects to the **Pi**, not
the cloud — at the network level (boat-local Wi-Fi, no WAN), which is the strong compliance posture.
The console (`pi/console/`) is an nginx that serves the **same web app** (`vps/web/public`) but pointed
only at the onboard engine (`/api/` → `127.0.0.1:8200`, no `/ws`), on **:8091** (host net). The app
runs in an **onboard mode** flipped by a one-line `config.js` (the cloud serves `window.SR33_ONBOARD =
false`; the console overrides `/config.js` to `true`). Onboard mode (in `app.js`): no password gate
(the engine has no auth), no LLM chat WebSocket, `apiFetch` drops the bearer and never re-gates on 401,
the chat card is hidden, and the mode pill becomes a static **ONBOARD** badge — and crucially **every
panel is available** (sail/navigator/tactics/route ungate, since the boat's own computer is legal
in-race; `tacticsAllowed()`/`plot.racing()` are onboard-aware). The cloud web is unchanged (it serves
the static `config.js=false` and stays auth-gated + LLM-chat + RRS-41-toggle as before). Bench-verified
with Playwright pointed at `:8091`: loads with no gate, `SR33_ONBOARD=true`, chat hidden, **zero
`/auth` and zero WebSocket calls**, all panels (incl. the gated ones) fetched through the engine proxy,
and the course/sail/navigator render. Cloud regression check: `:8090/config.js`=false and
`/api/conditions` without a token still 401s. **So in a race the iPad uses `http://<pi-ip>:8091`
(onboard, legal); between races it uses the cloud app (full LLM + debrief).**

## Onboard engine service (Phase 9.1)

The in-race-legal tier: a small FastAPI service (`pi/engine/`) that runs **on the boat** and
serves the same nav/sail/plot/tactics/route endpoints the iPad uses, but computed **onboard from
the boat's own data** — no LLM, no cloud round-trip, **no race gate** (the boat's own computer
crunching its own sensors is Expedition-class, not an "outside source" under RRS 41). It reuses
the cloud agent's deterministic modules unchanged (`app.navigator/tactics/routing/fatigue/sails/
weather`) with `DATA_SOURCE=onboard`, so `datasource.active()` resolves to the new
**`OnboardSource`** (`vps/agent/app/datasource_onboard.py`), which reads:
- **telemetry history** — the Phase-2 full-res SQLite archive (`sk_archive` volume, written by
  `pi/archiver`), mounted read-only — for `series`/`series_by_source` (fatigue/tactics windows);
- **freshest live value** — an in-process Signal K WS cache (lower latency than the ~2-s archive
  flush; falls back to the archive if the WS is down) — for `latest_value` + the instrument strip;
- **polars** — parsed from the committed `vps/db/seed/polars_sr33.sql` (the one canonical source;
  no DB) — for `best_angles`/`polar_nearest`/`polars_stw`;
- **course marks** — a small local SQLite store on the `engine_state` volume (the boat has no
  `waypoints` Postgres table) — for `marks`/`save_practice_course` (the practice course).

The instrument strip / multi-source view is built by `app.onboard_conditions` (mirrors
`tools.PRESENT`; the cloud builds it off Postgres in `tools.py`). All values are raw SI / epoch
seconds, identical in shape to `CloudSource`, so the modules' conversions are byte-identical. To
let the onboard image ship **no psycopg**, `datasource.py` guards the `pool` import (cloud has it
→ unchanged; onboard absent → unused). Endpoints (port **8200**): `/health`, `/conditions`,
`/conditions/full`, `/sources`, `/fatigue`, `/sail`, `/course`, `/navigator`,
`POST /course/practice`, `/tactics`, `/forecast`, `/route`. Wired into `compose.pi.yml` as the
`engine` service; cloud parity reference is `vps/agent/app/main.py`. See `pi/engine/README.md`.

**Bench-verified** (sample Pi stack + engine): every endpoint returns real data through
`OnboardSource` — `/conditions` + `/conditions/full` (14 channels) + `/sources` (5 sources) +
`/sail` (optimal A2 + crossovers) off the live cache; `/course/practice` → `/course` → `/navigator`
(ETA/laylines) → `/route` (isochrone via Open-Meteo, `wind_source: forecast`) + `/forecast`; and
the **archive-history path** (`/fatigue`, `/tactics`) verified against a recent-timestamped seeded
archive (`series` 2999 pts; fatigue computes all 5 components; tactics returns favored-side +
recommendation). **Bench gotcha:** `--sample-n2k-data` replays a 2014 log, so the archive's SK
*source* timestamps fall outside any wall-clock window — history endpoints look empty on the bench
even though live ones work (on the real boat SK timestamps are current); `/sources` therefore
merges the live cache. Cloud path unaffected (`active: CloudSource`, `pool` still set).

## C4 Performance Lab (cloud) — Phase 9 / Lab-0

`vps/lab/` is the browser-based, between-races **prep + debrief** surface (the race-day surface is the
deliberately-simple `pi/console`). One FastAPI container serves the Lab web shell + a race-library API;
shared **team** login (`LAB_PASSWORD`, dev `lab-dev`) gates `/api/*`, the static shell is public. Dev
**:8103**. The Lab is organized into hash-routed tabs across the three phases (PREP: Races, Course &
Marks, Rules/Safety/Checklists, Fleet, Learnings, Gameplan, Lock-in & Deploy · RACE: Monitor · DEBRIEF)
so each opens in its own browser tab; the **Races** tab is live, the rest are descriptive placeholders.

**RaceDefinition** (`shared/race_def.py`) is the portable artifact a race's NOR/SI/SER distil into —
course geometry (marks/gates/finish, **decimal-degrees WGS84**), a **comprehensive `requirements`
checklist** (safety/SER + procedural; each tagged phase + trigger, race-time items flagged
`deliver_to_ipad` so they compile into the playbook for the onboard console), and the `rules_profile`
(rule modifications incl. the RRS-41 carve-out + scoring). It feeds **both** the optimizer/navigator
and the race gate. A dependency-free validator (`python3 -m shared.race_def <json>`) separates errors
(block) from warnings (human-review items, e.g. `needs_review` coordinates).

**Lab-0 race ingestion (live).** Dual input → Opus extraction → a **draft** → review → save:
`POST /api/ingest/discover` (auto-find PDF links on a race page), `/api/ingest` (URLs — auto-find
selections or pasted direct links), `/api/ingest/upload` (multipart PDFs — for JS-rendered hubs a
crawler can't reach), `/api/races` (save the reviewed draft to the `lab_ingested` volume).
`app/extract.py` pulls text with pypdf and Opus (`ANTHROPIC_MODEL`, `max_tokens` 32k) emits a
schema-conformant RaceDefinition — coordinates **only when stated** (else `needs_review`; never
guessed). The lab image carries `anthropic`+`pypdf`; the dev compose passes `ANTHROPIC_API_KEY`.
Bundled `vps/lab/races/bayview_mackinac_2026.json` is the hand-curated reference instance.
**Verified on the real 2026 Bayview Mackinac NOR + SER:** 0 errors, **56 requirements (8 →iPad)**, the
Cove Island gate + finish coordinates, 13 RRS modifications, ORC ToT scoring — a draft more complete
than the hand-built one; discover returned 39 candidates; save round-trips; UI Playwright-verified.
**Drafts are always machine-extracted → human review before they're relied on.**

**Course & Marks review + course loader (built).** The Lab's Course & Marks tab renders each course on
a schematic map + an editable marks table; fill any `needs_review` mark by hand or **Geocode**
(`POST /api/geocode` → Nominatim, human-confirmed) and Save — the reviewed copy lands on `lab_ingested`
and **overrides the bundled seed** (store precedence). The **homework→onboard course loader**:
`shared.race_def.course_to_marks()` flattens a course (gate→midpoint, finish→midpoint; un-geocoded
marks skipped + reported) and **`POST /course/load`** (on BOTH the cloud agent and the onboard engine)
writes it via `datasource.save_course(route, marks)` + activates the route, so the navigator/plot use
the real course. Bench-verified on Mackinac cloud + onboard (gate+finish midpoints, Duck/Bois-Blanc
skipped). The per-race `rules_profile`→gate wiring is deferred until a consumer exists (tracker access
/ optimizer scoring).

## C4 Performance Lab — Lab-1 multi-model GRIB optimizer core

The optimizer that turns a reviewed RaceDefinition course into ONE optimal route + a pre-race
briefing, routing through a **real multi-model wind field** (cloud / between-races homework, frozen at
the gun — RRS 41). Lives in `vps/lab/app/`:
- **`wind/` package — the multi-model wind field.** `grib.py` downloads a 10 m UGRD/VGRD GRIB2 **bbox
  subset** per (model, cycle, forecast-hour, member) and parses it with cfgrib/eccodes (the eccodes
  pip wheel bundles the binary → no apt; `python -m eccodes selfcheck` runs at build) into a samplable
  `GribFrame` (bilinear on regular GFS/GEFS/ECMWF grids, nearest on curvilinear NAM/HRRR Lambert).
  `models.py` defines key-free sources — **GFS / NAM / HRRR** (NOMADS GRIB-filter), **GEFS** (NOMADS
  ensemble, opt-in), **ECMWF** IFS open-data (the `ecmwf-opendata` client) — each knowing its cadence,
  forecast-hour grid, availability lag and a lag-aware **freshest-cycle** picker. `windfield.py`'s
  `WindField.wind_at(lat,lon,epoch) → (tws_kn, twd_deg)` is a **drop-in for the agent's
  `weather.wind_at`**: it samples every model/member series (spatial-bilinear/nearest + temporal
  linear), blends by model priority, and reports the **SPREAD across models/members as a confidence**
  (the fuzzy-adherence signal — models disagree → low confidence → sail conservatively). Ingestion is
  best-effort: a field not yet posted (or no egress) is skipped and the route runs on what loaded.
- **`optimizer.py` + `polars.py`.** `optimizer.py` is a self-contained isochrone router (no agent
  package) that routes the course **leg by leg** through the `WindField` on the SR33 polars
  (`polars.py` parses the one canonical `polars_sr33.sql`, the same source the onboard engine reads) →
  one optimal route, per-leg ETA/tacks/point-of-sail/wind, total time/distance, a route-wide
  **confidence** (mean model agreement sampled along the path) and skipped-mark report. `briefing()`
  has Opus write the pre-race routing briefing — explicitly flagging the low-confidence legs — with a
  deterministic template fallback so a briefing always returns.
- **Wiring.** `POST /api/optimize {race_id, course_id?, start_epoch?, models?, ensemble_members?}`
  derives the bbox + time window from the course, builds the wind field, routes, and returns the route
  + briefing + wind-field provenance; `GET /api/models` lists the models + the default deterministic
  set (`gfs,nam,hrrr`; ensembles opt-in). The **Gameplan → Optimizer** web tab (`vps/lab/web`) picks
  race/course/start + models, runs it, and renders the stats, the isochrone route on a canvas, the leg
  table, the briefing, and the model/cycle provenance — all confidence-coloured. Dev compose adds a
  `lab_gribcache` volume so re-runs / many members are cheap. **Bench-verified end-to-end** on the
  Mackinac `cove_island` course (live GFS 18Z + NAM 00Z + HRRR 01Z, ~73 frames; 133 nm/17.8 h/1 tack;
  route confidence 0.69; Opus briefing led with the confidence caveat; ~52 s first run, then cached;
  Playwright-verified the tab). **Next:** Lab-2 — fan the optimizer across ensemble members/scenarios →
  cluster → the **branching playbook bundle**. See `vps/lab/README.md`.

**Sparse/degraded-GRIB hardening: SHIPPED 2026-06-21.** Four pieces so a thin wind field can't silently
produce a confident-looking but fake route (the optimizer falls back to a constant 12-kn wind wherever
the field has no coverage). (1) **Cycle-fallback** (`windfield._load_model`): if a model's freshest
cycle is too sparse (< `GRIB_MIN_FRAME_FRAC`=0.5 of expected frames — i.e. not fully posted yet) it
steps back to the previous cycle and retries (`GRIB_CYCLE_FALLBACK`=2 tries); meta now carries
`expected_frames` + `cycle_fallbacks`. (2) **HRRR per-cycle horizon** (`HRRR.pick_cycle` /
`ModelSource.horizon_for`): HRRR runs hourly but only its SYNOPTIC cycles (00/06/12/18) reach 48 h —
off-synoptic stop at 18 h. For a race needing more, it now picks the freshest synoptic cycle, and
`_fhrs_for_cycle` caps the requested forecast-hours at the horizon a cycle actually reaches (no more
wasted f19–f48 404s). (3) **Coverage gate** (`optimizer._wind_coverage`): measures the fraction of the
routed path that had REAL multi-model coverage vs the fallback wind → `wind_coverage`. (4)
**Route-sanity guard** (`optimizer._route_sanity`): flags `degraded` + a `warnings[]` list when no data
loaded, coverage < `GRIB_COVERAGE_MIN`=0.6, a leg's average speed exceeds the polar max ×1.2 (a tell of
a wind gap), or the optimizer timed out. The briefing (Opus + deterministic fallback) OPENS with the
degraded warning; the Gameplan optimizer tab shows a degraded banner + a wind-coverage stat + per-model
frames/expected with a cycle-fallback badge. Verified: 13 unit tests (cycle selection / fhr-cap /
fallback / coverage / sanity) + a real end-to-end Mackinac run (HRRR auto-picked the 18Z synoptic cycle
→ 46 frames reaching ~48 h for the 47-h race; coverage 1.0, not degraded) + Playwright UI smoke.

**GRIB parse isolation (crash hardening): SHIPPED 2026-06-29.** cfgrib/eccodes can intermittently
SEGFAULT on a frame (a native finalizer crash) — uncatchable by `try/except`, so it took down the whole
optimize worker (observed: the live `/api/optimize` empty-replied at ~99 s, container restarted). With
`GRIB_ISOLATE_PARSE` (default ON) the cfgrib parse runs in a PERSISTENT child process
(`app/wind/_grib_parser.py`, one per `build_windfield`, paying the xarray/cfgrib import once):
`grib.IsolatedGribParser.parse()` feeds the child one file/line and reads back the U/V arrays via a temp
`.npz`; on child death (segfault) or a `GRIB_PARSE_TIMEOUT_S`=60 hang it **respawns + retries**
(`GRIB_PARSE_RETRIES`=2), then skips the frame — which `_load_model`'s existing `except: continue`
already treats like any unreadable frame, so a crashing frame degrades to a skipped frame instead of a
dead worker. Threaded through `GribFrame.from_file(..., parser=)` / `_load_model(..., parser=)` /
`build_windfield` (creates + closes one parser per build). Verified (`test_grib_isolation.py`): an
injected child crash is survived (parse→None, process lives) + the parser respawns and works again + the
isolated parse matches in-process `open_uv` exactly; integration — injecting a crash on every GFS frame
skips GFS (0/26) while NAM/HRRR load fully and the build still succeeds. (This is the durable fix for the
intermittent crash; the consistent crash was the separate unpinned-pandas one — see the requirements
pin.) Tunables `GRIB_ISOLATE_PARSE` / `GRIB_PARSE_TIMEOUT_S` / `GRIB_PARSE_RETRIES`.

**Obstacle avoidance (routing fidelity 2a, from the Bitsailor gap analysis): SHIPPED 2026-06-20.**
`vps/lab/app/geo/` keeps the optimizer's route off land — **race-agnostic**: three layers rasterize
into one boolean mask the isochrone prune queries (`blocked`/`crosses`): (1) a GLOBAL coastline
(`coastline.py`, `land∧¬lake` + islands-in-lakes, fetched once to the `lab_coastline` volume +
auto-clipped to the course bbox → works for any race, ocean or lake; **GSHHG full-res by default**,
Natural Earth 1:10m as the dependency-light fallback — see "Higher-res coastline backstop" below),
(2) the race's `zones[]` (exclusion/hazard/tss), (3) the race's geocoded `island` marks
buffered to a disk (`radius_nm`; islands are obstacles, NOT waypoints — `course_to_marks` omits them
as waypoints). `optimize_course(avoid=True)` builds the field (cached by `cache_key`, so Lab-2's
same-course scenarios share one mask) + threads it through `route_leg`; `POST /api/optimize` takes
`avoid_land` (default true) and returns an `obstacles` summary + `obstacle_steps_avoided`; the Gameplan
tab overlays coast/islands/zones on the route canvas. **A/B-verified on the real Cove GRIB route:**
avoid OFF passes 1.9 nm from Bois Blanc center (cuts across it); ON clears at 5.7 nm for +0.3 nm/+1 tack.
Caveats: NE 1:10m is coarse near shore + misses sub-nm islands (the race island/zone layer covers the
critical ones; island coords geocoded `approx` → human-review); rounding SIDE is now enforced for
islands that are MARKS of the race (2f, below) — plain hazard islands are still avoided either side.
Tunables `GEO_RES_DEG`/`GEO_ISLAND_NM`. See `vps/lab/README.md`.

**Map accuracy upgrade — NOAA ENC + BoatProfile + a real slippy map: SHIPPED (dev).** Fixes the
"map is not accurate" complaint in three pieces (detail in `vps/lab/README.md`): **[A] NOAA ENC
charts** (`app/geo/enc.py`) — authoritative S-57 vector charts as a pluggable obstacle source
(`COASTLINE_SOURCE=enc`, NE fallback): prep-time `ogr2ogr` → cached GeoJSON on `lab_enc`, giving real
land (LNDARE), **draft-aware shoals** (DEPARE < boat safety depth) and rocks (OBSTRN/UWTROC); `POST
/api/enc/prep` warms it. **[B] BoatProfile** (`shared/boat_profile.py` + `app/boats.py`) — race × boat
as two dimensions; the active boat's **draft** sets the ENC depth no-go (canonical metres, UI in
**feet**; SR33 = 7 ft); `/api/boats[/active]` + a Gameplan boat selector + Charts toggle. Also carves a
navigable pocket at each waypoint (`GEO_MARK_CARVE_NM`) so a near-shore finish is reachable (was
thrashing the router to 1406 nm / 148 tacks; now 278 nm / 6 tacks on ENC). **[C] GRIB-on-ENC slippy
map** (`web/mapview.js`, Leaflet vendored) — the route canvas is now a Leaflet layer stack [OSM (+
OpenSeaMap) + our ENC shoal/rock/land polygons + GRIB **wind** arrows faded by confidence + route],
with a **forecast time slider** over an embedded multi-time `wind_grid` (`WindField.sample_grid`).
NOAA ENC Online tiles are SCAMIN-gated / blank and RNC was sunset → the chart layer is our own
extracted ENC polygons over OSM (self-contained, no CDN/build).

**Higher-res coastline backstop — GSHHG full-res: SHIPPED 2026-06-22.** Natural Earth 1:10m misses
sub-nm islands (it had **zero** islands across the whole North Channel / Georgian Bay) and is coarse
right at the shoreline. The global coastline (`coastline.py`) now defaults to **GSHHG** (Global
Self-consistent Hierarchical High-resolution Geography), full resolution. GSHHG's hierarchy maps
exactly onto our three roles — **L1 = land**, **L2 = lakes**, **L3 = islands-in-lakes** — so the
existing fill logic (fill land → carve lakes → re-add islands) is source-agnostic. GSHHG ships as
shapefiles → a prep-time `ogr2ogr -clipsrc <bbox>` (GDAL already in the lab image for ENC) clips each
level to the course bbox into cached GeoJSON, then the hot path stays pure-python (same pattern as
`enc.py`). The 149 MB bundle is fetched + the chosen-res L1–L3 extracted once to the `lab_coastline`
volume. `COASTLINE_GLOBAL` (`gshhg` | `natural_earth`, default `gshhg`) + `GSHHG_RES`
(`f`|`h`|`i`|`l`|`c`, default `f`); falls back to NE automatically if GSHHG can't be fetched.
This is the **global backstop under BOTH modes** — it runs first in ENC mode too, so US-only ENC's
Canadian gap (Cove Island, Manitoulin) is covered by GSHHG, not NE. **Mask A/B-verified** on the
Bayview Mackinac cove_island bbox: GSHHG blocks **778 cells across 251 island clusters** that NE
leaves open + refines 663 cells where NE's coarse shoreline over-blocked water; ENC mode still blocks
Canadian Manitoulin (via the GSHHG backstop) with US draft-aware shoals intact; live optimize 42.4 h,
coverage 1.0, reaches the finish. (Cove Island's own landmass already read as land under NE because it
abuts the coarse Bruce-Peninsula blob; the real win is the many mid-lake islands NE omits, plus the
shoreline refinement.) Rounding **side** is now enforced for marked islands (2f, below).

## C4 Performance Lab — Lab-2 branching playbook bundle

The output of the prep studio: the optimizer's one route becomes a small set of strategic
**variants** + a crew decision-tree, synthesized + **signed**, and dropped onboard as the copilot's
frozen homework. Two stages, both in `vps/lab/app/`:

- **Lab-2a fan-out → variants (`playbook.py`).** Routing through the *blended* field gives one
  answer, but the models disagree — so split the multi-model `WindField` into per-model sub-fields
  (each a "what if the wind follows THIS model" scenario, free — reuses the GRIB already down­loaded),
  route the course through each + the blended consensus, and **cluster by which side of the first
  beat** each favors (left/middle/right of the rhumb). Returns variants with `supported_by` models,
  `share` (agreement), total-hours + range, a representative route, and the **decision spread** (the
  time stakes between the side options). `POST /api/playbook`.
- **Lab-2b synthesis → signed bundle (`synthesis.py`).** Turns the 2a variants into the artifact the
  crew carries. **Opus** writes, per variant, a crew-facing `summary` / `rationale` / `tradeoffs` and
  — most important — `what_flips_it`: the concrete **observable** on-the-water trigger (a wind shift
  past a bearing relative to the first-beat rhumb, persistent vs oscillating) that says "abandon this
  variant, switch to that one" — that trigger is what makes the playbook *branching*. Plus a
  `headline`, a `recommended` start default, and an ordered `decision_tree`. A **deterministic
  fallback** builds a valid bundle with no API key (the Lab never depends on the model). The bundle is
  the **`c4.playbook/v1`** schema — a *superset* of what the onboard copilot's `playbook.Playbook`
  reads (`race_id` + `variants[].id/summary/what_flips_it`), so freezing one and pointing the
  copilot's `PLAYBOOK_PATH` at it is the whole onboard wiring. **Signing:** `sign_bundle()` hashes the
  canonical content (sha256 over the bundle minus its `signature`, sorted-key/no-space JSON) →
  tamper-evident "frozen at the gun"; the copilot's `playbook.verify_signature()` recomputes the
  **identical** canonical bytes, so a bundle that arrives byte-for-byte verifies (surfaced in copilot
  `/health` as `signed`/`signature_ok`, non-fatal). `pbstore.py` persists frozen bundles on the
  `lab_playbooks` volume (`/srv/playbooks`, id `<race_id>__<start_epoch>`). Endpoints: `POST
  /api/playbook/synthesize` (draft), `POST /api/playbook/freeze` (sign + persist), `GET
  /api/playbooks[/{id}]`, `GET /api/playbooks/{id}/download` (the exact signed bytes — scp to the
  Orin). The **Gameplan tab** gains a "Synthesize branching playbook" panel below the optimizer:
  headline + recommended + stakes, per-variant cards (summary/why/tradeoffs/what-flips-it), the
  decision tree, and a **Freeze & sign** → signature + download. RRS 41: all pre-race cloud homework
  — the copilot SELECTS/INTERPRETS these variants in-race, never originates new strategy.

**Verified end-to-end on the real Bayview Mackinac cove_island course** (live GFS+NAM+HRRR + Opus,
~2.5 min): a 3-way split (HRRR-left / NAM-middle / GFS-right), low agreement 0.33, 252-min decision
spread; Opus wrote specific rhumb-relative triggers ("if the breeze veers and holds right of ~020°
for two-plus oscillation cycles = persistent right shift, bail to right"); freeze → signed (sha256) →
download → the onboard copilot loaded it, **verified the signature**, and emitted the LLM digest with
each variant's flip trigger. UI Playwright-verified. See `vps/lab/README.md`.

**Routing fidelity 2b — per-leg SAIL PLAN + reviewable boat sail model: SHIPPED.** The optimizer
routes on the Best-Performance polar envelope, which IS the max-over-sails speed — so the route's
speed is already sail-optimal, but it didn't say WHICH sail. 2b attaches that. `build_speed_guide.py`
now also emits **`vps/db/seed/sr33_crossovers.json`** — the per-TWS sail crossover bands (optimal sail
by TWA: J1 upwind → A2/A3 reaching → S2 running), precomputed from the ORC cert via the existing
`optimal_sail()`. `vps/lab/app/sailplan.py` loads it (`optimal_sail(tws,twa)`, clamped so an upwind
beat's sub-close-hauled direct TWA still maps to the up sail; `crossovers(tws)`; `model()`).
`optimizer.py` adds `sail` to each leg (TWS/TWA already in scope) + a route-level `sail_plan` (the
peel sequence); these flow into the playbook variants for free. The synthesis bundle gains a
**`boat_model`** block (the crossover table + polar source + active-boat draft) so the reviewed boat
model is **frozen into the signed homework and loaded onto the copilot** — `pi/orin/copilot/
playbook.py` surfaces `boat_model` + the per-variant sail plan in its LLM digest (`/health` reports
`sail_inventory`). The Lab **Gameplan tab** gains a "Review boat model — polars & sail crossovers"
panel: the crossover bands per TWS (color-coded sails over a 0–180° TWA axis) + the polar grid (TWS ×
TWA → target boatspeed) — what gets loaded onto the copilot, reviewable before lock-in. New endpoints
`GET /api/crossovers` + `/api/polars`; the optimizer leg table gains a Sail column + a sail-plan
strip. Verified end-to-end (per-leg sail attaches where wind detail exists, bundle carries
boat_model, freeze→signed→copilot digest shows the sail model; UI Playwright-verified).
**Jib change-downs by TWS (J1/J2/J3):** the ORC cert rates only ONE headsail (the speed-optimal J1),
so J2/J3 — same upwind slot, smaller jibs for a building breeze — aren't in the polar. The
`BoatProfile` carries an editable **`jib_crossovers`** (TWS bands, e.g. SR33 J1<14 / J2 14–20 / J3>20
kn — crew/sailmaker thresholds, NOT from the cert); `sailplan.optimal_sail(tws,twa,jib_crossovers)`
specialises the upwind jib by TWS; the active boat's bands thread through `optimize_course`/
`build_playbook`/`synthesize` and into the bundle's `boat_model`. The review panel shows + edits them
(`POST /api/boats/jib-crossovers`); the copilot digest surfaces "Upwind jib by wind: J1 <14; …".
`sailplan.crossovers_specialized()` relabels the upwind band of **each TWS row** to the real jib for
that wind (a row is one TWS → exact), so the crossover chart + bundle show J1 (light) → J2 (mid) → J3
(heavy), not just the cert's lone J1.

**Routing fidelity 2c — VMG-gate + cone-prune + anti-over-tack: SHIPPED.** Three refinements to the
isochrone `route_leg` (`optimizer.py`), all env-tunable so they're A/B-able: (1) **VMG gate** —
`_vmg_headings()` computes the true best-VMG upwind (beat) and downwind (run) compass headings at the
local TWS (argmax of `stw·cos(twa)` up / `−stw·cos(twa)` down over the polar band) and **injects them
into the heading fan**, so the router sails the exact optimum tacking/gybing angle instead of being
limited to the nearest coarse 12° grid heading. (2) **Cone prune** — the fan only opens headings
within `ROUTE_CONE_DEG` (120°) of the bearing-to-mark (plus the VMG angles, always kept), dropping the
truly-backward third of the compass; if the whole cone is obstacle-blocked at a node it **reopens the
full fan** so land/island avoidance can still detour. (3) **Anti-over-tack** — a `ROUTE_TACK_COST_S`
(30 s) maneuver penalty subtracts distance-made-good from any step that crosses the wind to the other
tack vs the node's incoming heading, so the isochrone prune disfavors spurious tacking and the route
tacks only when a shift makes the new board genuinely pay. Verified: unit tests (VMG beat/run headings
correct; upwind leg tacks at the VMG angle + reaches the mark; heavy tack-cost ≤ zero-cost tack count;
obstacle detour still works) + a real end-to-end Mackinac `cove_island` run (43 h, coverage 1.0, not
degraded, sail plan attached, reaches the finish). Tunables `ROUTE_CONE_DEG` / `ROUTE_TACK_COST_S`.

**Routing fidelity 2e — finish/mark over-tack ("scramble") fixes: SHIPPED (dev).** Diagnosed from a
real Bayview Mackinac `cove_island` run whose FINISH leg was a light-air beat that the optimizer turned
into a degenerate zig-zag — dozens of tiny tacks, ~3x oversail, arriving ~2x slower than necessary
(the "crazy scramble near the finish" the user reported). Root cause was structural in the isochrone
`route_leg`: the 2c tack penalty was a one-step distance haircut (not cumulative), the prune buckets by
bearing-from-leg-start so opposite-tack nodes never eliminate each other, and nothing committed the boat
to a layline near the mark. Three env-flagged fixes (`optimizer.py`): (1) **`ROUTE_LAYLINE_COMMIT`** —
once a node within `ROUTE_LAYLINE_COMMIT_NM` (10 nm) of the mark can lay it (bears more than the VMG
half-angle `_vmg_twa()` off the LOCAL wind axis), drop the opposite-tack headings so it fetches the
layline instead of free-tacking; re-checked each generation against node-local wind (a real shift
re-opens it), and only on the final approach so strategic side choice is preserved farther out.
(2) **`ROUTE_TACK_CUMULATIVE`** — the tack cost accrues into a per-path penalty `pen` (and the node ETA)
so the prune ranks by range-made-good-net-of-maneuvers (`rng_eff = rng − pen`); repeated alternation
genuinely loses ground instead of a ~5% per-step nudge. (3) **`ROUTE_MARK_POS_PRUNE`** — within
`ROUTE_MARK_PRUNE_NM` (6 nm) of the mark the prune buckets by POSITION (a `ROUTE_MARK_PRUNE_CELL_NM`
≈0.25 nm lat/lon cell) instead of bearing-from-start, so near-colocated opposite-tack nodes compete and
the least-tacked wins. **A/B against ONE frozen GFS+NAM+HRRR field** (Jun-29 19:00Z start, the reported
case): baseline finish leg 27 tacks / 2.7x oversail / 83 h total → **#2 alone 0 tacks / 1.1x / 40 h**,
**#3 alone 0 tacks / 1.1x / 40 h** (each independently kills the scramble; #1 is the clean-layline
finisher for genuine-beat finishes). Anti-under-tack verified: a steady dead-upwind leg still tacks the
minimum and reaches; `test_routing_2c/2d/2e` all green. **Also fixed the per-leg tack COUNTER** — it
classified tack-side off a single frozen leg-start wind, so on a clocking leg every shift-following
heading swing was mis-classified; it **mis-reports in either direction** (on the frozen-field baseline
it actually UNDER-counted the carnage 135 vs 173 real maneuvers, and would have shown the clean route as
a false "0 tacks" when it really makes 3). Now each segment's board is classified against the wind LOCAL
to where/when it's sailed, so the count is the true tacks-up/gybes-down tally; route geometry is
unchanged (metric-only). On the reported case: baseline finish ≈173 real maneuvers → 3 with the fixes,
now reported honestly. Tunables `ROUTE_LAYLINE_COMMIT[_NM]` / `ROUTE_TACK_CUMULATIVE` /
`ROUTE_MARK_POS_PRUNE` / `ROUTE_MARK_PRUNE_NM` / `ROUTE_MARK_PRUNE_CELL_NM` (all default ON).

**Routing fidelity 2f — island ROUNDING-SIDE enforcement: SHIPPED (dev).** Obstacle avoidance (2a)
kept the route off islands but on EITHER side; a race often says "leave Bois Blanc to port / Duck
Islands to starboard" — and that side was thrown away (`course_to_marks`/`course_roundings` drop all
`type:"island"` marks). Per the scoping rule **we only enforce a side when the island is a MARK OF THE
RACE** — i.e. its `rounding` is `port`/`starboard`; a plain hazard island (`rounding:"none"`) stays
avoided either side. Enforcement is a **wrong-side barrier** in the obstacle mask (`geo/obstacles.py`):
for each marked island, `_island_rounding_marks()` finds the leg it sits on (transit bearing from the
nearest preceding nav point to the nearest following one, islands skipped, gate/finish→midpoints), and
`_fill_wrong_side_barrier()` rasterizes a wall on the ILLEGAL side (perpendicular to that axis, within
|along| ≤ radius + `ROUTE_ROUNDSIDE_BAND_NM`), so the only gap is the legal hand. Source-independent
(runs in ENC and GSHHG/NE backstop alike — it's a race rule, not an obstacle) and applied AFTER the
waypoint carve so it can't be re-opened; barrier provenance lands in `obstacles.geometry.rounding_barriers`
and the Gameplan map draws a P/S marker + a tick toward the legal side (`mapview.js`). Verified
(`test_routing_2f.py`): scoping (a `none` hazard island gets no side); a controlled open-water flip
(natural route takes the WRONG side → barrier flips it to the legal side, both port and starboard,
still reaching the mark); and the real cove_island Duck(stbd)/BoisBlanc(port) barriers (legal side open,
illegal blocked) — plus an offline gate→finish leg routed through the full mask still reaches the finish
(no over-block). Tunables `ROUTE_ROUNDSIDE_ISLANDS` (default ON) / `ROUTE_ROUNDSIDE_BAND_NM`.
**Crew-facing roundings summary (2f follow-up):** the route now ENFORCES island sides but nothing TOLD
the crew — so `race_def.marks_with_side()` returns the ordered required sides for ALL marks (nav marks
AND islands with rounding port/starboard, plus gates), the optimize result carries it as `roundings`,
and `briefing()` states it explicitly (Opus prompt + deterministic line: "Roundings: … leave Duck
Islands to starboard; leave Bois Blanc Island to port"). Verified `test_roundings.py` (ordering,
island inclusion, briefing text).

**Routing fidelity 2g — SAIL-AWARE routing (per-sail polars + a peel cost): SHIPPED (dev).** 2b attached
a per-leg sail LABEL but the optimizer still routed on the Best-Performance *envelope* (the max-over-sails
speed) and peeled for FREE — so a route could thrash sails across a crossover at zero cost and the sail
plan was a post-hoc per-leg guess. 2g makes the sail a first-class part of the isochrone search. **Data:**
`build_speed_guide.py` now also emits `vps/db/seed/sr33_sail_polars.json` — the speed of EACH inventory
sail (J1/A2/A3/S2) across its rated TWA domain (the cert already rates every sail; the envelope is just
their max), loaded by `polars.sail_polars()` (env `SAIL_POLARS_FILE`, copied to `/srv` in the lab image);
absent → the optimizer routes on the envelope exactly as before. **Search (`optimizer.py`):** `route_leg`
carries the current sail in the node state; per step `sail_step` HOLDS it (at its OWN, slower-off-optimal
per-sail speed) until it's `ROUTE_PEEL_HOLD_TOL` (6%) off the envelope-optimal sail, then PEELS to the
optimal sail at full speed — a peel costs `ROUTE_PEEL_COST_S` (90 s honest ETA) + a one-off
`ROUTE_PEEL_PRUNE_S` prune penalty (mirrors the cumulative tack cost), so the isochrone disfavors a course
needing an extra peel and the hysteresis dead-band stops crossover-boundary thrash. A kite is `0` outside
its rated TWA domain (can't fly it hard upwind → a forced peel); a jib change-down (J1→J2/J3) shares the
J1 curve, so it's a free relabel, not a routing peel. The carried sail threads across marks
(`start_sail`) so a peel at a rounding counts once; the route's `sail_plan` is rebuilt from the
isochrone's OWN sail track (where it actually peeled) — physically real, not a per-leg guess — and the
result carries per-leg `peels` + `total_peels`. **Surfaced:** the Gameplan cockpit gains a *sail peels*
stat + a per-leg peel badge + a CSV peels column; `briefing()` states the real sail plan + peel count.
Env-flagged `ROUTE_SAIL_AWARE` (default ON) for A/B; off ⇒ envelope routing, geometry byte-identical.
Verified `test_routing_2g.py` (per-sail load + domain gate, carrying the wrong sail peels to the right one
both ways, a within-tolerance sub-optimal sail is HELD with no thrash, SAIL_AWARE off reproduces the
envelope baseline exactly, starting on the optimal sail adds no peel) + an end-to-end on the real
cove_island course (`S2 → J1`, one peel — the post-hoc labeler's spurious A3 transient correctly NOT
flown) + the deployed lab container. Tunables `ROUTE_SAIL_AWARE` / `ROUTE_PEEL_COST_S` /
`ROUTE_PEEL_PRUNE_S` / `ROUTE_PEEL_HOLD_TOL` / `ROUTE_SAIL_DOMAIN_MARGIN`.

**Water currents — set & drift (routing-fidelity 2d lever a): SHIPPED.** The optimizer accounts for
water current: in `route_leg` each step advances by the boat's water-velocity (polar speed on its
heading) PLUS the current's drift, so the route crabs into a cross stream and ETAs reflect a fair/foul
current (`vps/lab/app/current.py`). `build_currentfield` returns a real **`GLOFSCurrent`** over the
course bbox — NOAA Great-Lakes OFS surface currents (Lake Michigan-Huron **LMHOFS**) via the CO-OPS
THREDDS OPeNDAP server (freshest-cycle pick + per-slice timeout + in-process cache); outside the
Great-Lakes domain / on any fetch miss it degrades to **`ZeroCurrent`** (route unchanged). The optimize
result carries `current` (the field `status()`) and a **`current_grid`** (set/drift sampled on the SAME
bbox + times as the wind grid, emitted only when something actually flows). **Surfaced in the Gameplan
cockpit:** a *current* stat (source · slices · peak drift), a teal **Current arrows overlay** on the
slippy map (`mapview.js` `drawCurrent`, scrubbed by the same forecast slider, toggle + legend in the
Control Center), and a current line in the briefing (Opus weaves it in; deterministic fallback states
source + slices). Verified live on the real cove_island course (LMHOFS 18Z, 8 slices, ~1 kn peak,
44 grid frames; Playwright-confirmed the toggle/legend/stat render with zero console errors). The
current is threaded through ALL routing, not just the main optimize: the **per-model candidate fan**
(`_per_model_paths`, the confidence-fan overlay) and the **playbook** consensus + every scenario
sub-field (`playbook.build_playbook` builds its own `cur`; Lab-2b `synthesis` inherits it) all crab
through the same stream, so the variants/fan reflect a fair/foul current too (verified live — the
playbook result carries the LMHOFS `current` status). Tunables `CURRENTS_ENABLED` / `CURRENTS_STEP_H` /
`CURRENTS_MAX_SLICES` / `CURRENTS_FETCH_TIMEOUT` / `CURRENTS_CYCLE_LAG_H`.

**Realized (achievable) speed — helm + sea state (routing-fidelity 2d lever d, fuzzy baseline): PHASE 1
SHIPPED.** The ORC polar is a FLAT-WATER, perfectly-sailed target; the boat never quite makes it. The
optimizer routes on **realized** speed = `polar × helm_factor × wave_factor(hs, twa)`, so ETAs are
achievable (not theoretical) and the gap to the polar is a coaching number — the fuzzy-adherence
baseline (perflab §5). **Helm-skill factor**: `BoatProfile.helm_factor` (0–1, default 1.0, editable in
the Gameplan boat panel as `Helm %`; the Lab-4 loop can refine it from real tracks). **Sea state**: a
`WaveField` seam (`vps/lab/app/wave.py`) parallel to wind/current — `wave_at(lat,lon,epoch) → hs_m`;
phase 1 ships the seam (`ZeroWave` default = no behaviour change + `ConstantWave`/`WAVES_CONST_HS`
what-if), **phase 2** wires a real Great-Lakes wave provider (NOAA GLWU Hs via THREDDS, like the GLOFS
current provider). The degradation MODEL (`optimizer._wave_factor`) is source-agnostic and
**deliberately CONSERVATIVE** (under-correcting beats distorting the route on an uncalibrated guess): a
low-Hs **deadband** (`ROUTE_WAVE_HS_DEADBAND`=0.5 m — small chop costs NOTHING, so ripples never perturb
the route), then a gentle linear slope on the *excess* Hs scaled by point of sail (head sea hurts most
`ROUTE_WAVE_K_UP`=0.04/m → only ~6% at 2 m, following sea least 0.01/m), capped by `ROUTE_WAVE_FLOOR`=0.6.
The coefficients are PRIORS to be calibrated from the boat's realized-polar archive (Lab-4), not trusted
as-is. **Per-run opt-out:** the optimize/playbook endpoints take `use_waves` (Gameplan checkbox
"Sea-state (waves)", default on) → uncheck for flat-water (polar) routing; the helm factor still applies
(crew efficiency, not waves). Threaded through the main optimize + the per-model fan + the playbook
consensus/variants (helm read from the active boat via `boats.active_helm_factor`); the result carries a
`realized` roll-up (`realized_pct`/`helm_factor`/`sea_state_hs_mean`) + per-leg `realized_factor`; the
cockpit shows a *realized %* stat and the briefing states "routing at ~N% of the flat-water polar".
Default no-op (helm 1.0 + flat water ⇒ geometry/ETA byte-identical). Verified `test_routing_realized.py`
(wave-factor shape + deadband + point-of-sail scaling + floor, helm slows + is reported, sea state
degrades a beat more than a run, default == baseline) + in-container + Playwright. Tunables
`ROUTE_WAVE_HS_DEADBAND` / `ROUTE_WAVE_K_UP` / `ROUTE_WAVE_K_REACH` / `ROUTE_WAVE_K_DOWN` /
`ROUTE_WAVE_FLOOR` / `WAVES_ENABLED` / `WAVES_CONST_HS` + the per-run `use_waves`.

**Over-correction guards (why this won't distort the route):** discussed 2026-06-30 — the model can't run
away (deadband + floor + conservative slopes; ~6% upwind at 2 m, downwind barely touched), it's OFF by
default until a real wave field exists (phase 2) and per-run opt-out-able, and the route-*reshaping*
effect (vs the ETA effect) only matters once a spatially-varying field is in — where the plan is to gate
reshaping on wave-field confidence and to calibrate the coefficients from the boat's own logs rather than
trust the linear prior. Keep `helm_factor` a FLAT-WATER number so it doesn't double-count waves.

**Optimizer UI study + restyle — `docs/OPTIMIZER_UI_STUDY.md`** (Orca + Expedition gap analysis). Tier 0
(ensemble-control fix + ECMWF-ENS wired as a separate 51-member `ecmwf-ens` ensemble source) + Tier 1
quick wins (map wind color-scale legend, forecast ▶/⏸ animation, grouped control cards Course/Boat &
charts/Weather models, ⇄ tack badge in the leg table) SHIPPED. **Tier 2a / PR-3 SHIPPED** — the
optimizer emits down-sampled **isochrone frontier** polylines (`route_leg(capture=)`) + **laylines**
into each beat/run mark (`_layline_pair`); `mapview.js` draws them as toggleable map layers
(Isochrones default off, Laylines default on) via a `drawExplore` canvas overlay; clicking a leg row
(`MapView.focusLeg`) highlights that segment + snaps the forecast slider to its ETA; and a client-side
**CSV export** of the leg table. **Tier 2b / PR-4 SHIPPED** — the **per-model candidate-paths overlay**:
opt-in "Per-model route fan" (`optimize_course(per_model=)` → `_per_model_paths` splits the blended
field per model, routes each reusing the obstacle field, emits `candidate_paths`); `mapview.js` draws a
colour-per-model route fan under the chosen route (Model-routes toggle + per-model legend) — the
multi-model-confidence moat made VISUAL (tight = models agree, spread = a real decision). Untrustworthy
solo routes (degraded / timed-out / 0.5×–1.6× off the blended hours) are dropped, not drawn. We already
win the two dims both references are weak on — forecast confidence + a reviewable sail model. **Tier-2
polish SHIPPED** — **2.4** wind display modes (a Layers selector: arrows / **barbs** standard offshore
convention / **shaded** TWS heatmap, all keeping the color ramp + confidence-fade); **2.5** an
**Auto / Fast / Fine** routing-resolution selector (`optimizer.RESOLUTIONS` → heading-fan deg +
per-leg step ceiling + time budget, threaded through `optimize_course(resolution=)` / `route_leg`)
with a one-line explainer + an inline **common-error checklist** in the degraded banner. **Tier 3
restyle SHIPPED** (built from the study's mockups; the user's own Orca notes fold in later as
refinements) — **3.1** the four scattered map L.Controls collapsed into ONE bottom-docked, collapsible
**Control Center** (`mapview.js` `.mv-cc`: scrubber + layer toggles + wind-mode + Follow + legend);
**3.2** `renderOptResult` is now a map-led **cockpit** grid (`.opt-cockpit`: the slippy map is the
hero ~620 px; stats + collapsible `<details>` rail = Legs / Briefing / Wind field, stacks on narrow);
**3.3** the timeline scrub now **pans the map to the projected boat position** (Follow toggle, default
on) — Orca's "ride along". NEXT = fold in the user's own Orca UX notes as refinements when they arrive.

**Copilot track — crew-facing narration ✅ + proactive auto-coach timer ✅ + PLAYBOOK-ADHERENCE
dashboard tile ✅ + collision/AIS safety callout ✅** (the copilot interprets the signed playbook +
boat sail model; see "Onboard LLM copilot"). **Collision callout** (`narrate._safety_callout`):
narration now gathers the engine's `/ais` and voices the nearest CLOSING contact inside the CPA/TCPA
guard as a TOP-priority safety callout ("Collision risk: <vessel> — CPA x nm in y min") — the one thing
the copilot interrupts for, always legal in-race (own receiver + own math). act ≤0.5 nm/12 min = "now",
watch ≤1.5 nm/30 min = "soon"; level is in the callout id so a watch→act escalation re-voices. Verified
`bench_copilot.test_safety_callout` + end-to-end against the live :8200 engine (voiced a real
CPA-0.0 nm closing target). Tunables `COPILOT_AIS_{ACT,WATCH}_{CPA_NM,TCPA_MIN}`.
**Handicap-rival callout** (`narrate._fleet_callout`): narration also gathers the engine's `/fleet` and
voices the top roster competitor we're racing — a RIVAL (within the ±3-min corrected-time band) or one
projected AHEAD of us on corrected ("{boat} ahead on corrected — projected to beat us by m:ss …
consider covering"). Grounded in `get_fleet` (onboard: own AIS + frozen roster + own corrected-time
math — in-race-legal tactical layer), confidence-gated (`COPILOT_FLEET_MIN_CONF`=0.4), category `fleet`
(priority below safety/rounding/sail, persist-2 raise-slow), tag in the id so behind→rival→ahead
re-voices. The spoken counterpart to the dashboard AIS/Fleet tile's corrected-time overlay. Verified
`bench_copilot.test_fleet_callout`. **Handicap-aware fleet tactics ✅** (incl. the verified YB/bycmack over-the-horizon tracker
source) — see "Handicap-aware fleet tactics". **Next:** (open) — island rounding-side enforcement is now
in (routing fidelity 2f: marked islands only); the overstand/2d gate + nav-mark side were already in.

## Handicap-aware fleet tactics

`vps/agent/app/fleet.py` extends the collision-only AIS layer to TACTICAL: it matches AIS targets to
the pre-loaded race roster (MMSI exact → high confidence, else fuzzy name-match → lower) and turns the
matched competitors into intelligence — each boat's distance-to-finish (projected onto the course
polyline), on-water lead/lag, leverage (signed cross-track), and the **ORC corrected-time delta**: who
you actually need to beat and by how much, NOT raw on-water position. A negative delta = that boat is
projected to BEAT us on handicap (a rival/threat). Corrected-time supports **ToT** (corrected = elapsed
× coeff; coeff from the entry's `rating`, else derived from GPH) and **ToD** (allowance = GPH s/nm),
selected from the race `scoring` block; the delta is the part of the race still in play (projected to
the finish, no race-start time needed). Everything is **fuzzy + confidence-flagged** (perflab item-5):
AIS coverage is partial, matching imperfect, corrected-time a projection — every row carries a
confidence and the gaps are stated. Unmatched vessels stay in the collision layer (`get_ais_targets`).

Source-agnostic on the 9.0 seam (`datasource.active()` + the `ais` helpers), so the identical code
runs cloud + onboard. The fleet **homework** (roster + scoring + own rating) is loaded with
`shared.race_def.fleet_blob(definition, own)` via **`POST /fleet/load`** (onboard engine + extendable
to cloud) → persisted by `datasource.save_fleet()` (cloud → `app_state` key `race_fleet`; onboard →
the engine SQLite `kv` table) — frozen at the gun, legal in-race. The onboard engine serves **`GET
/fleet`**; the cloud agent has a gated **`get_fleet`** tool (customized tactical advice → withheld
racing, like tactics) + prompt section + fallback. The crew dashboard's **AIS / Fleet** tile overlays
corrected-time standings under the collision rows (collision keeps status primacy = safety; fleet
takes the tile face when traffic is clear), with Δ-corrected arrows (▲ = a rival ahead on handicap).
**Verified:** unit test `vps/agent/test_fleet.py` (MMSI + name matching, course-progress DTF/leverage,
ToT + ToD corrected deltas, tags, graceful no-roster) + onboard e2e (load roster → `GET /fleet` matched
2 boats by MMSI against the bench's 34 live AIS targets, with DTF/lead/leverage/corrected deltas) +
Playwright (10-tile grid, live + demo AIS/Fleet tile shows the fleet section with Δ arrows + rivals).
**Over-the-horizon public tracker (perflab item-6, BUILT).** A permitted public race tracker (YB/
TracTrac-style, e.g. bycmack.com/tracking) is now a SECOND, DELAYED fleet source. `vps/agent/app/
tracker.py` does a best-effort, **cached** PULL (TTL `TRACKER_REFRESH_S`, never blocks the per-poll
fleet view) via pluggable providers (`generic_json` for the common JSON/XHR endpoint behind the web UI
via a per-race field map; `sample` for the bench) → normalized fixes, with **every position aged +
confidence-reduced** (`_age_conf` decays to a floor past `TRACKER_STALE_MIN`) — a fix is never shown as
current. `fleet.get_fleet()` folds it in two ways: **(a) identity** — an unmatched AIS target sitting on
a roster boat's tracker fix (within `TRACKER_MATCH_NM`) is resolved by position (`matched_by=
tracker_position`, source stays `ais`), filling the AIS↔roster MMSI gap; **(b) over-the-horizon** —
roster boats on the tracker but not on our AIS at all become aged rows (`source=tracker`, `age_s`,
reduced confidence). The per-race gate is **`rules_profile.tracker_permitted`** (authoritative;
default conservative — off if unset; for Bayview Mackinac the user confirmed it's allowed); the config
(provider/race/url/field-map/delay) is a `RaceDefinition.tracker` block carried by `fleet_blob` (its
`permitted` is driven strictly by `tracker_permitted`). The response gains `count_ais`/`count_tracker`
+ a `tracker` status block; the dashboard tile flags tracker rows with a ⌛ age marker + an
"over the horizon" note (live AIS outranks the delayed tracker for the face). Verified: `test_fleet.py`
tracker cases (aging/confidence decay, over-horizon rows, permission gate off, identity-resolution) +
onboard e2e (`sample` provider → 3 over-horizon rows aged ~15 min, conf-reduced; gate-off withholds) +
Playwright (⌛ marker renders, over-horizon text).
**bycmack endpoint VERIFIED 2026-06-28:** bycmack.com/tracking is **YB Tracking (yb.tl)**. The `yb`
provider (`tracker.py`, alias `bycmack`/`ybtracking`) pulls the viewer's JSON positions API
`https://cf.yb.tl/API3/Race/<race>/GetPositions?t=0` — per-team `teams[].name` + latest `positions[]`
with `latitude`/`longitude`/`sogKnots`/`cog`/`gpsAtMillis`(epoch-ms)/`dtfNm`. Name + SOG + COG + time
in ONE JSON call — no binary decode, no RaceSetup join. Set `tracker.race` (the yb.tl id, convention
`bayviewmack<year>`) + optional `host`; the url is built from those. Confirmed live against the real
`bayviewmack2025` feed (108 boats parsed); `bayviewmack2026` returns `{"error":...}` until the event is
published (~July 2026) → the provider degrades to no positions gracefully. (YB also serves a big-endian
binary `…/BIN/<race>/AllPositions3` track feed, lat/lon=int/1e5 — not needed; the JSON carries all.)
**HONEST v1 scope:** corrected-time is a projection (uses SOG toward the finish, common-division-start
assumption); the entry list rarely carries MMSI so matching is partial. The engine computes; the LLM
only interprets.

## Onboard LLM copilot — Orin Nano (Phase 9.4, Tier 2)

The optional in-race conversational LLM (`docs/ONBOARD_ENGINE_SCOPING.md` §3). A **Jetson Orin Nano
8GB (Super)** dedicated to inference, **separate** from the Pi 4 that runs the deterministic engine
(Tier 1) — they talk over boat-local Wi-Fi. Legal in-race because the boat's own computer reasoning
over its own sensors + pre-loaded homework + common public data is not "outside help"; it never
phones the cloud mid-race, never does the math (the engine does), never invents strategy outside the
playbook. **The Orin is in hand as of 2026-06-18.**

**Runtime appliance: LIVE.** The Orin is a turnkey headless offline-inference appliance —
**Ollama serving `qwen2.5:7b-instruct-q4_K_M` on `:11434`** (OpenAI `/v1`), built from source with a
`cuda_v13`@sm_87 GPU backend, ~12 tok/s (memory-bandwidth-bound; the strict 20 tok/s milestone was
relaxed — quality over speed), reboot-verified + systemd-persistent, reachable over Tailscale. (This
replaces the originally-planned MLC-on-:9000 path: JetPack 7.2/R39 was too new for jetson-containers'
MLC matrix, and R39/CUDA-13.2 removed the R36.4.x llama.cpp regression that ruled Ollama out — full
story in the Orin bring-up memory + `pi/orin/DEPLOYMENT.md`.) The copilot only sees the OpenAI `/v1`
contract, so the runtime stays swappable.

**SR33 copilot — decision-support layer: BUILT** (`pi/orin/copilot/`, the next 9.4 increment, first
slice). A thin FastAPI service (**:8300**, runs on the Orin) that turns the Tier-1 engine's facts into
**bounded, grounded decision support** via the local LLM. The guardrails are structural, not just
prompt: the LLM's only capabilities are a closed set of **read-only engine-fact tools** (it can't do
math or fetch anything else — the engine does the math); every `factor`/`recommendation` must be
`grounded_in` a tool actually used or it's **dropped** by `brief.validate()`; caveats are computed by
the engine (`structural_caveats`), not authored by the model; every brief carries a standing
disclaimer + confidence; and if the LLM is off/slow/ungrounded the service returns the **deterministic
brief** built from the same facts (always works, never depends on the model). A frozen **playbook**
(Lab-2 output) loads via `PLAYBOOK_PATH` — the copilot selects/interprets its variants, never
originates strategy; absent → it says so. Endpoints: `GET /health` (honest llm/deterministic/
unreachable modes + the auto-coach state), `GET /tools` (the bounded surface), `POST /brief`, `POST
/narrate` (proactive callouts on demand), `GET /coach` (the auto-coach held state), `GET /adherence`,
`GET /snapshot`. **Bench-verified
on the real Orin** (over a Tailscale SSH forward of :11434, Pi engine on :8200): deterministic path
green; LLM tool-loop returns a grounded brief in ~45 s warm (the model calls `get_forecast` on demand);
graceful fallback fires on the ~2 min cold model-load or ungrounded JSON. Exit test:
`python3 -m copilot.bench_copilot [--llm]`.
**Proactive auto-coach timer ✅ (`coach.py`):** `make_narration` is PULL (the iPad asks); the auto-coach
is the TIMER that DRIVES it. A background loop in the copilot lifespan ticks every `COACH_INTERVAL_S`
(env `COPILOT_COACH_INTERVAL_S`, default 30 s; `COPILOT_COACH=false` disables), runs the narration
engine, and HOLDS the latest result — so the copilot volunteers coaching whether or not anything polls,
and the TIME-DRIVEN callouts (a closing-traffic COLLISION warning — safety, top priority; 15/10/5-min
rounding prep, a playbook branch firing, a sail change-down) fire on the clock. It mirrors the cloud alerting loop; `narrate.step`'s raise-slow/clear-fast
speak-once already dedups, the loop just calls it on a schedule + keeps a short spoken history. The LLM
only phrases NEW callouts (most ticks are deterministic + cheap), following `USE_LLM`. `GET /coach`
reads the held state with no recompute (the canonical proactive surface; `POST /narrate` is the
on-demand/debug equivalent — don't poll both for one route, they share the dedup). The crew dashboard
shows a **COACH speech line** in the commentary panel (`fetchCoach` polls `/copilot/coach` ~15 s; the
last volunteered line + "Ns ago", hidden when there's nothing to say). Verified: `bench_copilot.test_coach_logic`
(held state / history-on-new / nothing-new / error-survival), live `/coach`+`/health` end-to-end (timer
ticks against the Pi engine), Playwright (coach line renders the spoken history, only the known-unrelated
`/copilot/adherence` 404). **Next copilot increment = (open).** See
`pi/orin/copilot/README.md`. The **iPad crew dashboard** that surfaces the copilot graphically (a
fixed, all-items-visible status grid that the LLM scores green/yellow/red with color-blind-safe
redundant encoding + a commentary panel + tap-to-detail LLM deep-dives) is designed/locked in
`docs/COPILOT_DASHBOARD.md` and **BUILT** (`pi/console/dashboard/`, served at `:8091/dashboard/`):
phases 1–4 shipped 2026-06-19/20 — static prototype → live engine wiring + deterministic status →
LLM commentary/status-refine (`copilot dashboard_brief.py`, `POST /dashboard`) → streamed
tap-to-detail (`POST /detail`), plus wind-trend charts, forecast-vs-actual verification, demo
scenarios, day/night, feedback widget. **10 higher-order tiles** (`vmg, wind, tactics, playbook,
forecast, sail, eta, ais, charge, data`) on a 5×2 grid; the **AIS / Fleet** tile is built (see "AIS /
Fleet dashboard tile" below) and the **PLAYBOOK-ADHERENCE** tile (the last "later tile") is built —
deterministic `pi/orin/copilot/adherence.py` + `GET /copilot/adherence` compares the frozen Lab-2
variants (recommended start + each variant's `what_flips_it` first-beat-side trigger) against the
engine's tactical read → ok (on plan) / watch (oscillating lean) / act (a persistent shift fires the
branch — names the variant to switch to); polled on its own ~8 s cadence; `na` with no playbook
aboard. (Also fixed: `narrate.py`'s playbook-branch callout read `tac["persistent"]` flat vs the
engine's nested `tac["wind"]["persistent"]`, so it never fired — now reads the nested path.) Runtime
bring-up files (the originally-MLC plan, port **9000**) — note
they describe MLC, the unit runs Ollama: `pi/orin/`:
- **`SETUP.md`** — the bring-up runbook: flash JetPack 6.2 (L4T R36.4.x) + the QSPI firmware →
  Super mode (`sudo nvpmodel -m 2` + `jetson_clocks`) → NVIDIA-default docker runtime →
  `jetson-containers` → MLC → benchmark Qwen2.5-7B INT4 → serve → autostart → `tegrastats` thermal.
- **`serve.sh`** — launch the OpenAI-compatible MLC server (`MODEL`/`PORT` env, idempotent restart).
- **`bench.sh`** — benchmark one model's prefill/decode tok/s via MLC (A/B the 7B vs a 3-4B).
- **`smoke_api.py`** — pure-stdlib client that sends a "narrate these engine facts" prompt, prints
  the answer + latency + effective tok/s, pass/fail — **the milestone's exit test** (a grounded
  answer, fully offline, at usable latency; no SR33 tool-calling yet).
- **`models.md`** — A/B matrix (NVIDIA numbers + a column for measured results) + how to confirm the
  exact MLC model id on-unit.
- **`../systemd/sr33-orin-llm.service`** — the MLC-plan autostart unit (superseded by the live Ollama
  systemd unit on the unit itself); **`../systemd/sr33-orin-copilot.service`** runs the copilot svc.

See `pi/orin/README.md` + `pi/orin/copilot/README.md`. The runtime as-built (Ollama-from-source) is
documented in `pi/orin/DEPLOYMENT.md`; the copilot decision-support layer is bench-verified on real
hardware (above).
