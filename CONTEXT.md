# Agent_C4 — SR33 AI Navigator

Race strategy, onboard navigation, and post-race learning for the SR33 "C4".
The vocabulary below is the project's shared language — CLAUDE.md holds the
operational detail; this file only settles what words mean.

## Language

### Rules & architecture framing

**Bright line**:
The RRS-41 compliance boundary: all frontier/cloud computation happens pre-start and
is frozen at the gun; in-race work runs onboard on own sensors + public data. Never
crossed, never argued with.

**Homework**:
Everything the boat needs in-race, compiled ashore and loaded frozen: playbook,
obstacles, fleet roster, forecast fingerprint, venue stats, course, checklists.
_Avoid_: pre-race data, bundle contents

**Race-gated**:
Cloud advice tools withheld while racing, fail-closed — missing race-mode state means
RACING, and the tools refuse.

**Tier 1 / Tier 2 / Tier 3**:
Onboard deterministic engine (Pi, no LLM, legal in-race) / onboard LLM copilot (Orin,
narrates and condition-matches, never originates strategy) / cloud (Lab + agent,
between races only).

### Strategy objects

**Playbook**:
The signed, frozen bundle of pre-authored strategy — variants, plays, venue stats,
forecast fingerprint — loaded aboard via `/playbook/load` before the gun.

**Play**:
One pre-authored conditional recommendation: predicates over live signals, arm-slow /
clear-fast hysteresis, per-leg applicability, a narrative. Plays arm; they never
originate aboard.

**Variant**:
A pre-computed route alternative in the playbook (e.g. a side variant). The selector
switches between variants; anything outside them is off-script.

**Scenario fan**:
Routes computed across perturbed forecasts (rotation, pressure, timing, sea state)
to locate the decision boundaries the plays encode.

**Forecast fingerprint**:
The frozen forecast the plan was routed on — the "promise" that live signals compare
reality against.
_Avoid_: baseline forecast

**Venue stats**:
Seasonal/historical statistics for the racing venue, frozen from the retro archive
into the homework.

### Live signals (the executor stack)

**Deviation**:
Live position vs the frozen recommended track: XTE and side, along-track progress,
time behind plan.

**Drift**:
The live forecast re-sampled at the fingerprint, vs the fingerprint — a
forecast-vs-forecast signal (same source, no cross-model bias).

**Plangap**:
Own observed wind vs the fingerprint's promise for here/now — the obs-vs-forecast
signal drift can't produce.

**Selector**:
The single unified verdict over shift + deviation + drift across the frozen variants:
HOLD / SWITCH → variant / OFF-SCRIPT, with confidence.

**Off-script**:
Conditions outside every frozen variant. Triggers the onboard re-route fallback
(own polars + live public forecast + frozen obstacles), always flagged off-book.
_Avoid_: off-plan (off-book is an accepted synonym)

**Watchlist**:
The quiet-but-close plays: per-predicate distance-to-trigger surfaced as "what flips
the plan," each with live number vs threshold.

**Corroborator**:
An up-course buoy/station/METAR observation that raises a play's confidence but never
gates it.

**Matcher**:
The Tier-1 engine module that evaluates play predicates against live signals. (The
Orin's fine-tuned model condition-matches narratively on top; the engine's matcher is
the authority.)

### Data & learning

**Collect everything**:
The data doctrine: every sensor reading, per source, raw SI, all sources kept visible;
readers cross-check rather than pre-filtering.

**Session**:
An owner-declared race-log window (the ⏺ LOG switch). Only session windows are kept
long-term and backfilled to the cloud; everything else prunes on the boat.
_Avoid_: recording, trip

**Sail configuration**:
The crew-logged SET of flying sails plus reef state (e.g. C0+J2, kite+staysail).
The unit that config-polars and sail-log debriefs attribute performance to.

**Config polar**:
An observed performance curve grown per sail configuration from debrief data — for
combinations the crossover chart doesn't rate.

**Crossover**:
The sail-selection boundary chart: which sail wins at a given TWS/TWA.

**Oracle re-route**:
The hindsight-optimal route computed for the conditions that actually occurred; the
debrief scores the sailed track against it (regret, XTE, helm %).

**Learning loop**:
Debrief → score vs the oracle → proposed polar/helm/wave-coefficient refinements —
every proposal human-approved before it touches the boat model.

### Crew & ops

**Watch system**:
The crew-rotation block schedule: who's on, countdown, hold/swap/all-hands edits;
authored ashore, edited live on the CREW tile.

**Race checklist**:
The SI/NOR requirement subset delivered aboard in the homework, evaluated
deterministically at its trigger (sunset, mark proximity, finish approach) and
latched until the crew acks.
