# Agent_C4 — SR33 AI Navigator

LLM-powered navigator/coach/data-archive for the SR33 racing yacht. Boat NMEA 2000 →
Raspberry Pi (Signal K) → 15-s telemetry aggregates pushed over Starlink → this VPS
(TimescaleDB + Claude-API agent) → mobile web chat for the crew.

Full project brief: Google Doc `1lUqXt3JZ8Cao467CfGT9CP3O75wtuO6z3CvoMr56v5Y`.
This file is the operational summary (brief §§1–8); read the doc for full context.

## Architecture (two halves, linked over Starlink)

- **Boat:** NMEA 2000 backbone → Pi 4 + PICAN-M HAT. Signal K decodes N2K → JSON;
  full-resolution local archive; a Python uplink service POSTs 15-s aggregates to the VPS.
  Orca Core/app stay unchanged — the Pi is a silent additional listener.
- **Cloud (this VPS):** nginx+TLS → FastAPI ingestion API → TimescaleDB → agent service
  (Claude tool-use loop, alerting, summarizer) → web app.

**Design principles:** push-only from the boat (Starlink CGNAT, no inbound; admin via
Tailscale). Boat is source of truth (link outage loses nothing). The LLM never sees raw
NMEA — it reads facts through SQL-backed tools.

**Direction — the three-tier pivot (2026-06-17; see "Racing-rules caveat" + `docs/ONBOARD_ENGINE_SCOPING.md`):**
RRS 41 forces *customized in-race* coaching to be computed **onboard**, so the roadmap adds a
**Phase 9 / Onboard + C4 Performance Lab track**: (1) relocate the deterministic engine
(routing/tactics/sails/polars/nav/fatigue — plain physics, no LLM, Expedition-class) onto the Pi so
it's legal in-race; (2) optionally add a **Jetson Orin Nano** local LLM (Qwen2.5-7B) for in-race chat
over the engine's facts; (3) keep cloud **frontier Opus 4.8** for *between-races* prep, debrief, and a
write-back **C4 Performance Lab**. The cloud stack above stays the practice/cruising/debrief product and
the C4 Performance Lab.

## Repo layout (monorepo)

```
pi/                 Signal K config, vcan/systemd units, uplink + full-res archiver
vps/ingestion/      FastAPI ingestion API (token-auth, writes batches to TimescaleDB)
vps/agent/          Claude tool-use service + alerting + summarizer + WebSocket chat
vps/web/            mobile web chat app (nginx static)
vps/db/             TimescaleDB schema + migrations + fake-data seed
shared/             data schemas, tool contracts, units
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
| web         | 80             | **8090**   |
| Signal K    | 3010 (host net)| **3010**   |

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

## Phased build (each phase has a clear exit test)

| Phase | Deliverable | Exit test |
|-------|-------------|-----------|
| **0** | Repo + dev compose + schema + stubs + fake data | `compose.dev.yml up`; DB reachable; fake data loads |
| 1 | Pi base + PICAN-M + vcan0 + Signal K | sample N2K flows; Signal K dashboard populated |
| **2** ✅ | Pi local archive | full-res capture verified on bench; survives reboot; backfill lands in cloud `telemetry_raw` |
| **3** ✅ | Ingestion + uplink store-and-forward | forced-outage test passed: batches queue to a named volume, survive a reboot mid-outage, drain with no loss |
| 4 | Agent core + SQL tools | accurate answers on conditions/perf/AIS vs live dev data |
| **5** ✅ | iPad nav companion: day/night, sail dial, course plot, navigator, tactics, routing | bench-verified end-to-end; server-side shared-pw auth still a stub |
| 6 | Alerting + summarizer + polar tooling | acceptable alert false-positive rate over 2 practice sails |
| 7 | Prod stack + deploy + rules review + soak | NOR compliance determined; 48-h unattended soak passes |

**Current status:** Phases 0–4 built and bench-verified. Phase 1 (Signal K + uplink) end-to-end;
Phase 2 (full-res onboard archive + backfill); Phase 3 uplink store-and-forward (disk-backed
queue on a named volume — forced-outage test passed, survives reboot mid-outage, drains with no
loss); Phase 4 agent runs the *real* Claude tool-use loop (`vps/agent/app/agent.py`, not stubbed)
with the boat-speed gospel + per-source skepticism + source priority/failover. True wind/VMG now
flow via the auto-enabled `signalk-derived-data` plugin. Phase 5 ✅ the iPad crew interface is
built (day/night, sail dial, course plot + navigator, tactics, weather/isochrone routing — see
"iPad crew interface"). Phase 6 IN PROGRESS — **6.0 live AIS** (see "Live AIS"), **6.1 alerting**
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
