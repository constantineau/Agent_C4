"""Synthetic play-library generator (MATCHER_LORA_PLAN §3.1).

Emits v2-shaped bundles varied over the Phase-B scenario families — different predicate
thresholds, sustain windows, applicability legs and NARRATIVE PHRASINGS — so an eval (or a
training run) sees many libraries and must read the one in front of it rather than memorize one
race's plays. Predicates use exactly the signal vocabulary of matcher.gather(); narratives quote
their own thresholds the way the Lab's synthesis writes them, which is what makes a
just-below-threshold scenario a fair near-miss test.

Every generator here is seeded-RNG deterministic: same seed -> same corpus, reproducible splits.
"""

import random

# (signal, op, (lo, hi), unit, phrasings) — each phrasing formats with t=threshold.
# Several plays are COMPOUND (two predicates) because missed compound matches are a §1 eval axis.
_FAMILIES = [
    dict(key="rot-right", category="external", kind="rotation",
         preds=[("drift_twd_signed_deg", ">=", (10, 25), "°"), ("shift_persistent", "==", True, "")],
         name="Right rotation arriving",
         narr=["The forecast right rotation is arriving — live wind has walked right {t0}°+ vs the "
               "frozen promise and the shift reads persistent, not oscillation.",
               "Wind gone right by at least {t0}° against the plan's promised direction, and it is "
               "holding (persistent shift, no oscillation)."],
         guidance="Consolidate right — the rotation variant's breeze is here."),
    dict(key="rot-left", category="external", kind="rotation",
         preds=[("drift_twd_signed_deg", "<=", (-25, -10), "°"), ("shift_persistent", "==", True, "")],
         name="Left rotation arriving",
         narr=["The breeze has walked LEFT {t0a}°+ vs the frozen forecast and the shift is "
               "persistent — the left-rotation scenario is verifying.",
               "Persistent left shift of at least {t0a}° against the plan's promise."],
         guidance="Work left with the rotation."),
    dict(key="pressure-gone", category="external", kind="pressure",
         preds=[("plangap_tws_kn", "<=", (-6, -3), " kn")],
         name="Promised breeze missing",
         narr=["The promised breeze isn't here: own wind reads {t0a} kn or more UNDER what the "
               "frozen forecast promised for this position and hour.",
               "Plan gap: we are {t0a}+ kn light of the playbook's promised pressure."],
         corr=[("upcourse_tws_delta_kn", "<=", -3,
                "the up-course buoy is also under the promise")],
         guidance="Stop waiting for the plan's breeze — sail the pressure you can see."),
    dict(key="build-reef", category="internal", kind="sail_guidance",
         preds=[("tws_trend_kn_per_hr", ">=", (1.5, 2.5), " kn/hr"), ("tws_kn", ">=", (14, 18), " kn")],
         name="Building into the reef window",
         narr=["Breeze building at {t0}+ kn/hr with {t1}+ kn already across the deck — the reef-1 "
               "window is approaching; stage the change early.",
               "Sustained build ({t0} kn/hr or better) on top of {t1}+ kn: sail-change window "
               "opening from below."],
         guidance="Brief the reef early — change on your terms, not the gust's."),
    dict(key="pace-behind", category="internal", kind="pace",
         preds=[("time_behind_min", ">=", (12, 30), " min"), ("polar_pct", "<=", (85, 92), "%")],
         legs=True,
         name="Behind plan pace",
         narr=["Behind the plan by {t0}+ minutes AND boatspeed under {t1}% of target — this is a "
               "pace problem, not a trim wobble; the pace re-route from this mark applies.",
               "Pace play: {t0} minutes or more behind plan with polars under {t1}% on this leg."],
         guidance="Take the pace re-route from the applicable mark."),
    dict(key="leverage-open", category="external", kind="timing",
         preds=[("xte_nm", ">=", (2.0, 5.0), " nm")],
         name="Leverage opened",
         narr=["We are {t0}+ nm off the planned track — real leverage against the plan is open and "
               "the rejoin-vs-continue table applies.",
               "Cross-track error past {t0} nm: the split from the frozen route is now a position, "
               "not a wobble."],
         guidance="Decide rejoin-vs-continue deliberately; don't drift into the split."),
    dict(key="gear-a2-out", category="internal", kind="gear_loss",
         preds=[("sail_out_of_service", "==", "A2", "")],
         name="A2 out of service",
         narr=["The A2 is out of service — the gear-loss re-run without it applies; downwind legs "
               "re-planned on the A3/S2 envelope.",
               "Running without the A2 (flagged out of service): use the no-A2 route and sail plan."],
         guidance="Switch to the no-A2 plan; expect deeper/hotter angles on the A3."),
    dict(key="changedown", category="internal", kind="sail_guidance",
         preds=[("tws_kn", ">=", (18, 24), " kn"), ("hoisted_sail", "==", "J1", "")],
         name="Overpowered on the J1",
         narr=["{t0}+ kn with the J1 still up — past the change-down crossover; the smaller jib is "
               "faster and safer now.",
               "J1 flying above {t0} kn TWS: overpowered window, change down."],
         guidance="Change down from the J1 — the crossover table says so."),
    dict(key="c0-window", category="internal", kind="sail_guidance",
         preds=[("tws_kn", "<=", (8, 10), " kn"), ("hoisted_sail", "==", "J1", "")],
         name="Code 0 window",
         narr=["Under {t0} kn on the J1 — the Code 0 crossover window is open if the angle allows.",
               "Light air ({t0} kn or less) with the jib up: C0 window."],
         guidance="Consider the C0 in its TWA band."),
    dict(key="fatigue-rotate", category="internal", kind="crew",
         preds=[("fatigue_index", ">=", (0.65, 0.8), ""), ("polar_pct", "<=", (90, 94), "%")],
         name="Helm fade showing in the numbers",
         narr=["Helm fatigue reads {t0}+ with boatspeed slipping under {t1}% of target — the fade "
               "is costing distance now.",
               "Fatigue index at or above {t0} and polars under {t1}%: rotate before the next "
               "maneuver-heavy stretch."],
         guidance="Rotate the helm; performance first, pride later."),
]


def _threshold(rng, spec):
    lo_hi = spec[2]
    if not isinstance(lo_hi, tuple):
        return lo_hi                          # fixed value (== predicates)
    lo, hi = lo_hi
    step = 0.5 if (hi - lo) <= 6 else 1.0
    n = int((hi - lo) / step)
    return round(lo + rng.randint(0, n) * step, 1)


def make_play(rng, fam, idx):
    preds, tvals = [], []
    for spec in fam["preds"]:
        val = _threshold(rng, spec)
        tvals.append(val)
        preds.append({"signal": spec[0], "op": spec[1], "value": val,
                      "sustain_min": rng.choice([0, 5, 10, 15])})
    fmt = {f"t{i}": (f"{v:g}" if isinstance(v, (int, float)) else v) for i, v in enumerate(tvals)}
    fmt.update({f"t{i}a": f"{abs(v):g}" for i, v in enumerate(tvals) if isinstance(v, (int, float))})
    narrative = rng.choice(fam["narr"]).format(**fmt)
    play = {
        "id": f"{fam['key']}-{idx}", "name": fam["name"], "category": fam["category"],
        "scenario": {"kind": fam["kind"]},
        "summary": narrative.split("—")[0].strip()[:120],
        "conditions": {"narrative": narrative, "predicates": preds},
        "response": {"type": "guidance", "guidance": fam["guidance"]},
        "stakes_min": rng.choice([5, 10, 20, 40]),
        "what_flips_it": "Conditions clear below threshold (clear-fast).",
    }
    if fam.get("legs"):
        play["applicability"] = {"gate": "hard", "legs": sorted(rng.sample(range(4), 2))}
    if fam.get("corr"):
        play["conditions"]["corroborators"] = [
            {"signal": s, "op": op, "value": v, "why": why} for s, op, v, why in fam["corr"]]
    return play


def make_library(rng, n_plays=6, race_idx=0):
    """One synthetic v2-shaped bundle: n_plays sampled without family repeats + two plain variants
    (Playbook.digest() renders variants, so the prompt path needs them to exist)."""
    fams = rng.sample(_FAMILIES, min(n_plays, len(_FAMILIES)))
    plays = [make_play(rng, fam, i) for i, fam in enumerate(fams)]
    return {
        "schema": "c4.playbook/v2",
        "race_id": f"eval-race-{race_idx}",
        "variants": [
            {"id": "left", "summary": "Start left of rhumb for the forecast left phase.",
             "what_flips_it": "A persistent right shift past the rhumb."},
            {"id": "middle", "summary": "Hold rhumb; models split, stay reactive.",
             "what_flips_it": "Either side's rotation verifying for 30+ min."},
        ],
        "plays": plays,
    }
