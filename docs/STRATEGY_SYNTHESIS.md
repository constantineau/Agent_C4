# In-race Strategy Synthesis — the onboard copilot as a tactician

**Status:** design / plan (2026-07-03). Companion to `docs/RRS41_COMPLIANCE.md` (§4 — the on-boat vs
off-boat line) and `docs/ONBOARD_ENGINE_SCOPING.md` §3 (the Orin as a judgment layer). No code yet.

## What this adds

Today the onboard copilot narrates each strategic signal in its own silo: a route-deviation callout, a
forecast-drift callout, a fleet-rival callout, a playbook-adherence tile. The **`selector`** unifies
three of them (wind shift + deviation + drift) into a HOLD / SWITCH / OFF-SCRIPT pick — but by *fixed
concordance rules*, and it says nothing about the fleet.

This increment gives the copilot a **higher-order synthesis**: reason *across* forecast-vs-actual, fleet
positioning, and route deviation at once to assess **the overall plan** — "are we still on the right
strategy, and if not, what should change?" — and, when the situation outruns the pre-authored branches,
**originate a suggestion**. This is now squarely legal: the copilot runs on the boat, so it's the boat's
own tactician, not outside help (see the compliance doc — the reframe of 2026-07-03).

The value is the **cross-signal narrative** the siloed callouts miss:
- *concordant* — "the forecast veer, the fleet's split left, and your position all point right — this is
  a high-confidence moment to consolidate right."
- *discordant* — "you're on the plan, but the fleet went the other way and the forecast is starting to
  disagree — one of you is about to be wrong; hold and watch the next shift."

## Design principle — deterministic weighing, LLM synthesis + origination

Keep the **numbers and the mechanical concordance deterministic** (Tier-1, reliable, math-correct), and
let the **LLM synthesize the picture, explain the interplay, and originate the recommendation** (Tier-2).
The LLM may now propose strategy beyond the playbook — the guardrails that remain are for *reliability*,
not compliance:
1. **The engine does the math.** Every number in the synthesis comes from the deterministic signal
   streams; the 7B never computes XTE, corrected-time, or drift itself.
2. **Grounded.** Every element of the picture and every recommendation cites a real signal tool
   (`get_drift`/`get_fleet`/`get_deviation`/`get_tactics`/`get_selector`) or a playbook variant, or it's
   dropped by the validator — the model may *reason to* a fresh call, it may not *fabricate the facts*.
3. **Playbook is the strong prior.** The frozen Lab-2 variants are the trusted default (pre-race Opus +
   full GRIB > the onboard 7B); a recommendation that departs from them is explicitly flagged `off-book`.
4. **Advisory + confidence + deterministic fallback.** Always returns a brief; if the LLM is off/slow/
   ungrounded, the `selector`-derived deterministic brief stands in.

## The inputs (all already computed onboard — no new engine math)

| Stream | Endpoint / fn | Key grounded fields |
|---|---|---|
| Forecast vs actual | `/drift` · `drift.get_drift` | `drift_twd_signed_deg`, `drift_dir`, `drift_tws_kn`, `worst`, `status` |
| Competitor positioning | `/fleet` · `fleet.get_fleet` | per-boat `corrected_delta_s`, `tag` (rival/ahead_corrected), `leverage_nm`, `confidence` |
| Route deviation | `/deviation` · `deviation.get_deviation` | `xte_nm`+`xte_side`, `along_pct`, `time_behind_s`, `vmc_deficit_kn`, `status` |
| On-water shift | `/tactics` · `tactics.get_tactics` | `wind.persistent`, `favored_side`, `leverage`, `phase` |
| Unified branch pick | `/selector` · `selector.get_selector` | `action`, `target_variant`, `confidence`, `driven_by`, `signals` |
| (fallback route) | `/reoptimize` · `reoptimize.get_reoptimize` | fresh onboard route when off-book |

## The artifact — `StrategyBrief`

A superset of the `DecisionBrief` grounding pattern (`pi/orin/copilot/brief.py`):

```jsonc
{
  "assessment": "<1–2 sentences: are we on the right overall plan? the emerging picture>",
  "picture": [                          // the synthesized higher-order reads, each grounded
    {"signal": "forecast|fleet|deviation|shift|concordance",
     "read": "<what it says in tactician's words>",
     "grounded_in": ["get_drift"|"get_fleet"|"get_deviation"|"get_tactics"|"get_selector"],
     "confidence": "high|med|low"}
  ],
  "concordance": {                      // DETERMINISTIC (Tier-1) + LLM-explained
    "agree": true, "lean": "right|left|hold|split",
    "strength": "strong|weak|split",
    "note": "<what the agreement/disagreement means>"},
  "recommendation": {
    "action": "<hold | consolidate <side> | switch to <variant> | reassess | off-book: <new plan>>",
    "vs_playbook": "on-plan|departs",   // flagged when it goes beyond the frozen variants
    "target_variant": "<id>|null",
    "rationale": "<why, drawn from the picture>",
    "grounded_in": ["get_selector","get_drift", ...],
    "urgency": "now|soon|monitor",
    "confidence": "high|med|low"},
  "caveats": ["<engine-authored: each stream's uncertainty>"],
  "confidence": "high|med|low",
  "mode": "llm|deterministic",
  "disclaimer": "Advisory. The crew decides."
}
```

`concordance` is computed deterministically (does forecast direction agree with the fleet's committed
side and the boat's leverage?) so the *judgment of agreement* is math, not a 7B guess; the LLM only
phrases it and folds it into `assessment`/`recommendation`.

## Phasing

**Phase 0 — Tier-1 strategy digest (no LLM, bench-verifiable).**
- Extend `selector.py` (or a new `strategy.py` beside it) with `get_strategy_signals()` → a normalized,
  grounded cross-signal digest: the four reads above + a deterministic `concordance` (direction agreement
  + strength) + a deterministic `StrategyBrief` (wrap the existing `selector` HOLD/SWITCH/OFF-SCRIPT into
  the new shape, add the fleet read).
- New engine endpoint **`GET /strategy`** on `pi/engine/engine_app.py` serving the deterministic brief
  (legal, no LLM, `na` with no playbook aboard).
- **Exit test:** unit tests for concordance (concordant → strong lean; discordant → split) + the digest
  shape; bench e2e against `:8200` (load a playbook + fleet + a perturbed drift/deviation → `/strategy`
  returns the right lean).

**Phase 1 — Tier-2 LLM synthesis (`POST /strategy` on the copilot).**
- `copilot.gather()` already pulls drift/fleet/deviation/tactics; add `get_selector` + the Phase-0 digest.
- New `strategy_brief()` in `copilot.py`: seed the LLM with the digest + the playbook, bounded tool loop,
  emit a `StrategyBrief`, run it through `brief.validate()` (extend `allowed_sources` with the signal
  tools + `playbook:<id>`), engine-authored caveats. **Deterministic fallback = the Phase-0 brief.**
- Copilot service endpoint **`POST /strategy {route?, hoisted?, use_llm?}`** in `pi/orin/copilot/app.py`.
- **Exit test:** `bench_copilot` case (grounded synthesis, concordant + discordant fixtures, off-book
  flag, fallback fires); live against the real Orin (:11434) + Pi engine (:8200).

**Phase 2 — surface it (iPad Strategy card + coach).**
- The Strategy card (`pi/console/dashboard/`) gains a **synthesis section** above the triggers: the
  `assessment` headline + the concordance read + the recommendation (with the `off-book` badge when it
  departs). Its own ~15 s cadence, `mode` pill (llm/engine), tap-to-detail streams the reasoning
  (`POST /detail` pattern).
- The auto-coach (`coach.py`) can volunteer a **strategy callout** (`category: "strategy"`, priority
  just under playbook) when the recommendation changes — raise-slow/clear-fast, same dedup as the rest.

**Phase 3 — proactive + off-book chaining + tuning.**
- When the recommendation is `off-book`, chain the `/reoptimize` hint (already built) so the card can
  offer the fresh onboard route.
- A later **LoRA pass** (see `docs/ORIN_LORA_PLAN.md`) can target strategy-synthesis quality once the
  reliability pilot lands — this pilot targets brief JSON/tool reliability first.

## Guardrails recap (reliability, not compliance)

- Engine does the math (digest numbers are all deterministic) · grounded-or-dropped · playbook is the
  strong prior with an explicit `off-book` flag · advisory + confidence + disclaimer · deterministic
  fallback always available. Fully offline in-race — inputs are own sensors + common public data (GRIB /
  Open-Meteo / own AIS); **nothing is fetched from off-boat while racing** (the one real RRS-41 line).

## Honest limits (state them in the output)

- All three streams are **uncertain**: AIS coverage is partial, corrected-time is a projection, a
  forecast is a forecast. The synthesis must be **confidence-weighted and hedged** — its job is exactly
  to say "these three *weakly* agree, so lean but don't commit."
- The 7B's synthesis prose is imperfect (grounded numbers are always right; the narrative can misread —
  the LoRA pass addresses this).
- The playbook stays the strong prior; `off-book` suggestions are for when the situation genuinely
  outruns the pre-authored branches, not a license to freelance every shift.
