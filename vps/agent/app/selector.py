"""Branch SELECTOR — the graceful-degradation decision at the heart of the onboard executor.

Lab-3: the two branch triggers (route-deviation `deviation.py`, forecast-drift `drift.py`) plus the
engine's on-water tactical read (a persistent wind shift, `tactics.py`) each say "something's up".
This module UNIFIES them into a single crew-facing recommendation over the FROZEN playbook:

    HOLD the recommended variant  ·  SWITCH to a pre-authored variant  ·  OFF-SCRIPT (no branch aboard)

It SELECTS a pre-authored variant — it never originates strategy (the RRS-41 posture): the switch
target is always one of the bundle's own variants, and the rationale is the bundle's own
`what_flips_it`. This is the Tier-1 (Pi engine, no Orin) generalization of the copilot's wind-shift-only
`adherence.py` tile — deterministic, legal in-race (own instruments + pre-loaded homework + common data).

GRACEFUL DEGRADATION (perflab item-2): (1) a persistent shift favours a side that HAS a pre-authored
variant → recommend it, with rich frozen rationale; (2) the favoured side has NO variant aboard →
OFF-SCRIPT flag ("sail your own to that side, off the playbook" — the onboard re-optimizer is a later
tier); (3) nothing decisive → HOLD. Forecast-drift and route-deviation don't trigger a switch on their
own (drift is a forecast, deviation is an execution gap) — they REINFORCE a wind-shift switch (raising
confidence/urgency) and, on their own, raise a "reassess" caution. FUZZY (perflab §5): confidence is a
first-class output from how many signals concur; the wind trigger keeps the engine's own persistence
hysteresis so it doesn't flip-flop.
"""
from . import deviation, drift as drift_mod, tactics


def _variant_for_side(bundle, side):
    """The pre-authored variant for a first-beat side. Variant ids ARE the side (left|middle|right;
    from the Lab synthesis), so this is an exact id match — the same contract adherence.py relies on."""
    if side not in ("left", "right", "middle"):
        return None
    for v in bundle.get("variants") or []:
        if str(v.get("id") or "").lower() == side:
            return v
    return None


def _label(v, fallback="the plan"):
    if not v:
        return fallback
    return v.get("name") or str(v.get("id") or fallback)


def _conf_label(c):
    return "high" if c >= 0.7 else "medium" if c >= 0.45 else "low"


def _na(note):
    return {"available": False, "action": "na", "status": "na", "value": "—", "why": note,
            "consider": "—", "based": [], "conf": "engine"}


# a persistent VEER (clockwise, TWD rising) tends to favour the RIGHT of the beat, a BACK the LEFT —
# used only as soft concordance between the forecast-drift direction and the on-water favoured side.
_DRIFT_SIDE = {"veered": "right", "backed": "left"}


def get_selector(route=None):
    """The unified branch recommendation over the frozen playbook. Reuses the two trigger reads +
    the tactical read (each already fuzzy/hysteretic), so this stays a thin, deterministic decision."""
    bundle = deviation._load_playbook()
    if not bundle:
        return _na("no playbook aboard")
    variants = bundle.get("variants") or []
    if not variants:
        return _na("playbook has no variants")

    rec_id = str(bundle.get("recommended") or (variants[0].get("id") if variants else "") or "")
    rec_v = _variant_for_side(bundle, rec_id) or (variants[0] if variants else None)
    rec_label = _label(rec_v, "the start plan")

    dev = deviation.get_deviation(route)
    dft = drift_mod.get_drift(route)
    tac = tactics.get_tactics(route)

    wind = (tac.get("wind") or {}) if tac.get("available") else {}
    persistent = bool(wind.get("persistent"))
    favored = tac.get("favored_side") if tac.get("available") else None    # left | right | either
    trend = wind.get("trend")
    trend_txt = (trend + " ") if trend and trend not in (None, "steady") else ""

    dev_ok = dev.get("available")
    dft_ok = dft.get("available")
    dev_status = dev.get("status") if dev_ok else None
    dft_status = dft.get("status") if dft_ok else None

    signals = {
        "shift": {"persistent": persistent, "favored_side": favored, "trend": trend},
        "deviation": {"status": dev_status, "side": dev.get("xte_side") if dev_ok else None,
                      "variant": dev.get("variant") if dev_ok else None},
        "drift": {"status": dft_status, "dir": dft.get("drift_dir") if dft_ok else None,
                  "deg": dft.get("drift_twd_deg") if dft_ok else None},
    }
    base = {"available": True, "recommended": rec_id, "recommended_label": rec_label,
            "signals": signals, "conf": "engine"}

    # ---- decisive path: a PERSISTENT shift favours a side other than the one we're on -----------
    if persistent and favored in ("left", "right") and favored != rec_id:
        target = _variant_for_side(bundle, favored)
        driven = ["get_tactics"]
        concur = 0
        # forecast-drift concurs if the forecast has moved the same way (veer→right / back→left)
        if dft_status in ("watch", "act") and _DRIFT_SIDE.get(dft.get("drift_dir")) == favored:
            driven.append("get_drift"); concur += 1
        # route-deviation concurs if we're ALREADY set up to that side (XTE on the favoured hand)
        if dev_status in ("watch", "act") and dev.get("xte_side") == favored:
            driven.append("get_deviation"); concur += 1
        confidence = min(0.95, 0.55 + 0.15 * concur + (0.1 if dft_status == "act" else 0))

        if target is not None:                                    # TIER 1 — pre-authored branch
            tlabel = _label(target, favored)
            flip = target.get("what_flips_it") or ""
            extra = []
            if "get_drift" in driven:
                extra.append(f"the forecast has {dft.get('drift_dir')} ~{round(dft.get('drift_twd_deg', 0))}° the same way")
            if "get_deviation" in driven:
                extra.append(f"you're already working the {favored} side ({dev.get('xte_nm')} nm {favored})")
            why = (f"A persistent {trend_txt}shift now favours the {favored} side — against the "
                   f"recommended '{rec_label}'. That's the playbook's branch trigger"
                   + (f": {flip}" if flip else ".")
                   + (" Reinforced: " + "; ".join(extra) + "." if extra else ""))
            return {**base, "action": "switch", "status": "act", "tier": 1,
                    "target_variant": str(target.get("id")), "target_label": tlabel,
                    "value": f"Switch → {tlabel}", "driven_by": driven,
                    "confidence": round(confidence, 2), "confidence_label": _conf_label(confidence),
                    "why": why,
                    "consider": f"Execute the branch — commit {favored} per the pre-authored '{tlabel}'.",
                    "clears": "the shift reverses / settles back toward the rhumb",
                    "based": driven + [f"playbook:{target.get('id')}"], "what_flips_it": flip}

        # TIER 2 — favoured side has NO pre-authored variant aboard: off the playbook.
        confidence = min(0.7, confidence)
        return {**base, "action": "off_script", "status": "act", "tier": 2,
                "target_variant": None, "target_label": None,
                "value": f"Off-script: sail {favored}", "driven_by": driven,
                "confidence": round(confidence, 2), "confidence_label": _conf_label(confidence),
                "why": (f"A persistent {trend_txt}shift favours the {favored} side, but there is NO "
                        f"pre-authored variant for that side aboard — you're off the playbook."),
                "consider": (f"The breeze has committed {favored} and the plan has no branch for it — "
                             "sail your own best angle to that side and flag it (onboard re-optimize "
                             "is the next tier, not yet automatic)."),
                "clears": "the shift reverses", "based": driven}

    # ---- persistent shift CONFIRMS the side we're on → hold, confirmed -------------------------
    if persistent and favored in ("left", "right") and favored == rec_id:
        return {**base, "action": "hold", "status": "ok", "tier": 1,
                "target_variant": rec_id, "target_label": rec_label,
                "value": f"Hold: {rec_label}", "driven_by": ["get_tactics"],
                "confidence": 0.75, "confidence_label": "high",
                "why": (f"A persistent {trend_txt}shift favours the {favored} side — exactly what the "
                        f"recommended '{rec_label}' plays. Stay committed."),
                "consider": "Commit to the gameplan side — the shift backs it.",
                "clears": "—", "based": ["get_tactics"]}

    # ---- no switch signal: is the FORECAST warning us to reassess (drift act, wind not yet shifted)? ----
    if dft_status == "act":
        return {**base, "action": "hold", "status": "watch", "tier": 1,
                "target_variant": rec_id, "target_label": rec_label,
                "value": f"Hold · reassess: {rec_label}", "driven_by": ["get_drift"],
                "confidence": 0.5, "confidence_label": "medium",
                "why": (f"No persistent on-water shift yet, so hold '{rec_label}' — but the forecast the "
                        f"plan was built on has moved materially ({dft.get('drift_dir')} "
                        f"~{round(dft.get('drift_twd_deg', 0))}°). Watch for the breeze to confirm it."),
                "consider": "Hold the gameplan but stay alert — the forecast has drifted; a real shift "
                            "may branch the plan soon.",
                "clears": "the forecast settles / a persistent shift resolves it",
                "based": ["get_drift"]}

    # ---- default: oscillating / no decisive signal → hold the start plan -----------------------
    osc = wind.get("oscillation_deg")
    sub = (f"oscillating ±{round(osc / 2)}°" if osc else "holding the plan")
    return {**base, "action": "hold", "status": "ok", "tier": 1,
            "target_variant": rec_id, "target_label": rec_label,
            "value": f"Hold: {rec_label}", "driven_by": ["get_tactics"] if tac.get("available") else [],
            "confidence": 0.6, "confidence_label": "medium",
            "why": (f"No persistent shift and no material forecast drift — the recommended "
                    f"'{rec_label}' stands ({sub}). Play the shifts within the band."),
            "consider": "Hold the gameplan — no branch yet.", "clears": "—",
            "based": ["get_tactics"] if tac.get("available") else []}
