"""Branch SELECTOR — the graceful-degradation decision at the heart of the onboard executor.

Lab-3: the two branch triggers (route-deviation `deviation.py`, forecast-drift `drift.py`) plus the
engine's on-water tactical read (a persistent wind shift, `tactics.py`) each say "something's up".
This module UNIFIES them into a single crew-facing recommendation over the FROZEN playbook:

    HOLD the recommended variant  ·  SWITCH to a pre-authored variant  ·  OFF-SCRIPT (no branch aboard)

This deterministic layer picks a pre-authored variant — the switch target is always one of the
bundle's own variants (the copilot above it only narrates and condition-matches; it never originates
strategy — descope 2026-07-06, docs/PLAYBOOK_V2.md §7), and the rationale is the bundle's own
`what_flips_it`. This is the Tier-1 (Pi engine, no Orin) generalization of the copilot's wind-shift-only
`adherence.py` tile — deterministic, legal in-race (own instruments + pre-loaded homework + common data).

GRACEFUL DEGRADATION (perflab item-2): (1) a persistent shift favours a side that HAS a pre-authored
variant → recommend it, with rich frozen rationale; (2) the favoured side has NO variant aboard →
OFF-SCRIPT flag ("sail your own to that side, off the playbook" — the onboard re-optimizer is a later
tier); (3) nothing decisive → HOLD. Forecast-drift and route-deviation don't trigger a switch on their
own (drift is a forecast, deviation is an execution gap) — they REINFORCE a wind-shift switch (raising
confidence/urgency) and, on their own, raise a "reassess" caution. FUZZY (perflab §5): confidence is a
first-class output from how many signals concur.

⚠️ This docstring used to claim "the wind trigger keeps the engine's own persistence hysteresis so
it doesn't flip-flop". **There is no such hysteresis.** `tactics.get_tactics` decides with a bare
`abs(slope) * span > max(3, osc * 0.6)`, and on Jul 18 2026 that quantity sat within ±10% of its
own threshold for 13% of the race and flipped **74 times** — which flipped this tile 40 times, 20
separate excursions, half of them a minute or less. A documented guarantee that was never
implemented is the shape this project keeps meeting; SETTLE_S below is the thing that actually
does it, and it does it here, where the crew reads it.
"""
import os
import time

from shared import windphrase as wp
from . import deviation, drift as drift_mod, tactics

# PLAYBOOK_V2 locked input #5 — pivot hygiene is point-of-sail-aware: a lateral SWITCH downwind
# must be CONFIRMED longer than upwind (the 2025 retro: lateral pivots bought little in the
# runner; the 2025 known-answer backtest: 13/15 wrong-side switch calls were short-lived downwind
# excursions, median ~40 min). Upwind keeps the current fire-on-persistent behavior; downwind the
# decisive condition must HOLD this long before the verdict escalates from a reassess to a SWITCH.
#
# 🔴 **60 min was unreachable aboard, and lowering it has a measured price. Read both numbers.**
# On Jul 18 2026 the decisive condition never held longer than 7.5 min, so this branch had never
# fired and could not. But the 60 was not wrong where it was SET: `backtest_replay.py` runs
# tactics' own formula over a **180-minute** window of smooth analysis wind, while the boat runs
# it over **12 minutes** of anemometer. A trend lasts hours in the first and minutes in the
# second. **The bar was calibrated in one timescale and applied in another** — the real defect,
# and the real fix is the detector, not this number.
#
# Lowered to 1200 s on Cole's instruction (2026-09-17: "forgive dropouts and lower the bar"),
# with the cost measured on the 2025 known-answer race, where RIGHT paid 18:2:
#
#     bar      wrong-side time (winner / 88th)
#     3600 s   7% / 12%      <- what locked input #5 bought (from 17% / 21% unprotected)
#     1800 s   12% / 18%
#     1200 s   12% / 18%     <- here
#      600 s   11% / 18%
#
# So this gives back roughly half of #5's gain on that race. 1200 s is chosen because every
# lowered value costs the same there, and 1200 is the LARGEST that is actually reachable on the
# boat's own signal (Jul 18's longest run with the grace below is 24.5 min) — the least damage
# that still makes the branch exist. ⚠️ Revisit with a second real race, or by widening the
# onboard tactics window so the two timescales agree.
SWITCH_CONFIRM_DOWNWIND_S = float(os.environ.get("SEL_SWITCH_CONFIRM_DOWNWIND_S", "1200"))
# How long the decisive condition may LAPSE without resetting the confirmation clock. Cole,
# 2026-09-17: "forgive dropouts and lower the bar."
#
# Why it was needed: the clock used to clear-fast on any dropout, and the signal it is timing
# comes from `tactics.get_tactics`, whose persistence test is a bare threshold that flipped 74
# times on Jul 18 2026. With no grace, the longest the condition ever held on that race was
# **7.5 minutes**; forgiving a 3-minute lapse it reaches 19.0, and a 5-minute lapse 24.5.
# The clock keeps RUNNING through a forgiven lapse — a dropout shorter than the grace is treated
# as noise in the detector, not as the shift going away, which is exactly what it is.
SWITCH_GRACE_S = float(os.environ.get("SEL_SWITCH_GRACE_S", "300"))
_CONFIRM = {}      # (route, favored) -> {"first": epoch, "lapsed": epoch|None}

# How long a CHANGED verdict must hold before the crew is shown it. Measured on the Jul 18 2026
# full-race timeline: the tile changed state 40 times in 9 hours, in 20 excursions, and TEN of
# those lasted a minute or less — a recommendation that appears and withdraws inside one tack is
# worse than no recommendation, because it teaches the crew to stop reading the card.
#
# 2 minutes is where the measured curve turns: 40 flips -> 26 at 1 min, -> 12 at 2 min, and only
# -> 6 by 5 min. It also removes the `watch` state from the race entirely; every `watch` on Jul 18
# was a sub-2-minute excursion. The cost is up to 2 minutes of delay on a genuine SWITCH call,
# which is nothing against a branch the playbook already makes you confirm for an hour downwind.
#
# The tile never lies about it: while a change is settling it carries a `settling` block saying
# what it is moving to and how far through the wait it is, the same way the downwind confirmation
# reports "Held 12 of 60 min".
SETTLE_S = float(os.environ.get("SEL_SETTLE_S", "120"))
_SETTLE = {}       # route -> {key, shown, cand, cand_since}


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


def _rows(bundle, rec_id, favored):
    """The per-variant agreement table for the dashboard PLAYBOOK tile — recommended starred (start),
    the currently-favoured side flagged 'now'. Same shape the copilot's adherence tile emitted, so the
    tile renders identically from the (Tier-1) selector without needing the Orin."""
    out = [{"hdr": True, "cols": ["agree", ""]}]
    for v in (bundle.get("variants") or [])[:4]:
        vid = str(v.get("id") or "")
        share = v.get("share")
        shp = f"{round(share * 100)}%" if isinstance(share, (int, float)) else "—"
        tags = []
        if vid == rec_id:
            tags.append("start")
        if favored in ("left", "right") and vid == favored:
            tags.append("now")
        out.append({"label": ("★ " if vid == rec_id else "") + (v.get("name") or vid),
                    "emph": bool(favored in ("left", "right") and vid == favored),
                    "cols": [shp, " · ".join(tags)]})
    return out


def _na(note):
    return {"available": False, "action": "na", "status": "na", "value": "—", "why": note,
            "consider": "—", "based": [], "conf": "engine"}


# compass-shift direction of the forecast drift -> signed rotation (+1 = shifted RIGHT/clockwise).
# The favoured SIDE it implies is point-of-sail aware (wp.favored_side), computed against the leg.
_DRIFT_SIGN = {"right": 1, "left": -1, "veered": 1, "backed": -1}


def get_selector(route=None, now=None):
    """The unified branch recommendation, SETTLED — what the crew is actually shown.

    `_decide` is the pure verdict for this instant; this wrapper is the only thing that makes the
    card stable enough to read. A verdict that differs from what is on screen must hold for
    SETTLE_S before it replaces it, and while it waits the card says so in `settling`. Both
    directions settle, because the churn runs both ways: most of Jul 18's excursions were an
    alarm that appeared and withdrew inside a minute.

    State lives here, keyed by route, next to `_CONFIRM`, which this module has always held.
    Clear both to reset a scenario (`test_selector.py` does).

    `now` is injectable for replay/backtest; live callers leave it None (wall clock)."""
    now = float(now) if now is not None else time.time()
    out = _decide(route, now)
    if not out.get("available", True) or out.get("status") == "na":
        _SETTLE.pop(route, None)          # nothing to steady — don't hold a verdict over a gap
        return out
    key = (out.get("status"), out.get("action"), out.get("target_variant"))
    st = _SETTLE.get(route)
    if st is None or st["key"] == key:    # agrees with the card: refresh it, drop any candidate
        _SETTLE[route] = {"key": key, "shown": out, "cand": None, "cand_since": now}
        return out
    # A change the logic BELOW has already timed does not need timing again: when the downwind
    # confirmation completes it has held the decisive condition for an hour, and making the crew
    # wait another two minutes for the card to admit it would be the gate arguing with itself.
    if st["shown"].get("confirming") and out.get("action") == "switch":
        _SETTLE[route] = {"key": key, "shown": out, "cand": None, "cand_since": now}
        return out
    if st.get("cand") != key:             # a different change started: its own clock, from zero
        st["cand"], st["cand_since"] = key, now
    held = now - st["cand_since"]
    if held >= SETTLE_S:
        _SETTLE[route] = {"key": key, "shown": out, "cand": None, "cand_since": now}
        return out
    shown = dict(st["shown"])
    shown["settling"] = {"to_value": out.get("value"), "to_status": out.get("status"),
                         "to_action": out.get("action"), "why": out.get("why"),
                         "held_s": round(held), "need_s": SETTLE_S}
    return shown


def _decide(route=None, now=None):
    """The unified branch recommendation over the frozen playbook. Reuses the two trigger reads +
    the tactical read, so this stays a thin, deterministic decision. Pure for this instant apart
    from the downwind confirmation clock — `get_selector` is what steadies it for the crew."""
    now = float(now) if now is not None else time.time()
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
    pos = tac.get("point_of_sail", "upwind") if tac.get("available") else "upwind"

    dev_ok = dev.get("available")
    dft_ok = dft.get("available")
    dev_status = dev.get("status") if dev_ok else None
    dft_status = dft.get("status") if dft_ok else None
    # the forecast-drift's implied favoured side, POINT-OF-SAIL aware (right shift → right upwind /
    # left downwind), so its concordance with the on-water read is correct on every leg.
    drift_sign = _DRIFT_SIGN.get(dft.get("drift_dir")) if dft_ok else None
    drift_favored = wp.favored_side(drift_sign, pos) if drift_sign else None
    drift_ref = dft.get("ref_twd") if dft_ok else None
    drift_now = dft.get("now_twd") if dft_ok else None

    signals = {
        "shift": {"persistent": persistent, "favored_side": favored, "pos": pos,
                  "base_twd": wind.get("mean_12min"), "now_twd": wind.get("now"),
                  "tack": tac.get("tack") if tac.get("available") else None,
                  "oscillation_deg": wind.get("oscillation_deg")},
        "deviation": {"status": dev_status, "side": dev.get("xte_side") if dev_ok else None,
                      "variant": dev.get("variant") if dev_ok else None,
                      "xte_nm": dev.get("xte_nm") if dev_ok else None,
                      "time_behind_s": dev.get("time_behind_s") if dev_ok else None},
        "drift": {"status": dft_status, "dir": dft.get("drift_dir") if dft_ok else None,
                  "deg": dft.get("drift_twd_deg") if dft_ok else None,
                  "tws_kn": dft.get("drift_tws_kn") if dft_ok else None,
                  "ref_twd": drift_ref, "now_twd": drift_now, "pos": pos, "favored": drift_favored},
    }
    agreement = bundle.get("agreement")
    base = {"available": True, "recommended": rec_id, "recommended_label": rec_label,
            "signals": signals, "conf": "engine",
            "rows": _rows(bundle, rec_id, favored), "headline": bundle.get("headline", ""),
            "agreement_pct": (round(agreement * 100) if isinstance(agreement, (int, float)) else None),
            "decision_spread_min": bundle.get("decision_spread_min")}

    # ---- decisive path: a PERSISTENT shift favours a side other than the one we're on -----------
    decisive = persistent and favored in ("left", "right") and favored != rec_id
    if not decisive:
        # The condition is gone THIS instant — but the detector feeding it chatters, so a lapse
        # shorter than SWITCH_GRACE_S is noise, not a reversal. Mark when it lapsed and only
        # drop the clock once the lapse outlives the grace.
        for k in [k for k in list(_CONFIRM) if k[0] == route]:
            rec = _CONFIRM[k]
            if rec.get("lapsed") is None:
                rec["lapsed"] = now
            elif now - rec["lapsed"] > SWITCH_GRACE_S:
                _CONFIRM.pop(k, None)
    if decisive:
        # locked input #5 — downwind, the pivot must CONFIRM before the verdict escalates to a
        # SWITCH; a different favored side restarts its own clock (the old one is dropped
        # outright, grace or no grace — a shift that swapped sides did not merely flicker).
        key = (route, favored)
        for k in [k for k in list(_CONFIRM) if k[0] == route and k != key]:
            _CONFIRM.pop(k, None)
        rec = _CONFIRM.setdefault(key, {"first": now, "lapsed": None})
        rec["lapsed"] = None                      # back on: the forgiven lapse is over
        first = rec["first"]
        held_s = now - first
        if pos == "downwind" and held_s < SWITCH_CONFIRM_DOWNWIND_S:
            held_m, need_m = round(held_s / 60), round(SWITCH_CONFIRM_DOWNWIND_S / 60)
            return {**base, "action": "hold", "status": "watch", "tier": 0,
                    "target_variant": None, "target_label": None,
                    "value": f"Hold: {rec_label}", "driven_by": ["get_tactics"],
                    "confidence": 0.5, "confidence_label": _conf_label(0.5),
                    "why": (f"A persistent shift now favours the {favored} — but this is a DOWNWIND "
                            f"leg, and downwind pivots need longer confirmation before they pay "
                            f"(2025 retro: lateral pivots bought little in the runner). "
                            f"Held {held_m} of {need_m} min."),
                    "consider": (f"Stay on '{rec_label}' while the {favored} shift confirms — the "
                                 f"branch fires at {need_m} min sustained."),
                    "clears": "the shift reverses (clock resets), or confirmation completes",
                    "based": ["get_tactics"],
                    "confirming": {"favored": favored, "held_s": round(held_s),
                                   "need_s": SWITCH_CONFIRM_DOWNWIND_S}}
        target = _variant_for_side(bundle, favored)
        driven = ["get_tactics"]
        concur = 0
        # forecast-drift concurs if it implies the SAME favoured side (point-of-sail aware)
        if dft_status in ("watch", "act") and drift_favored == favored:
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
                extra.append(f"the forecast has shifted {dft.get('drift_dir')} ~{round(dft.get('drift_twd_deg', 0))}° the same way")
            if "get_deviation" in driven:
                extra.append(f"you're already working the {favored} side ({dev.get('xte_nm')} nm {favored})")
            why = (f"A persistent shift now favours the {favored} side — against the "
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

        # TIER 2 — favoured side has NO pre-authored variant aboard: off the playbook. The onboard
        # RE-OPTIMIZER (GET /reoptimize) is the fallback route — hint it (don't run the heavy
        # isochrone here; the card/crew fetch it on demand).
        confidence = min(0.7, confidence)
        return {**base, "action": "off_script", "status": "act", "tier": 2, "reoptimize_hint": True,
                "target_variant": None, "target_label": None,
                "value": f"Off-script: sail {favored}", "driven_by": driven,
                "confidence": round(confidence, 2), "confidence_label": _conf_label(confidence),
                "why": (f"A persistent shift favours the {favored} side, but there is NO "
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
                "why": (f"A persistent shift favours the {favored} side — exactly what the "
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
