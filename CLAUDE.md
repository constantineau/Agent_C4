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
| **9** 🔶 | Onboard + C4 Performance Lab (three-tier pivot) | **9.0 data-access abstraction ✅ · 9.1 onboard engine service ✅ · 9.2 race gate + iPad onboard console ✅ · Lab-0 race ingestion + course loader ✅ · Lab-1 multi-model GRIB optimizer ✅ · Lab-2a/2b branching playbook bundle ✅ (fan-out → variants → Opus synthesis → signed, onboard-loadable artifact) · 9.4 Orin LLM appliance live (Ollama+Qwen2.5-7B :11434) + copilot decision-support layer ✅ (`pi/orin/copilot`)**; next the copilot crew-facing narration increment + routing-fidelity (b) sail-specific polars — see `docs/ONBOARD_ENGINE_SCOPING.md` |

**Current status:** Phases 0–6 built and bench-verified; Phase 7 started; **Phase 9 in progress
(9.0 data-access abstraction ✅, 9.1 onboard engine service ✅ — see "Onboard engine service",
9.2 server-side race gate ✅ + iPad onboard console ✅ — see "Race-mode gate" / "Onboard race
console"; the C4 Performance Lab (`vps/lab`) is live with **Lab-0 race ingestion + course loader ✅**
and **Lab-1 the multi-model GRIB optimizer ✅** + **Lab-2a/2b the branching playbook bundle ✅** —
see "C4 Performance Lab"; next is the copilot crew-facing narration + routing-fidelity sail polars;
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
context; `polars_sr33.sql` = real polars for the DB). The agent advises sail selection and
crossovers/peels from the sail plan. Regenerate after a cert update:
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

**Obstacle avoidance (routing fidelity 2a, from the Bitsailor gap analysis): SHIPPED 2026-06-20.**
`vps/lab/app/geo/` keeps the optimizer's route off land — **race-agnostic**: three layers rasterize
into one boolean mask the isochrone prune queries (`blocked`/`crosses`): (1) a GLOBAL coastline
(`coastline.py`, Natural Earth 1:10m `land∧¬lake`, fetched once to the `lab_coastline` volume +
auto-clipped to the course bbox → works for any race, ocean or lake; source pluggable for a higher-res
upgrade), (2) the race's `zones[]` (exclusion/hazard/tss), (3) the race's geocoded `island` marks
buffered to a disk (`radius_nm`; islands are obstacles, NOT waypoints — `course_to_marks` omits them
as waypoints). `optimize_course(avoid=True)` builds the field (cached by `cache_key`, so Lab-2's
same-course scenarios share one mask) + threads it through `route_leg`; `POST /api/optimize` takes
`avoid_land` (default true) and returns an `obstacles` summary + `obstacle_steps_avoided`; the Gameplan
tab overlays coast/islands/zones on the route canvas. **A/B-verified on the real Cove GRIB route:**
avoid OFF passes 1.9 nm from Bois Blanc center (cuts across it); ON clears at 5.7 nm for +0.3 nm/+1 tack.
Caveats: NE 1:10m is coarse near shore + misses sub-nm islands (the race island/zone layer covers the
critical ones; island coords geocoded `approx` → human-review); rounding SIDE not yet enforced (avoided
either side). Tunables `GEO_RES_DEG`/`GEO_ISLAND_NM`. See `vps/lab/README.md`.

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
each variant's flip trigger. UI Playwright-verified. See `vps/lab/README.md`. **Next: the copilot's
crew-facing narration increment** (it now has a real, signed playbook to interpret) + routing-fidelity
(b) sail-specific polars.

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
unreachable modes), `GET /tools` (the bounded surface), `POST /brief`, `GET /snapshot`. **Bench-verified
on the real Orin** (over a Tailscale SSH forward of :11434, Pi engine on :8200): deterministic path
green; LLM tool-loop returns a grounded brief in ~45 s warm (the model calls `get_forecast` on demand);
graceful fallback fires on the ~2 min cold model-load or ungrounded JSON. Exit test:
`python3 -m copilot.bench_copilot [--llm]`. **Next copilot increment = crew-facing narration.** See
`pi/orin/copilot/README.md`. The **iPad crew dashboard** that surfaces the copilot graphically (a
fixed, all-items-visible status grid that the LLM scores green/yellow/red with color-blind-safe
redundant encoding + a commentary panel + tap-to-detail LLM deep-dives) is designed/locked in
`docs/COPILOT_DASHBOARD.md` (not built yet). Runtime bring-up files (the originally-MLC plan, port **9000**) — note
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
