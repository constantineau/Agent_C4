# Onboard Engine + C4 Performance Lab — Scoping

**Status:** design / scoping only (2026-06-17). No code written yet. Companion to
`docs/RRS41_COMPLIANCE.md` (the *why*); this is the *how*. Supersedes the old "all-onboard needs a
local LLM (big build, deferred)" framing — see RRS41 §4.

This is a **scope extension**, provisionally a **Phase 9 / Onboard + C4 Performance Lab track**. It does
not change Phases 0–7; it adds a compliant in-race execution path and a between-races learning loop.

---

## 1. The three-layer architecture

| Layer | Where | In-race | What |
|---|---|---|---|
| **A. Deterministic engine** | **Onboard (Pi 4)** | ✅ legal | `navigator`, `routing`, `tactics`, `sails`, `polar_tool`, `fatigue` — physics/geometry on the boat's own sensors + published course. No LLM. Expedition-class. |
| **B. Common-data fetch** | cloud or onboard | ✅ legal | GRIB / forecast / AIS / buoys — "information available to all boats" (verbatim, no per-boat processing). |
| **C. Conversational coaching** | onboard local LLM (in-race) / cloud Opus (otherwise) | onboard ✅ / cloud ❌ | Narrate the engine's facts; free-form crew Q&A. |
| **D. C4 Performance Lab** | **Cloud Opus 4.8 ashore** (studio) → **onboard executor** (Pi+Orin) | studio: pre-race ✅ / executor: in-race ✅ | Shore studio compiles a multi-variant **playbook** (routes + decision tree + rationale) + write-back learning (polars/crossovers/calibration/fatigue); onboard executor tracks live GRIB+buoys, picks/recomputes variants, and shows the tradeoffs (glass-box) on the iPad. |

Connective tissue = the **"homework" pattern**: Opus produces artifacts off-boat *before the start*,
they are **loaded onto the boat**, and the onboard system merely executes/recomputes them. The plan
**freezes at the gun**; nothing is re-derived from the cloud mid-race.

---

## 2. Layer B — relocate the deterministic engine to the Pi

The six modules already run on the VPS. The only real porting work is **data access** and
**packaging**; the algorithms are unchanged.

**Key technical wrinkle — the data source differs onboard:**
- *Cloud today:* the modules query TimescaleDB `telemetry_raw` (15-s aggregates uplinked from the
  boat).
- *Onboard:* the data is local — the Phase-2 full-res SQLite archive (`sk_archive` volume), and for
  live values the **Signal K WS directly** (full-res, lowest latency — better than the 15-s
  aggregates the cloud sees).

**9.0 — Data-access abstraction.** Put a small pluggable data layer behind the six modules:
`source = CloudTimescale | OnboardArchive(+SignalK live)`. Same module code, same outputs, different
backend. Also stage the knowledge files on the Pi (`sr33_speed_guide.md`, `polars_sr33.sql` → a local
polars store) and a slot for the **loaded race plan + refined polars**.
*Exit test:* on the bench, the engine produces outputs identical to the cloud path when fed the same
data from the local backend.

**9.1 — Onboard API + compose.** Package the engine as an onboard service in `compose.pi.yml`
(runs on the Pi alongside Signal K / uplink / archiver), exposing the same REST endpoints the iPad
already uses: `/navigator`, `/course`, `/route`, `/tactics`, `/sail`, `/polar-analysis`, `/fatigue`,
`/forecast`. No LLM, no tool-loop — direct deterministic responses.
*Exit test:* the iPad, pointed at the Pi, renders the same nav/sail/plot/tactics screens it gets from
the cloud.

**9.2 — iPad race-mode routing + cloud race gate.** In race mode the iPad talks **only to the Pi**
(channel separation at the config/network level, not a soft toggle); the cloud agent gets the
**server-side, fail-closed** RRS-41 gate (RRS41 §4A) so even if reached it refuses
tactics/routing/polar/sail/fatigue. Add the audit log (mode on/off, channel state, refusals).
*Exit test:* in race mode, no request reaches the cloud; the cloud agent refuses gated topics with the
RRS-41 message; the audit log shows it.

Latency note: onboard the engine reads full-res Signal K live data, so responses should be *faster*
than the cloud path (no uplink lag, no 15-s aggregation, no WAN round-trip).

---

## 3. Layer C — optional onboard conversational LLM (Jetson Orin Nano)

For in-race natural-language coaching. **Not required** for layers A/B; add only if narration over the
engine's facts is wanted on the water.

**Hardware:** Jetson Orin Nano **8GB, Super mode** (JetPack 6.2; ~67 TOPS, 102 GB/s, ~$249). Pi 4 stays
the sensor/engine box; the Orin is an inference companion.

**Confirmed benchmarks** (NVIDIA JetPack 6.2, INT4 / MLC, Super mode):

| Model | tok/s (Super) | Mem | Fit |
|---|---|---|---|
| **Qwen2.5-7B** | **21.8** | ~4.8 GB | ✅ primary pick — best capability + function-calling at 8GB |
| Llama-3.1-8B | 19.1 | ~4.8 GB | ✅ strong alternative |
| Phi-3.5 3.8B | 38.1 | ~2.3 GB | ✅ fast / headroom |
| Llama-3.2-3B | 43.1 | ~2 GB | ✅ fastest |
| Gemma-2-9B | 9.2 | ~5.5 GB | ⚠️ practical ceiling, slow |

Prefill ~285–300 tok/s; ~14.8 W at 25W mode for a 7B. A ~100-token tactical answer ≈ 5 s; longer
narration ≈ 15 s — usable on a boat.

**Practical caveats (must verify on the unit):**
1. **Use NVIDIA's MLC / TensorRT-LLM runtime (`jetson-containers`), not bare llama.cpp/Ollama, for
   7–8B** — a known CUDA memory-allocator regression in JetPack R36.4.7 broke >1B models under
   llama.cpp; NVIDIA's 7–8B numbers come from MLC. Pin a known-good JetPack.
2. **INT4 required** for 7–8B on 8GB — minor quality loss; fine for narration (the engine does the
   reasoning).
3. **Thermal** — Super numbers assume cooling; a hot enclosed nav box will throttle without an active
   heatsink/fan. Budget cooling.

**Role: a bounded decision-support copilot, not just a narrator.** Nothing in RRS 41 limits the Orin
to narration — it's the boat's own computer, so it may *reason and help decide* in-race (the
narrate-only guidance is a **reliability** guardrail for a 7B, not a legal one). Division of cognitive
labor: **Opus** builds the strategy space pre-race (unbounded); the **deterministic engine** owns the
numbers in-race (exact); the **Orin** is the in-race *judgment* layer — it interprets the engine's
numbers + live obs against the pre-loaded playbook, handles the gray/conflicting cases crisp rules
miss, and recommends with confidence + caveats.

What it does for decisions: match live obs to each scenario's signature; flag the borderline/conflicting
case ("rule says east, but obs are on the line and the up-course buoy says the shift is early"); answer
follow-ups from the pre-loaded rationale; produce a single-shot structured brief at a decision gate
(scenario-tracking + confidence → recommended variant + why → caveats/what-flips-it → conflict flag).

**Guardrails (keep a 7B trustworthy):** the LLM **never does the math** (routing/ETA/CPA/laylines stay
in the engine — it *consumes* those numbers); **never invents strategy outside the playbook** (decision
space bounded to Opus's pre-authored variants + engine outputs — selects/interprets/flags, doesn't
freelance); **never the sole authority** (surfaces to the crew with the deterministic recommendation
alongside, so divergence is visible — crew decides). Inputs are pre-digested + the option space is
bounded → reliable for a 7B. Short/optional tool loops only.

**9.4 — Local LLM copilot.** Stand up the Orin with Qwen2.5-7B (A/B vs Qwen3-4B for speed), fed the
engine's facts + the playbook + live obs. Start with narration (single-shot), then add the bounded
decision-support brief (Lab-3).
*Exit test:* (narration) an onboard NL answer grounded in the engine's facts at usable latency, offline
from any cloud; (decision support) at a decision gate, a correct scenario-match + variant recommendation
+ conflict flag vs a replayed obs stream, alongside the deterministic recommendation.

---

## 4. Layer D — the C4 Performance Lab (cloud Opus 4.8 ashore + onboard executor)

The C4 Performance Lab is a **frontier-model race-strategy studio** that, *before the start*, compiles
an **onboard playbook**; the boat then *executes* that playbook in-race using onboard computation on
its own data + genuinely-common public data. All Opus/cloud work is pre-start (unrestricted); all
in-race computation is onboard (legal). See RRS41 §6.

### 4.1 Between-races learning loop (makes everything below more accurate)
- **Hoisted-sail logging (prerequisite).** Today the hoisted sail is only in browser `localStorage`
  (`sr33.hoisted`), passed transiently to `/sail` — **not persisted**. Add a timestamped log so
  crossover-learning has labels. (Polar + calibration learning work without it.)
- **Polar refinement.** Extend `polar_tool.py` (today read-only observed-vs-ORC p90) to aggregate
  across many sails and **write back** refined polars (`target_stw`/`target_vmg`) — *review-before-replace*.
- **Crossover refinement** (needs the sail log), **calibration learning** (cross-source offset/drift),
  **fatigue tuning** (`FATIGUE_*` vs labeled archives).

### 4.2 Route-strategy studio (shore, Opus 4.8, pre-race)
1. **Multi-scenario optimization.** Ingest *several* weather outcomes — multi-model (GFS/ECMWF/HRRR)
   and/or ensemble members — and isochrone-optimize each over the real course on the refined polars →
   an optimal route per scenario with leverage/decision points marked.
2. **Frontier-model strategic synthesis** (where Opus earns its keep):
   - **Robust strategy** across scenarios — the regret-minimizing play when models disagree, not just
     the single-GRIB optimum.
   - **Qualitative reasoning the optimizer misses** — Cove Island gate, shipping lanes / traffic
     separation, Lake Huron/Michigan thermal & shoreline effects, current/tide gates, day↔night
     transitions, the cost of a sail change / extra tack for this crew.
   - **Executable decision tree** — compiled into deterministic, onboard-runnable rules (so the boat
     needs no LLM to choose), each carrying a plain-language **rationale + tradeoffs**.
3. **Playbook artifact.** N pre-optimized variants + the decision tree + per-leg sail plan + decision
   gates (with trigger conditions) + the rationale/tradeoffs text, packaged into one portable file.
   Loaded onboard, **frozen at the gun**.

### 4.3 Onboard playbook executor (Pi engine + Orin, in-race — all legal)
- **Public-data monitor.** Onboard fetch of *common* public data in-race: GRIB updates +
  **NOAA NDBC / Great Lakes (GLOS) buoy obs** + NWS + CO-OPS water level — all "available to all
  boats". Treated skeptically (staleness/outage) like any source.
- **Scenario tracker.** Scores live signals — latest GRIB, **buoy obs (ground truth, often up-course of
  the boat = a leading indicator)**, the boat's own masthead wind — against each pre-loaded scenario →
  "reality is tracking scenario B".
- **Variant selector + onboard re-optimizer.** Deterministically (a) pick the live-best variant by the
  pre-loaded rules, and/or (b) **re-run the isochrone optimizer onboard** on the latest public GRIB
  from the current position (the Expedition core, relocated into the engine).
- **Decision-gate alerts.** "Gate at WP3 in ~18 min: buoy+GRIB favor the west variant." The Orin's
  local LLM narrates from the engine's facts + the pre-loaded rationale.

### 4.4 Glass-box, not a black box (design principle)
The crew must see *why*, with the tradeoffs — and this is compliance-clean because the **rationale is
pre-authored by Opus before the start** and merely **surfaced + narrated onboard** in-race (no fresh
cloud call). Two layers:
- **Pre-race (rich):** Opus writes, into the playbook, each variant's rationale, the tradeoffs (ETA vs
  distance vs risk vs pressure vs gate position), and what would flip each decision.
- **In-race (onboard, iPad Strategy card):** the **currently-favored variant + one-line why**, a
  **compare view** (variants side-by-side with tradeoffs), the **decision-gate countdown + what flips
  it**, a **live-signal panel** (GRIB vs buoys vs own wind: agreement/divergence + provenance), and a
  **confidence** read (how much scenarios diverge / obs match forecast). The Orin answers "why west?"
  from the pre-loaded rationale + live facts. The crew stays in command and can override.

This extends the project's existing self-explaining / skeptical-source ethos to strategy, and it
strengthens RRS-41 defensibility (the boat presents its own pre-made plan + public data; the crew decides).

### 4.5 New dependencies
- **Real GRIB ingestion** (multi-model/ensemble) — today only Open-Meteo *point* forecast (old Phase
  8.1, now central).
- **Buoy/obs ingestion** — NDBC / GLOS / CO-OPS onboard fetch + store (new; small public feeds).
- **Onboard routing** — relocate `routing.py` into the engine + onboard GRIB fetch/store (rides with
  9.0/9.1).
- **Real course/marks** — old Phase 8.0; the Mackinac course is known and loadable.
- **Playbook artifact format + onboard scenario-tracker/selector** — new components.

### 4.6 Phasing (Lab-1 → Lab-4)
- **Lab-1** — GRIB + buoy ingestion + single-scenario isochrone routing on refined polars (cloud) →
  one optimal route + briefing.
- **Lab-2** — multi-scenario/ensemble optimization + Opus robust-strategy synthesis + the **playbook
  artifact** (N variants + decision tree + rationale/tradeoffs).
- **Lab-3** — the **onboard executor**: relocate routing to the engine, onboard GRIB+buoy fetch,
  scenario tracker + variant selector + decision-gate alerts (Orin narration) + the iPad Strategy card.
- **Lab-4** — post-race validation (which variant/obs paid → feeds 4.1 learning).

*Exit test:* a real/replayed passage → Opus produces a multi-variant playbook; onboard, the executor
tracks scenarios from live GRIB+buoys, surfaces the favored variant with its rationale/tradeoffs on the
iPad, and recomputes the route onboard — all offline from the cloud.

**Bright line:** every frontier-model/cloud step is pre-start; in-race the optimizer, selector, and
narration run **onboard** on own-data + common public data. The cloud is **never** consulted mid-race
for a fresh customized route. Pre-loaded variants + onboard re-optimization from public GRIB/buoys =
the legal mechanism.

---

## 4.7 Design decisions locked 2026-06-17 (with the user)

These sharpen §4 and govern the Lab build. All respect the §7 bright line: frontier/cloud work is
pre-start and **frozen at the gun**; in-race is onboard compute on own-data + common public data; the
learning loop is between-races.

1. **GRIB — full multi-model set from the start.** Free stack: GFS + GEFS (NOMADS); ECMWF IFS HRES +
   ENS (open data); ICON (DWD); high-res regional over Lake Huron/Michigan: HRRR + NAM; Great Lakes
   physics: NOAA GLOFS (currents/level/temp) + NDBC/GLOS buoys. The optimizer runs per-model and
   per-ensemble-member → a cloud of optimal routes. Build = a `WindField(lat,lon,t)` abstraction +
   multi-scenario orchestration; the isochrone core already exists in `vps/agent/app/routing.py`. Add
   current (GLOFS) and maneuver/sail-change cost (refined polars + the hoisted-sail log).

2. **Playbook = a single signed bundle with a BRANCHING decision tree, generated as close to race
   time as possible.** The bundle is a *tree*: each variant is a segment ending in a branch node
   evaluated continuously onboard on two monitors — **route-deviation** (XTE / VMC-deficit /
   time-behind-optimal) and **forecast-drift** (latest public GRIB vs the GRIB the variant was built
   on). Branch children are **pre-authored ashore** (Opus runs the optimizer from plausible off-track
   positions and drifted forecasts), so onboard you *select* a frozen branch (with rationale), never
   generate one from the cloud. Graceful degradation: on-script → pre-authored branch → onboard
   re-optimize within the nearest variant (own polars + public GRIB, legal) → fully off-script onboard
   optimal (legal, LLM flags it). Engineering constraint: "as close to race time as possible" + full
   multi-model×ensemble + the branch children is heavy → cluster early, cap members, parallelize,
   pre-stage everything not dependent on the morning GRIB.

3. **Learning loop — start with prompt/policy + case-memory adaptation; LoRA deferred (Orin-gated).**
   The "onboard brain" is the bundle's `onboard_brain` block (system prompt + guardrails + few-shot
   exemplars + scenario matchers); the Lab edits that text between races. Judge mechanism: post-race
   the optimizer is an **oracle** (knows what the wind actually did from the boat archive + analysis
   GRIB) → computes the hindsight-optimal route and the **regret (min/places) at each decision gate**
   vs what the onboard brain recommended → Opus critiques → edits the onboard brain → next race's
   playbook carries the improvement. Judge **process/EV, not just outcome**: separate "good +EV call,
   bad luck" from "bad call", reward correct decisions under uncertainty even when they didn't pay,
   flag lucky bad calls (regret distributions + counterfactuals, so variance doesn't corrupt the
   signal). Delivers value with no Orin (v1 onboard brain = the deterministic decision tree + the iPad
   Strategy card). Also: the onboard LLM must **detect divergence** between the loaded playbook and how
   the race is actually unfolding and make suggestions **with reasons** — compliant because the reasons
   come from the frozen playbook rationale + own instruments + common public data (interpretation of
   pre-loaded homework, not fresh outside advice). Guardrail: every suggestion grounds in (a) a
   pre-authored variant/branch, (b) the onboard deterministic re-optimizer, or (c) common public data —
   it selects/interprets/flags, never originates novel strategy.

4. **FUZZY adherence — the unifying principle.** The boat never sails the line; the models are never
   exactly right; the helm varies by skill and fatigue; conditions differ from the forecast. So
   following the playbook and making calls is fuzzy, not black-and-white: (a) branch triggers are
   **soft with hysteresis** (a "consider" band + a "commit" band → no chatter near a threshold); (b)
   branch on **expected value, not deviation** — only when expected gain > maneuver cost + a margin
   that **scales with uncertainty** (models disagree → hold the robust plan); (c) **confidence** is a
   first-class output on every call (scenario/model spread + how cleanly obs match one scenario + data
   staleness), shown on the iPad and voiced by the LLM; (d) **deviation/adherence is a tracked iPad
   metric** (time-behind-optimal + % of *achievable* polar + XTE), **attributed** to helm vs conditions
   vs tactical choice and surfaced as gentle coaching, not blame; (e) baselines are **helm-aware and
   achievable, not theoretical** — reuse the fatigue-index DNA (anonymous current-helm, baselined vs
   the boat's own recent performance, maneuver-excluded); optimize/score against realized polars (ORC
   degraded by sea state + a helm-skill factor), keeping the gap to ORC theoretical as a separate
   coaching number; (f) the scenario tracker outputs a **distribution** over scenarios, not a hard pick.
   This is the project's existing DNA (source skepticism, the fatigue index) extended from sensors to
   strategy: the playbook is a prior, the boat's reality is the evidence, the agent reasons over the
   gap with humility. The agent's posture is **advisory under uncertainty, never imperative.**

5. **Fleet / competitor intelligence — onboard, handicap-aware.** The onboard LLM assesses our position
   relative to others in class/race and gives tactical input (who's in better pressure, positioning vs
   a front, cover/split). Compliance-clean: AIS arrives via the boat's own em-trak B951 receiver (own
   sensor), reasoned over by the boat's own computer; the fleet roster + handicaps are pre-loaded
   public homework; weather is common data — no mid-race cloud call. Foundation exists (Phase 6.0 AIS →
   `ais_targets`, `ais.py` CPA/TCPA). Extend from collision-only: classify AIS targets as **fleet**
   (matched to roster) vs **traffic** (keep the always-allowed collision guard); for fleet targets the
   engine computes tactical geometry (relative VMC/gain-loss, leverage/XTE split, up-course pressure
   proxy) and the **corrected-time delta** — the deep value, since handicap racing is a corrected-time
   problem ("behind on the water but ahead on corrected → sail your own race" vs "that's the real
   rival, to leeward in more pressure"). The relevant optimizer objective is **minimize corrected time
   vs class**, not absolute elapsed (ORC scoring flavor per the SI). The fleet is also a distributed
   sensor (a competitor up-course at higher SOG = more pressure there = a leading indicator for the
   scenario tracker). Coverage is partial (not all boats run AIS; Class B is laggy; MMSI↔boat matching
   is a gap) → fuzzy soft signals with confidence, flagged honestly.

6. **Fleet ingestion + race ingestion (a new Lab-0).** Teams upload a race (NOR + SI URLs/PDFs); Opus
   extracts a structured **RaceDefinition**: course/marks (WGS84), gates (e.g. Cove Island, rounding
   direction), start/finish, schedule, class splits, scoring, a **comprehensive requirements
   checklist** (safety/SER equipment + registration + procedural items — each tagged with the
   phase/trigger it applies at, and the race-time ones pushed to the iPad, e.g. nav lights at sunset,
   the gate GPS photo, the finish procedure + displaying numbers), **rule modifications** (RRS-41 is
   just one of these — comprehensive checking is the point), exclusion zones (shipping lanes/TSS,
   shoals, islands), and a **fleet** block (boat name, class, ORC rating/GPH, MMSI where available)
   assembled from public ORC data + the race entry/sign-up list. Mandatory
   **human-review** step on extracted geometry (a wrong waypoint is dangerous; coordinate formats
   vary). Key unification: the **same ingestion feeds both the optimizer** (geometry + zones the route
   must respect + the fleet) **and the RRS-41 race gate** (the per-race `rules_profile` — e.g. the
   Bayview Mackinac NOR §2.1(d) change to Rule 41). Different races → different gate config
   automatically; this automates/generalizes the hand-written `docs/RRS41_COMPLIANCE.md`. Target the
   **2026 Bayview Mackinac** first; generalize to any race (e.g. **Mills Trophy 2026**). **Dual input:**
   the setup lets users either (a) **auto-find/fetch** the docs from a race URL (works for static-doc
   sites like bycmack.com) or (b) **paste a direct link / upload the PDF** — many race hubs are
   JS-rendered (e.g. Mills on YachtScoring) where an auto-crawler can't reach the PDFs, so paste/upload
   is the required fallback. The human-review step on extracted geometry/rules applies either way.

7. **Public race tracker as a common-data source (Bayview Mackinac).** The official public tracker
   (bycmack.com/tracking — a separate YB/TracTrac-style system, public with a deliberate delay) is, by
   NOR §2.1(d) "available to all boats", a common-data source like GRIB/buoys. **For Bayview Mackinac
   the user confirms tracker access is allowed and normal during the race** — caveat resolved for this
   race. Remaining points are engineering, not compliance: the ~15-min+ delay neuters live tactics, so
   it's the **over-the-horizon fleet picture** (AIS = nearby/real-time/own-sensor; tracker = the whole
   fleet incl. unseen boats, delayed) — and it often supplies boat identity, helping the AIS
   MMSI-match gap. Architecturally identical to GRIB/buoys: onboard fetch of a common source, fed to
   the fleet layer, matched to the roster, **every position explicitly aged + confidence-reduced**
   (never shown as current), reasoning stays onboard. Generalization: for *other* races the Lab-0
   `rules_profile` must record per-race whether the official tracker is permitted (SI check), default
   conservative if unclear.

## 5. Phased plan (proposed)

| Step | Deliverable | Exit test | New HW |
|---|---|---|---|
| 9.0 ✅ | Data-access abstraction (`datasource.py`: CloudSource now; OnboardSource = 9.1) for the engine modules | **done** — cloud path byte-identical via `datasource.active()`; engine endpoints bench-verified | none |
| 9.1 ✅ | Onboard engine service + API in `compose.pi.yml` (+ `OnboardSource`: SQLite archive + Signal K live) | **done** — `pi/engine/` serves all engine endpoints onboard via `OnboardSource`; bench-verified (live cache + archive-history paths). iPad→Pi pairs with 9.2 | none |
| 9.2 ✅ | iPad race-mode → Pi only; server-side fail-closed cloud gate + audit log | **done** — cloud-gate half (race mode 403s advice + chat refuses + audit_log) AND iPad-side: the `pi/console` race console serves the app pointed only at the onboard engine (:8091→:8200), onboard mode (no auth/chat/cloud, all panels available); bench-verified (no /auth, no /ws, zero cloud calls) | none |
| 9.3 | C4 Performance Lab — learning loop: hoisted-sail logging, polar write-back, prep/debrief | a sail → refined polars loaded back onboard | none |
| 9.4 *(opt)* | Orin Nano local LLM narrator | grounded onboard NL answer, offline, usable latency | Orin Nano 8GB |
| Lab-0 | **Race ingestion** (input: auto-find from a race URL **or** a pasted link / uploaded PDF): NOR/SI → RaceDefinition (course/marks/gates/zones/scoring + `rules_profile`) + the `fleet` block (ORC + entry list) + human review | Bayview Mackinac course + zones + rules profile + fleet load cleanly from the NOR; feeds optimizer *and* the race gate; generalizes (Mills Trophy via paste-link) | none |
| Lab-1 | GRIB + buoy ingestion (full multi-model `WindField`) + single-scenario isochrone routing on refined polars (cloud) | one optimal route + briefing from real GRIB on a real RaceDefinition | none |
| Lab-2 | Multi-scenario/ensemble optimization + cluster + Opus robust synthesis + the **branching playbook bundle** (variants + branch tree on deviation+drift, pre-authored children, rationale) | a multi-variant branching playbook (routes + branch tree + rationale/tradeoffs) | none |
| Lab-3 | Onboard executor: relocate routing to engine, onboard GRIB+buoy(+tracker) fetch, **fuzzy** branch evaluator (soft triggers/hysteresis/EV) + selector + graceful degradation, fleet/corrected-time intelligence, iPad Strategy card (confidence + adherence gauge), LLM divergence-detection + reasoned suggestions | onboard tracks scenarios from live GRIB+buoys+fleet, surfaces favored variant + tradeoffs + confidence, recomputes route — offline from cloud | LLM layer uses Orin |
| Lab-4 | Post-race **judge loop**: optimizer-as-oracle regret (process/EV, not outcome) → Opus critique → edit the onboard brain (prompt/policy/case-memory) | debrief attributes regret to decisions + updates the onboard brain | none |

9.0 → 9.2 is the compliance-critical path (legal in-race, no hardware). 9.3 is the learning loop. 9.4
is the optional NL polish that needs the Orin. **Lab-1 → Lab-4 build the C4 Performance Lab studio +
onboard playbook executor** (§4); Lab-3 depends on the onboard engine (9.0/9.1) and the Orin (9.4).

---

## 6. Open decisions / inputs needed

- **Onboard live-data source:** ✅ RESOLVED (9.1) — SK-live WS cache for current values + the SQLite
  archive for history, in `OnboardSource`.
- **Race-mode channel separation mechanism:** ✅ DECIDED — network-level (iPad on a boat-local SSID, no
  WAN), the stronger compliance posture; the iPad-side build is 9.2.
- **Orin Nano:** ✅ IN HAND (2026-06-18) — 9.4 runtime/model bring-up authored (`pi/orin/`: flash →
  Super mode → MLC + Qwen2.5-7B INT4 → OpenAI-compatible server + tok/s A/B + API smoke test +
  systemd autostart); to be run on the fresh unit. The SR33 copilot service (engine facts + playbook
  → bounded decision support) is the next 9.4 increment. Layers A/B + the deterministic playbook
  still deliver most of the value with no LLM.
- **Engine on Pi 4 vs Orin:** ✅ DECIDED — engine is light/deterministic → Pi 4; the Orin, when added,
  is dedicated to the LLM.
- **GRIB scope:** ✅ DECIDED (§4.7-1) — full multi-model set; start the *ingestion* with the
  no-API-key NOAA stack (GFS/GEFS + HRRR + NAM), layer ECMWF open-data in next.
- **Playbook model:** ✅ DECIDED (§4.7-2) — single signed bundle with a branching decision tree.
- **Learning loop v1:** ✅ DECIDED (§4.7-3) — prompt/policy + case-memory adaptation; LoRA deferred.
- **First race target:** ✅ DECIDED (§4.7-6) — 2026 Bayview Mackinac, generalizing via Lab-0 to any
  race (Mills Trophy 2026 as the generalization test).
- **Public tracker:** ✅ for Bayview Mackinac (allowed/normal per the user); per-race SI check elsewhere.
- **Still needed from the user (when we reach the Lab track):** the 2026 Bayview Mackinac + Mills
  Trophy NOR/SI source docs (or links) for Lab-0.

---

## 7. Compliance summary (cross-ref RRS41_COMPLIANCE.md)

| Capability | Onboard? | In-race legal? |
|---|---|---|
| Deterministic engine on Pi (own sensors + published course) | yes | ✅ (Expedition-class) |
| Common data (GRIB/forecast) fetched verbatim | either | ✅ (available to all) |
| Local-LLM narration over engine facts | yes | ✅ |
| Cloud-LLM customized tactical/routing/coaching | no | ❌ (outside source) |
| Refined polars/crossovers computed off-boat, loaded *pre-start* | n/a | ✅ (own data, frozen) |
| Refined plan re-derived from cloud *mid-race* | n/a | ❌ |

*Confirm with the OA/RC in writing before race use; re-check the Sailing Instructions (~July 2026).*
