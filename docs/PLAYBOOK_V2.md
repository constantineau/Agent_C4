# Playbook v2 — the scenario-rich PLAY LIBRARY

**Status:** design LOCKED with the user 2026-07-06. Supersedes the Lab-2 "per-model side variants"
generation model (which remains as one scenario source inside this design) and the in-race LLM
**origination** posture of `docs/STRATEGY_SYNTHESIS.md` (see §7 Descope).

## 1. Why

Today's playbook (`vps/lab/app/playbook.py`) has exactly one variation axis: split the blended wind
field into per-model sub-fields, route each, cluster by **first-beat side**. Two failures:

1. **When the models agree we get one variant and no plays at all.** Model disagreement is a poor
   proxy for "how might this race depart from the nominal plan" — it is only one of many sources of
   variation, and often the smallest.
2. **Every play is a wind-side play.** Nothing in the bundle answers the situations the onboard
   triggers already *detect*: we're slower than the plan, we're off the optimizer's line, we're
   overpowered on the wrong sail, the front is late. The selector can only say HOLD / SWITCH-side /
   OFF-SCRIPT — there is no pre-authored *response* to point to.

Variation from the nominal plan has both **external** causes (the wind is different from the
forecast, as reality plays out) and **internal** causes (the crew flies the J2 longer than they
should and gets overpowered; the boat sails off the optimizer course; pace falls behind plan).
The playbook must carry plays for both.

## 2. The artifact — a PLAY LIBRARY

A **play** = a named scenario + machine-checkable detection conditions + a pre-computed response +
frontier-authored rationale/tradeoffs. The bundle is the library of plays around one **nominal**
route (the recommended plan, which stays what it is today).

```jsonc
// c4.playbook/v2 — superset of v1; v1 consumers keep working (see §5 compat)
{
  "schema": "c4.playbook/v2",
  "race_id": "...", "course_id": "...", "start_epoch": 0,
  "nominal": {                          // the recommended plan (v1 `recommended` variant, promoted)
    "route": {...}, "sail_plan": [...],
    "robustness": [                     // scenarios under which the nominal HELD (evidence, not plays)
      {"scenario": "wind_rot_-10", "note": "route unchanged within tolerance, +22 min"}
    ]
  },
  "plays": [
    {
      "id": "shift_right_20",           // stable slug
      "name": "Persistent right shift", // crew-facing
      "category": "external",           // external | internal
      "scenario": {                     // generation provenance
        "kind": "field_rotation", "params": {"deg": 20},
        "source": "synthetic"           // synthetic | model:<id> | ensemble:<cluster> | timing | pace | sail_loss | ...
      },
      "conditions": {
        // BOTH forms, by design: predicates are evaluated deterministically by the Tier-1 engine
        // matcher; the narrative is what the Tier-2 onboard LLM pattern-matches against the full
        // grounded picture (compound/fuzzy situations predicates can't express).
        "predicates": [                 // ALL must hold to ARM the play (Schmitt-style sustain)
          {"signal": "drift_twd_signed_deg", "op": ">=", "value": 15, "sustain_min": 20},
          {"signal": "shift_persistent",     "op": "==", "value": true}
        ],
        "narrative": "The breeze has gone right of the frozen forecast and held — not an oscillation. Deviation/fleet reads to the right reinforce; a left-leaning fleet does not negate it."
      },
      "applicability": {"legs": [1, 2], "phase": "any"},   // when the play can be in effect
      "response": {
        "type": "route",                // route | guidance
        "route": {...},                 // pre-optimized track (route plays)
        "guidance": null,               // guidance plays: a pre-authored call, no new track
        "sail_plan": [...]
      },
      "summary": "...", "rationale": "...", "tradeoffs": "...",
      "what_flips_it": "...",           // the exit/hand-back condition, same spirit as v1
      "stakes_min": 40                  // time cost of ignoring the play if its scenario is real
    }
  ],
  "variants": [...],                    // v1 first-beat side plays, unchanged shape (compat, §5)
  "decision_tree": [...],               // now condition→play mapping across the library
  "boat_model": {...}, "obstacles": {...}, "forecast_fingerprint": {...},   // unchanged
  "signature": {...}
}
```

**Guidance plays** are first-class: not every scenario needs a new track. "Change down to the J3 —
expected ~0.4 kn VMC recovery per the frozen crossover table" is a play whose response is a call,
grounded in the boat model, with no route attached.

## 3. Scenario sources (the generation levers)

All computable with existing machinery (the GRIB is already downloaded; scenarios are mostly CPU
re-routes through transformed fields or altered optimizer parameters).

### External — the environment departs from the forecast

| Source | Transform | Guarantees plays when models agree? |
|---|---|---|
| **Synthetic field perturbation** | rotate blended field ±10°, ±20°; scale TWS ×0.75, ×1.25 | **YES — the core new lever.** Classic router sensitivity analysis. |
| **Timing shift** | advance/delay the whole field ±3 h, ±6 h ("front early / front late") | YES |
| **Per-model sub-fields** | today's Lab-2a fan-out, unchanged | no (that's the flaw) |
| **Ensemble members** | GEFS / ECMWF-ENS members (already wired, opt-in), clustered into scenario families | mostly (physical spread survives deterministic agreement) |
| **Sea state heavier** | wave factor scaled up (GLWU + margin) | yes |
| *(later)* current anomaly | scale/rotate the current field | low priority on the lakes |

### Internal — the boat/crew departs from the plan

| Play | Pre-computation | Detection signal (all exist today) |
|---|---|---|
| **Pace behind/ahead** | re-route from each major waypoint at planned-ETA +2 h / +4 h / −2 h — being late means meeting DIFFERENT weather downstream; the optimum from there can flip | `deviation.time_behind_s` |
| **Off the line (rejoin-vs-continue)** | tabulate rejoin-vs-continue from representative off-track positions per leg | `deviation.xte_nm` + side |
| **Wrong sail / overpowered** | guidance play from the frozen crossover table (change-down call + expected recovery) | `sails` hoisted-vs-optimal + TWS margin + heel |
| **Gear failure (sail loss)** | re-run the 2g sail-aware optimizer with a critical sail REMOVED from the inventory (no A2; no S2) | crew reports via hoisted selector; play is armed manually or by prolonged sail-domain mismatch |
| **Conserve the crew (night/shorthanded)** | re-route with tack/peel costs ×3–5 → low-maneuver variant | `fatigue` index + night hours |

### Fan depth — how many scenarios, and why (2026-07-08)

The play count is EMERGENT, never a target: plays = the scenarios whose re-route genuinely
diverges from the nominal + the internal plays this race actually has. What IS chosen is the fan:
each scenario is a full isochrone re-route, so depth trades synthesis wall-clock for
decision-space coverage. Three tiers (Gameplan "Fan depth"):
- **quick** (6, ~5 min) — race-morning refresh close to the gun;
- **standard** (9, ~7 min) — the always-informative core grid (±10/20°, ×0.75/1.25, ±3 h, sea);
- **deep** (15, ~15 min) — the wide grid (+±30°, ×0.6/×1.4, ±6 h) for early-week homework.
The dedupe keeps the LIBRARY honest at any depth, and the priority order stays point-of-sail
aware (input #6). The real "more scenarios" lever beyond the grid is GEFS/ECMWF-ENS ensemble
members (physical spread, opt-in). **Boundary bisection (shipped 2026-07-08):** when
adjacent grid scenarios straddle the decision boundary (+10° holds, +20° diverges), the fan probes
the midpoint to LOCATE the flip and the located threshold becomes that play's arming predicate
(`boundary` block on the play; probe routes are never plays). One probe per straddled axis side,
largest-stakes first, capped `PB_BISECT_MAX_PROBES`=4; min gaps 6°/0.1×/2 h; kill-switch
`PB_BISECT`. The UI default fan depth is DEEP (user preference — a bigger library is welcome; the
dedupe keeps it honest).

### The dedupe discipline — plays only where the answer changes

A scenario whose route sticks to the nominal within tolerance is **NOT a play** — it is recorded as
robustness evidence on the nominal ("nominal holds under ±10° and −20% breeze"). This keeps the
library small and honest. It matters doubly because the onboard 7B's context is the constraint:
each play carries a compact digest; full play text is fetched per-play through a copilot tool.

## 4. Generation pipeline (Lab changes)

- **`vps/lab/app/scenarios.py`** (new): the scenario REGISTRY. Each entry = a field/optimizer
  transform + metadata + a detection-condition template. Fan-out = models ∪ ensemble clusters ∪
  perturbations ∪ timing ∪ internal scenarios, run under the existing time budget with priority
  ordering (perturbations + timing first — they're the cheap, always-informative ones).
- **`playbook.py`**: `build_playbook` consumes the registry; keeps the v1 side-clustering as the
  producer of the compat `variants[]`; adds route-vs-nominal divergence scoring for the dedupe.
- **`synthesis.py`**: writes the plays — crew-facing narrative + BOTH condition forms + tradeoffs +
  `what_flips_it` + the library-wide decision tree. **Model chain: Fable primary
  (`claude-fable-5`), Opus fallback (`claude-opus-4-8`)** — env `ANTHROPIC_MODEL_CHAIN`, tried in
  order; the deterministic no-LLM fallback bundle remains below both. Signing/freezing unchanged.

## 5. v1 compatibility

`variants[]`, `recommended`, `what_flips_it`, `boat_model`, `obstacles`, `forecast_fingerprint`
keep their v1 shapes, so `selector.py` / `adherence.py` / `deviation.py` / the dashboard tiles work
unchanged against a v2 bundle. v2 consumers read `plays[]`/`nominal`. The copilot's
`playbook.Playbook` gains play accessors + a per-play digest.

## 6. Onboard matching — the two-tier split (the LLM's job)

**The onboard LLM does pattern/condition matching — it does NOT originate tactics** (locked
2026-07-06; see §7).

- **Tier-1 (Pi engine, deterministic): `matcher.py`** beside the selector. Evaluates each play's
  structured predicates against the live signals (drift / deviation / tactics / sails / fatigue /
  fleet — all existing) with the same Schmitt-style sustain discipline the triggers use. Plays with
  all predicates true are **ARMED**. Served on the engine (e.g. `GET /plays`), part of the
  `/strategy` digest. Works with no Orin aboard.
- **Tier-2 (Orin LLM): the pattern matcher.** Reads the armed plays PLUS every play's condition
  narrative against the full grounded picture, catching the compound/fuzzy matches predicates can't
  express ("pace is off AND the drift says the breeze you were promised isn't coming — that's the
  pace play, not a trim problem"). It ranks matches, and explains each match **in the play's own
  frozen language**. Grounding: it may cite only `play:<id>` entries from the library + engine
  facts (the `brief.validate()` allow-list extends to play ids); an uncited or unknown play is
  dropped. The **recommendation stays deterministic** (selector/matcher output); the LLM
  contributes the assessment narrative + the ranked play matches.
- **Surface:** the iPad Strategy card gains an "armed plays" section under the synthesis apex; the
  auto-coach volunteers a callout when a play newly arms (same raise-slow/clear-fast discipline).

## 7. DESCOPE (locked 2026-07-06) — what this replaces

- **In-race LLM strategy origination is OUT OF SCOPE.** The 2026-07-03 posture ("the copilot MAY
  originate strategy; `vs_playbook: departs`") is withdrawn as a **product/reliability choice** —
  the pre-race frontier model + full GRIB is a far stronger tactician than the onboard 7B, and the
  deterministic OFF-SCRIPT + onboard re-route already covers "the situation outran the library"
  honestly. The RRS-41 *legal* reasoning (on-boat vs off-boat is the only line) still stands in
  `docs/RRS41_COMPLIANCE.md`; we are simply choosing not to use the latitude.
  - The LLM never authors or replaces a recommendation; it phrases the engine's digest and matches
    conditions to plays.
  - Tier-2 (LLM-originated) off-book re-route chaining is removed. **Tier-1 deterministic
    chaining stays**: a deterministic `off_script` verdict still attaches the compact
    `/reoptimize` offer — that's the engine, not LLM tactics.
- **The strategy-LoRA judgment/DPO training system is REMOVED** (`pi/orin/training/`, the
  `c4-labeling.service` ranker at lab.racertracer.net/training/, `docs/STRATEGY_LORA_PLAN.md`).
  Its premise — expert sailors ranking which *call* the 7B should make — is void when the 7B no
  longer makes calls. A future fine-tune pass would target **match quality + calibration**
  (does the model arm the right plays, with honest confidence) and can reuse the reliability-SFT
  plan (`docs/ORIN_LORA_PLAN.md`, unbuilt) plus a fresh labeling design; nothing built now.

## 8. Phase B design inputs — LOCKED 2026-07-06, outputs of the fleet retro study

`docs/RETRO_STUDY.md` §6 (66 boats, bayviewmack2025) converts several Phase B/D decisions from
judgment calls into measured quantities. These are **locked as implementation requirements**:

1. **Play mix is conditional on point of sail.** The generator computes the NOMINAL route's
   point-of-sail profile and weights play generation by it: a running race leans guidance-heavy
   (sail choice / target-speed / pace plays — execution beat geometry, polar% ρ −0.41 in every
   division while XTE pooled ≈ 0), a beat-heavy race leans geometry-heavy (side variants, shift
   plays). No fixed play mix.
2. **The bundle states the geometry-vs-execution verdict.** Synthesis computes a corridor-width /
   decision-stakes number from the scenario fan (lateral spread vs time stakes) and puts the
   verdict in the headline: "the lateral decision is worth ~N min — prioritize execution" (or the
   reverse when the fan splits). The 2025 fan was a wide corridor; crews should be told.
3. **Predicate thresholds are percentile-framed, not absolute.** Fleet-median XTE was 3.5 nm and
   median pace deficit 157 min on a 40 h race — that is NORMAL sailing, not an alarm. Play
   predicates key off the fleet distribution (consider ≈ median, commit ≈ p90: XTE 3.5/6.0 nm,
   pace normalized to % of elapsed with 384 min as the p90 anchor), and the bundle FREEZES the
   venue's fleet-normal stats so the onboard matcher can phrase honestly ("about fleet-normal"
   vs "p90 — a genuine departure").
4. **Execution leads the evidence hierarchy.** Realized-speed-vs-target joins the Tier-1 digest
   and the play predicates as a first-class signal; the matcher's narrative rubric ranks "under
   target / wrong sail past its crossover" above "N nm off the line". The most-armed plays will
   be pace + sail-guidance plays — the matcher must be best at exactly those.
5. **Pivot hygiene is point-of-sail-aware.** A lateral SWITCH recommendation downwind requires
   stronger/longer confirmation than upwind (lateral pivots bought little in the runner; upwind
   leverage genuinely pays). Composes with the existing point-of-sail favored-side frame.
6. **TWS scenarios outrank TWD scenarios downwind.** Top boats beat their own optimal by sailing
   hotter angles in pressure — the router under-rewards pressure/angle off the wind. The
   ×0.75/×1.25 TWS-scaling scenarios are the "more/less pressure" plays and take fan-out priority
   over rotations for downwind-heavy races. (Deeper fix — calibrating the downwind polar overlay
   from FLEET evidence — is a Lab-4 extension, not Phase B.)
7. **Venue side statistics accumulate as a labeled prior.** 2025: the Div-I top third worked right
   18:2. Each ingested race adds a data point; the bundle carries "historical side stats at this
   venue" as clearly-labeled historical context for the matcher's tie-breaker narrative — never a
   forecast.
8. **Two follow-on enrichments (post-B):** oracle-regret scoring (separate forecast-bust from
   slow execution — the scenario-mining input) and the **known-answer playbook backtest** —
   synthesize the 2025 playbook as-of-gun and replay the realized wind through the selector; it
   should have pivoted right early. Run before the 2026 race if time allows.

## 9. Phasing

- **Phase A — descope + model chain** (this commit): remove LLM origination from the copilot
  runtime + prompts + tests; remove the training system; Fable→Opus chain in synthesis/briefing;
  docs sweep.
- **Phase B — external scenario fan-out** *(shipped 2026-07-07)*: `scenarios.py` registry;
  perturbation/timing/ensemble scenarios; v2 schema + nominal/robustness dedupe; Fable synthesis
  writes plays with both condition forms; Gameplan UI shows the play library.
- **Phase C — internal plays** *(shipped 2026-07-07)*: pace re-routes from waypoints; sail-loss
  inventory re-runs (envelope rebuilt over the remaining sails, sail labels remapped); wrong-sail/
  overpowered guidance plays; low-maneuver variant (`maneuver_prune_mult` ×3–5, prune-only so the
  ETA delta stays honest; `PB_LOWMAN_MULT`); rejoin-vs-continue tabulation (per-leg off-track
  positions at the venue commit-band XTE → a guidance play carrying the table).
- **Phase D — onboard matcher** *(shipped 2026-07-07)*: Tier-1 `matcher.py` (predicates vs
  live signals, arm-slow/clear-fast sustain) + engine `GET /plays` + the crew sail-state store
  (`/sails/state`: hoisted + the out-of-service gear toggle — the gear-loss plays' arming
  signal); armed plays ride the `/strategy` digest; Strategy-card ARMED-PLAYS section + gear
  toggle; auto-coach `plays` callout (points in the play's frozen words); Tier-2 grounding
  extended to `play:<id>` + `get_plays` in the copilot gather. *Remaining slice: a dedicated
  Tier-2 ranked-match prompt section (the LLM reads narratives + ranks compound matches).*
  Honest v1 limits: `applicability.legs` carried but not gated; `polar_pct` not wired onboard.
  (A LoRA pass, if ever, comes after D with a rubric built on match quality.)

## 10. Honest limits

- Scenario plays are only as good as the transforms — a ±20° rotation is a crude stand-in for a
  real synoptic bust. The library's job is to bound the *decision space*, not predict the weather.
- Predicate thresholds are first-cut and env-tunable; the Lab-4 archive is the calibration source.
- The 7B's narrative matching can misread; the deterministic ARMED set is always shown alongside,
  and the deterministic recommendation always stands on any LLM trouble (unchanged fallback
  discipline).
