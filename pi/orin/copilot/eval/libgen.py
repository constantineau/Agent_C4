"""Synthetic play-library generator (MATCHER_LORA_PLAN §3.1).

Emits v2-shaped bundles varied over the Phase-B scenario families — different predicate
thresholds, sustain windows, applicability legs and NARRATIVE PHRASINGS — so an eval (or a
training run) sees many libraries and must read the one in front of it rather than memorize one
race's plays. Predicates use exactly the signal vocabulary of matcher.gather(); narratives quote
their own thresholds the way the Lab's synthesis writes them, which is what makes a
just-below-threshold scenario a fair near-miss test.

Two CONTENT splits (#54): "train" draws the training families with their training phrasings;
"eval" uses held-out phrasings for every family plus held-out families training never generates
(_HELDOUT_FAMILIES). A held-out score is therefore reading a play, not recalling a template.

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
         narr_eval=[
               "Compared with the frozen forecast, the wind now sits {t0}° or more further "
               "clockwise, and the tactics read calls the shift lasting rather than an "
               "oscillation.",
               "Clockwise rotation of {t0}°+ against the plan's wind, confirmed persistent: the "
               "right-hand forecast scenario is the one playing out."],
         guidance="Consolidate right — the rotation variant's breeze is here."),
    dict(key="rot-left", category="external", kind="rotation",
         preds=[("drift_twd_signed_deg", "<=", (-25, -10), "°"), ("shift_persistent", "==", True, "")],
         name="Left rotation arriving",
         narr=["The breeze has walked LEFT {t0a}°+ vs the frozen forecast and the shift is "
               "persistent — the left-rotation scenario is verifying.",
               "Persistent left shift of at least {t0a}° against the plan's promise."],
         narr_eval=[
               "Against the frozen forecast the wind has gone counter-clockwise by {t0a}° or more"
               " and it is a lasting shift, not a swing that will come back.",
               "Counter-clockwise rotation of at least {t0a}° vs the plan, holding steady — sail "
               "the left-hand scenario."],
         guidance="Work left with the rotation."),
    dict(key="pressure-gone", category="external", kind="pressure",
         preds=[("plangap_tws_kn", "<=", (-6, -3), " kn")],
         name="Promised breeze missing",
         narr=["The promised breeze isn't here: own wind reads {t0a} kn or more UNDER what the "
               "frozen forecast promised for this position and hour.",
               "Plan gap: we are {t0a}+ kn light of the playbook's promised pressure."],
         corr=[("upcourse_tws_delta_kn", "<=", -3,
                "the up-course buoy is also under the promise")],
         narr_eval=[
               "The breeze we planned on has not arrived: measured wind is at least {t0a} kn "
               "lighter than the plan's number for this spot and time.",
               'Wind deficit of {t0a}+ kn versus the playbook forecast at our position.'],
         guidance="Stop waiting for the plan's breeze — sail the pressure you can see."),
    dict(key="build-reef", category="internal", kind="sail_guidance",
         preds=[("tws_trend_kn_per_hr", ">=", (1.5, 2.5), " kn/hr"), ("tws_kn", ">=", (14, 18), " kn")],
         name="Building into the reef window",
         narr=["Breeze building at {t0}+ kn/hr with {t1}+ kn already across the deck — the reef-1 "
               "window is approaching; stage the change early.",
               "Sustained build ({t0} kn/hr or better) on top of {t1}+ kn: sail-change window "
               "opening from below."],
         narr_eval=[
               "Wind rising by {t0} kn or more per hour and already at {t1} kn or above — get the"
               " reef in before it is forced on us.",
               'At {t1}+ kn true and climbing {t0}+ kn every hour: the first-reef call is close.'],
         guidance="Brief the reef early — change on your terms, not the gust's."),
    dict(key="pace-behind", category="internal", kind="pace",
         preds=[("time_behind_min", ">=", (12, 30), " min"), ("polar_pct", "<=", (85, 92), "%")],
         legs=True,
         name="Behind plan pace",
         narr=["Behind the plan by {t0}+ minutes AND boatspeed under {t1}% of target — this is a "
               "pace problem, not a trim wobble; the pace re-route from this mark applies.",
               "Pace play: {t0} minutes or more behind plan with polars under {t1}% on this leg."],
         narr_eval=[
               "Running {t0} minutes or more late against the plan while sailing at or below "
               "{t1}% of polar — the slow-boat contingency route from this mark is the answer.",
               "Late by at least {t0} min and slow (polar {t1}% or less) on this leg: switch to "
               "the pace contingency."],
         guidance="Take the pace re-route from the applicable mark."),
    dict(key="leverage-open", category="external", kind="timing",
         preds=[("xte_nm", ">=", (2.0, 5.0), " nm")],
         name="Leverage opened",
         narr=["We are {t0}+ nm off the planned track — real leverage against the plan is open and "
               "the rejoin-vs-continue table applies.",
               "Cross-track error past {t0} nm: the split from the frozen route is now a position, "
               "not a wobble."],
         narr_eval=[
               "Separation from the planned line has reached {t0} nm or more; treat it as a "
               "committed split and run the rejoin table.",
               "Off the frozen route by {t0}+ nm cross-track — this is now leverage, decide it on"
               " purpose."],
         guidance="Decide rejoin-vs-continue deliberately; don't drift into the split."),
    dict(key="gear-s1-out", category="internal", kind="gear_loss",
         preds=[("sail_out_of_service", "==", "S1", "")],
         name="S1 out of service",
         narr=["The S1 is out of service — the gear-loss re-run without it applies; downwind legs "
               "re-planned on the J1/A3 envelope.",
               "Running without the S1 (flagged out of service): use the no-S1 route and sail "
               "plan."],
         narr_eval=[
               'With the S1 unavailable, sail the alternate downwind plan that never calls for it.',
               'S1 damaged or stowed for good: switch to the plan built without the S1.'],
         guidance="Switch to the no-S1 plan; expect hotter angles on the A3."),
    dict(key="changedown", category="internal", kind="sail_guidance",
         preds=[("tws_kn", ">=", (18, 24), " kn"), ("hoisted_sail", "==", "J1", "")],
         name="Overpowered on the J1",
         narr=["{t0}+ kn with the J1 still up — past the change-down crossover; the smaller jib is "
               "faster and safer now.",
               "J1 flying above {t0} kn TWS: overpowered window, change down."],
         narr_eval=[
               "True wind at {t0} kn or more and the J1 is still the headsail — move to the "
               "smaller jib now.",
               'J1 hoisted with {t0}+ kn of breeze: we are past its range, go down a size.'],
         guidance="Change down from the J1 — the crossover table says so."),
    dict(key="c0-window", category="internal", kind="sail_guidance",
         preds=[("tws_kn", "<=", (8, 10), " kn"), ("hoisted_sail", "==", "J1", "")],
         name="Code 0 window",
         narr=["Under {t0} kn on the J1 — the Code 0 crossover window is open if the angle allows.",
               "Light air ({t0} kn or less) with the jib up: C0 window."],
         narr_eval=[
               "Wind at or below {t0} kn with the J1 up: if the angle suits, the Code 0 is the "
               "faster sail.",
               'J1 flying in {t0} kn or less — light enough for the Code 0 if the angle works.'],
         guidance="Consider the C0 in its TWA band."),
    dict(key="fatigue-rotate", category="internal", kind="crew",
         preds=[("fatigue_index", ">=", (0.65, 0.8), ""), ("polar_pct", "<=", (90, 94), "%")],
         name="Helm fade showing in the numbers",
         narr=["Helm fatigue reads {t0}+ with boatspeed slipping under {t1}% of target — the fade "
               "is costing distance now.",
               "Fatigue index at or above {t0} and polars under {t1}%: rotate before the next "
               "maneuver-heavy stretch."],
         narr_eval=[
               "Driver fatigue at {t0} or higher and speed at or below {t1}% of polar — change "
               "helmsman before the next busy stretch.",
               "Tired helm ({t0}+ on the fatigue index) with polar at {t1}% or worse: hand the "
               "wheel over."],
         guidance="Rotate the helm; performance first, pride later."),
]


# HELD-OUT families (#54): never generated for training, so the eval measures reading a play the
# model has never seen rather than recalling a template. Same signal vocabulary as matcher.gather(),
# new combinations and new wording. Thresholds sit inside the engine's picture bands so an armed
# play's evidence is visible in the strategy seed (see scengen.visible_signals).
_HELDOUT_FAMILIES = [
    dict(key="breeze-fading", category="external", kind="pressure",
         preds=[("tws_trend_kn_per_hr", "<=", (-2.5, -1.0), " kn/hr")],
         name="Breeze fading",
         narr_eval=["Wind dropping by {t0a} kn per hour or faster over the last hour: the fade the "
                    "plan feared is under way; get into light-air mode early.",
                    "Breeze dying off at {t0a}+ kn/hr: expect lighter air ahead and set up for "
                    "it."],
         guidance="Shift to light-air mode before the lull arrives."),
    dict(key="pressure-over", category="external", kind="pressure",
         preds=[("plangap_tws_kn", ">=", (4, 7), " kn")],
         name="More breeze than planned",
         narr_eval=["Own wind is {t0}+ kn above the plan's number for this place and time: we are "
                    "sailing a windier race than the one we planned.",
                    "Surplus of at least {t0} kn over the forecast the playbook was built on."],
         guidance="Sail the windier plan: depower early, check the sail crossovers."),
    dict(key="split-and-late", category="internal", kind="timing",
         preds=[("xte_nm", ">=", (1.0, 3.0), " nm"), ("time_behind_min", ">=", (6, 15), " min")],
         name="Split and late",
         narr_eval=["{t0}+ nm away from the planned line AND {t1}+ minutes late: the split is not "
                    "paying, so decide now whether to come back.",
                    "Both off the track by {t0} nm or more and behind the plan's clock by {t1}+ "
                    "minutes: rejoin unless something new says otherwise."],
         guidance="Make the rejoin call on purpose, now."),
    dict(key="upcourse-more", category="external", kind="pressure",
         preds=[("upcourse_tws_delta_kn", ">=", (3, 6), " kn")],
         name="Pressure up the course",
         narr_eval=["The buoy up the course reads {t0}+ kn more wind than we have: new pressure is "
                    "on its way down to us.",
                    "Up-course station {t0} kn or more windier than our own instruments: stage for "
                    "the extra breeze."],
         guidance="Get ready for more breeze: sail plan and crew weight first."),
]

SPLITS = ("train", "eval")

def _threshold(rng, spec):
    lo_hi = spec[2]
    if not isinstance(lo_hi, tuple):
        return lo_hi                          # fixed value (== predicates)
    lo, hi = lo_hi
    step = 0.5 if (hi - lo) <= 6 else 1.0
    n = int((hi - lo) / step)
    return round(lo + rng.randint(0, n) * step, 1)


def make_play(rng, fam, idx, split="train"):
    preds, tvals = [], []
    for spec in fam["preds"]:
        val = _threshold(rng, spec)
        tvals.append(val)
        preds.append({"signal": spec[0], "op": spec[1], "value": val,
                      "sustain_min": rng.choice([0, 5, 10, 15])})
    fmt = {f"t{i}": (f"{v:g}" if isinstance(v, (int, float)) else v) for i, v in enumerate(tvals)}
    fmt.update({f"t{i}a": f"{abs(v):g}" for i, v in enumerate(tvals) if isinstance(v, (int, float))})
    narrative = rng.choice(fam["narr" if split == "train" else "narr_eval"]).format(**fmt)
    play = {
        "id": f"{fam['key']}-{idx}", "name": fam["name"], "category": fam["category"],
        "family": fam["key"],
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


def families(split):
    """The families a split may draw from. train: the training families only. eval: every family,
    held-out ones included."""
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    return list(_FAMILIES) if split == "train" else list(_FAMILIES) + list(_HELDOUT_FAMILIES)


def heldout_keys():
    return {f["key"] for f in _HELDOUT_FAMILIES}


def make_library(rng, n_plays=6, race_idx=0, split="train"):
    """One synthetic v2-shaped bundle: n_plays sampled without family repeats + two plain variants
    (Playbook.digest() renders variants, so the prompt path needs them to exist).

    split="train": training families, training phrasings. split="eval": held-out phrasings for
    every family plus at least one held-out family — no narrative an eval play carries can be
    produced by the train split (test_eval checks this), so a score on it is reading, not recall."""
    if split == "eval":
        held = list(_HELDOUT_FAMILIES)
        first = [rng.choice(held)]
        rest = [f for f in families("eval") if f is not first[0]]
        fams = first + rng.sample(rest, min(n_plays, len(rest) + 1) - 1)
        rng.shuffle(fams)
    else:
        pool = families("train")
        fams = rng.sample(pool, min(n_plays, len(pool)))
    plays = [make_play(rng, fam, i, split) for i, fam in enumerate(fams)]
    return {
        "schema": "c4.playbook/v2",
        "race_id": f"{split}-race-{race_idx}",
        "variants": [
            {"id": "left", "summary": "Start left of rhumb for the forecast left phase.",
             "what_flips_it": "A persistent right shift past the rhumb."},
            {"id": "middle", "summary": "Hold rhumb; models split, stay reactive.",
             "what_flips_it": "Either side's rotation verifying for 30+ min."},
        ],
        "plays": plays,
    }
