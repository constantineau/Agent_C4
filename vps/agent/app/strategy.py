"""In-race STRATEGY SYNTHESIS — the Tier-1 deterministic cross-signal digest.

The siloed triggers each say one thing (forecast moved / we're off the line / a rival is ahead / the
breeze shifted). This module SYNTHESIZES them into a higher-order read of the OVERALL plan: are the
signals pointing the same way (consolidate) or fighting each other (hold and watch)? — and a single
grounded recommendation.

Two layers, by design (see docs/STRATEGY_SYNTHESIS.md):
  * Tier-1 (this module) — DETERMINISTIC. It reuses `selector.get_selector` (the HOLD/SWITCH/OFF-SCRIPT
    backbone, already fuzzy/hysteretic) plus the handicap `fleet` read, and computes the concordance
    (do the directional signals agree, and how strongly) with plain arithmetic. The NUMBERS and the
    judgement-of-agreement are math, not a guess — that's the reliability guardrail.
  * Tier-2 (the copilot) — the onboard LLM phrases this picture, explains the interplay, and matches
    the live conditions against the playbook's pre-authored plays. It does NOT originate strategy
    (descope 2026-07-06, docs/PLAYBOOK_V2.md §7); the recommendation here is authoritative and this
    Tier-1 brief is also the copilot's deterministic FALLBACK.

Source-agnostic: pure composition of `selector` + `fleet` (both already on the 9.0 datasource seam), so
the identical synthesis runs cloud or onboard. It re-fetches nothing with Schmitt state — `selector`
does the single gather (its `signals` carry the enriched reads); `fleet` is stateless.
"""
from shared import strategy_render as sr
from . import fleet as fleet_mod, selector as selector_mod, tactics as tactics_mod

_conf_label = sr.conf_label            # the rendering lives in shared/strategy_render.py (#54):
_concordance = sr.concordance          # the matcher-LoRA corpus generators render through the
_picture = sr.picture                  # SAME functions, so training == serving
_recommendation = sr.recommendation


def _fleet_lean(flt):
    """The threats' committed side, confidence-weighted. leverage_nm sign: + = right of course
    (fleet.py). Returns (side|None, strength 0..1, n_threats). Only rivals/ahead-on-corrected count —
    the boats we actually have to beat."""
    if not flt.get("available"):
        return None, 0.0, 0
    threats = [f for f in (flt.get("fleet") or [])
               if f.get("tag") in ("rival", "ahead_corrected") and f.get("leverage_nm") is not None]
    if not threats:
        return None, 0.0, 0
    num = sum((f["leverage_nm"]) * (f.get("confidence") or 0.5) for f in threats)
    den = sum(abs(f["leverage_nm"]) * (f.get("confidence") or 0.5) for f in threats) or 1.0
    signed = num / den                       # -1 (all left) .. +1 (all right)
    side = "right" if signed > 0.15 else "left" if signed < -0.15 else None
    return side, min(1.0, abs(signed)), len(threats)


def _caveats(sig, flt, has_playbook):
    """Engine-authored uncertainty — each contributing stream's honest caveat (never LLM free-text)."""
    cav = []
    if not has_playbook:
        cav.append("No gameplan loaded. Live data only, low confidence.")
    if (sig.get("drift") or {}).get("status") in ("watch", "act"):
        cav.append("Forecast change is an early warning, not what's on the water.")
    if flt.get("available"):
        cav.append("Fleet times are projections and tracking is patchy. Treat as soft.")
    return cav


def _no_playbook_recommendation(sig, flt):
    """No frozen gameplan aboard (practice, or a race with no loaded playbook) — there's nothing to
    HOLD or SWITCH within, but the boat's own wind read still guides. Lead with the favoured side when
    the shift is persistent; else sail the phase / reason from the fleet. `vs_playbook: "no-plan"` so
    the card knows this isn't a playbook departure. Grounded in the tools that actually spoke."""
    sh = sig.get("shift") or {}
    fav = sh.get("favored_side")
    if sh.get("persistent") and fav in ("left", "right"):
        return ({"action": f"Work the {fav}. No gameplan loaded.",
                 "vs_playbook": "no-plan", "target_variant": None,
                 "rationale": f"Lasting shift favours the {fav}. Sail your best read there.",
                 "grounded_in": ["get_tactics"], "urgency": "soon", "confidence": "low"}, 0.4)
    if "shift" in sig:
        return ({"action": "Play the shifts. No gameplan loaded.",
                 "vs_playbook": "no-plan", "target_variant": None,
                 "rationale": "Wind is oscillating. Play each shift.",
                 "grounded_in": ["get_tactics"], "urgency": "monitor", "confidence": "low"}, 0.35)
    return ({"action": "Sail on live data. No gameplan loaded.",
             "vs_playbook": "no-plan", "target_variant": None,
             "rationale": "No gameplan loaded. Fleet picture shown only.",
             "grounded_in": ["get_fleet"], "urgency": "monitor", "confidence": "low"}, 0.35)


_SIDE = {"right": "right", "left": "left", "veered": "right", "backed": "left"}


def _reroute_suggestion(rec, conc, sig, plan_gap):
    """Should the crew think about a re-route? Quiet text for the Route tile (#125), never an
    alarm and never a computation: routes are made only when a crew member presses Re-route now.
    One short reason (12 words max), the strongest first:
      * the recommendation departs the playbook (the selector's off-script),
      * our own wind has left the plan's promise (plan gap at act),
      * the forecast has moved off the plan's (drift at act).
    None when nothing says so."""
    if (rec or {}).get("vs_playbook") == "departs":
        side = conc.get("lean") or (sig.get("shift") or {}).get("favored_side")
        reason = (f"Off the playbook. The {side} is paying." if side in ("left", "right")
                  else "Off the playbook.")
        return {"reason": reason, "why": "off_playbook"}
    pg = plan_gap or {}
    if pg.get("status") == "act" and pg.get("obs_twd") is not None and pg.get("plan_twd") is not None:
        d = ((pg["obs_twd"] - pg["plan_twd"] + 540) % 360) - 180      # + = our wind right of the plan
        if abs(d) >= 10:
            return {"reason": f"Wind shifted {round(abs(d))}° {'right' if d > 0 else 'left'} of the plan.",
                    "why": "plan_gap"}
        dtws = (pg.get("obs_tws_kn") or 0) - (pg.get("plan_tws_kn") or 0)
        if abs(dtws) >= 3:
            return {"reason": f"Wind {round(abs(dtws))} kts {'more' if dtws > 0 else 'less'} than the plan.",
                    "why": "plan_gap"}
    dft = sig.get("drift") or {}
    side = _SIDE.get(dft.get("dir") or "")
    if dft.get("status") == "act" and side and dft.get("deg") is not None:
        return {"reason": f"Forecast shifted {round(abs(dft['deg']))}° {side} since the plan.",
                "why": "forecast"}
    return None


def get_strategy_signals(route=None):
    """The Tier-1 deterministic StrategyBrief: assessment + grounded picture + concordance + one
    recommendation. `available` False only when there's genuinely nothing to say (no fix / no signals);
    with no playbook it still reasons from the live signals, flagged low-confidence."""
    sel = selector_mod.get_selector(route)
    flt = fleet_mod.get_fleet()

    has_playbook = sel.get("available", False)
    sig = dict(sel.get("signals") or {})
    # With no playbook the selector is `na` and carries no signals — but the boat's own tactical read
    # (favoured side, persistent vs oscillating) still matters and used to have its own Tactics tile.
    # Pull it DIRECTLY so the strip shows it in practice / when no gameplan is loaded — the strip must
    # not go blind without a playbook. (With a playbook the selector already carries the shift.)
    if not has_playbook:
        tac = tactics_mod.get_tactics(route)
        if tac.get("available"):
            w = tac.get("wind") or {}
            sig["shift"] = {"persistent": bool(w.get("persistent")),
                            "favored_side": tac.get("favored_side"),
                            "pos": tac.get("point_of_sail", "upwind"),
                            "base_twd": w.get("mean_12min"), "now_twd": w.get("now"),
                            "tack": tac.get("tack"), "oscillation_deg": w.get("oscillation_deg")}
    have_shift = "shift" in sig
    # Nothing to synthesise only when there's genuinely no signal at all (no playbook, no fleet, no
    # tactical read).
    if not has_playbook and not flt.get("available") and not have_shift:
        return {"available": False, "mode": "deterministic",
                "assessment": "No gameplan, fleet or wind data yet.",
                "picture": [], "concordance": {"strength": "none"}, "recommendation": None,
                "caveats": ["Load a gameplan or fleet list to get a strategy read."],
                "confidence": "low", "disclaimer": "Advisory. The crew decides."}

    lean = (sig.get("shift") or {}).get("favored_side")
    fleet_side, fleet_strength, n_threats = _fleet_lean(flt)
    conc = _concordance(lean if lean in ("left", "right") else None, sig, fleet_side)
    picture = _picture(sig, flt, conc)

    if has_playbook:
        rec, conf = _recommendation(sel, conc, flt)
    else:
        rec, conf = _no_playbook_recommendation(sig, flt)

    assessment = sr.assessment(conc, rec, sel, sig, flt, has_playbook)

    conf = min(conf, 0.7 if not has_playbook else 0.95)
    if len(picture) <= 1:
        conf = min(conf, 0.5)      # thin picture → cap confidence honestly

    out = {"available": True, "mode": "deterministic", "assessment": assessment,
           "picture": picture, "concordance": conc, "recommendation": rec,
           "caveats": _caveats(sig, flt, has_playbook),
           "confidence": _conf_label(conf), "confidence_value": round(conf, 2),
           "recommended_label": sel.get("recommended_label"),
           "disclaimer": "Advice only. The crew decides."}

    # UP-COURSE LEADING INDICATOR — a buoy ahead reporting a material difference from the boat's
    # own wind joins the picture (live common-data observation, the oldest leading indicator)
    try:
        from . import buoys as buoys_mod
        row = sr.buoys_row((buoys_mod.get_buoys(route) or {}).get("upcourse") or {})
        if row:
            picture.append(row)
    except Exception:
        pass

    # PLAN GAP — own observed wind vs the promise the plan was built on (drift stays quiet when
    # the real wind busts before the forecast catches up; this read doesn't)
    pg = None
    try:
        from . import plangap as plangap_mod
        pg = plangap_mod.get_plangap(route)
        row = sr.plangap_row(pg)
        if row:
            picture.append(row)
    except Exception:
        pass

    # WIND TREND — a material build/fade or a walking breeze joins the picture (own archive)
    try:
        from . import trend as trend_mod
        row = sr.trend_row(trend_mod.get_trend(route))
        if row:
            picture.append(row)
    except Exception:
        pass

    # PLAYBOOK v2 Phase D — the ARMED PLAYS ride on the digest (deterministic matcher; the
    # recommendation above stays the selector's — an armed play is a pointer, not an order),
    # plus the WATCHLIST: quiet plays measurably close to arming ("what flips the plan")
    try:
        from . import matcher
        pl = matcher.get_plays(route)
        block, row = sr.plays_block(pl)
        if block:
            out["plays"] = block
        if row:
            picture.append(row)
        if pl.get("available") and pl.get("watchlist"):
            out.setdefault("plays", {})["watchlist"] = pl["watchlist"]
    except Exception:
        pass

    # RE-ROUTE SUGGESTION (#125): when the plan has run out, SAY so, quietly. Nothing is computed
    # here: a new route is made only when a crew member presses Re-route now on the iPad.
    sug = _reroute_suggestion(rec, conc, sig, pg if (pg or {}).get("available") else None)
    if sug:
        out["reroute_suggest"] = sug
    # A changed verdict waiting out the selector's settle (#40): the recommendation above is still
    # the one on screen, and this says what it is moving to. Passed through for the iPad/copilot.
    if sel.get("settling"):
        out["settling"] = sel["settling"]
    return out
