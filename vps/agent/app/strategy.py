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
  * Tier-2 (the copilot, later) — the onboard LLM phrases this picture, explains the interplay, and may
    ORIGINATE a suggestion beyond the frozen playbook. Legal in-race: it's the boat's own gear (see
    docs/RRS41_COMPLIANCE.md §4). This Tier-1 brief is also the copilot's deterministic FALLBACK.

Source-agnostic: pure composition of `selector` + `fleet` (both already on the 9.0 datasource seam), so
the identical synthesis runs cloud or onboard. It re-fetches nothing with Schmitt state — `selector`
does the single gather (its `signals` carry the enriched reads); `fleet` is stateless.
"""
from shared import windphrase as wp
from . import fleet as fleet_mod, reoptimize as reoptimize_mod, selector as selector_mod, tactics as tactics_mod

_OPP = {"left": "right", "right": "left"}


def _conf_label(c):
    return "high" if c >= 0.7 else "med" if c >= 0.45 else "low"


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


def _concordance(lean, sig, fleet_side):
    """Given the primary LEAN (the side the shift proposes, or None), score how the OTHER directional
    reads line up with it. Returns a dict {agree, lean, strength, concur, conflict, note}. Pure math —
    each signal is a vote for/against the lean; strength = strong (>=2 concur, 0 conflict) / weak /
    split (any conflict)."""
    votes = []   # (name, side)
    dft = sig.get("drift") or {}
    dev = sig.get("deviation") or {}
    if dft.get("status") in ("watch", "act"):
        votes.append(("forecast", dft.get("favored")))   # point-of-sail-aware, set by the selector
    if dev.get("status") in ("watch", "act"):
        votes.append(("deviation", dev.get("side")))
    if fleet_side:
        votes.append(("fleet", fleet_side))

    if lean not in ("left", "right"):
        # no on-water lean — do the reinforcing signals themselves cluster to a side?
        sides = [s for _, s in votes if s in ("left", "right")]
        if sides and all(s == sides[0] for s in sides) and len(sides) >= 2:
            return {"agree": True, "lean": sides[0], "strength": "weak",
                    "concur": len(sides), "conflict": 0,
                    "note": f"no persistent shift yet, but {len(sides)} signals lean {sides[0]}"}
        return {"agree": None, "lean": None, "strength": "none", "concur": 0, "conflict": 0,
                "note": "no decisive directional signal"}

    concur = [n for n, s in votes if s == lean]
    conflict = [n for n, s in votes if s == _OPP.get(lean)]
    if conflict:
        strength = "split"
        note = (f"the shift leans {lean}, but " + " and ".join(conflict) + " point the other way — "
                "one read is about to be wrong")
    elif len(concur) >= 2:
        strength = "strong"
        note = f"the shift, {concur[0]} and {concur[1]} all point {lean}"
    elif concur:
        strength = "weak"
        note = f"the shift leans {lean}, {concur[0]} agrees"
    else:
        strength = "weak"
        note = f"the shift leans {lean}; no other signal has confirmed it yet"
    return {"agree": not conflict, "lean": lean, "strength": strength,
            "concur": len(concur), "conflict": len(conflict), "note": note}


def _picture(sig, flt, conc):
    """The grounded higher-order reads, each citing the tool it came from (so the LLM layer inherits
    the grounding for free)."""
    out = []
    sh = sig.get("shift") or {}
    if sh.get("favored_side") or sh.get("persistent") is not None:
        read = wp.describe_shift(sh.get("base_twd") or 0, sh.get("now_twd") or 0,
                                 tack=sh.get("tack"), pos=sh.get("pos"),
                                 persistent=bool(sh.get("persistent")),
                                 oscillation_deg=sh.get("oscillation_deg"))
        out.append({"signal": "shift", "read": read, "grounded_in": ["get_tactics"],
                    "confidence": "high" if sh.get("persistent") else "med"})

    dft = sig.get("drift") or {}
    if dft.get("status") in ("watch", "act"):
        if dft.get("ref_twd") is not None and dft.get("now_twd") is not None:
            read = wp.describe_drift(dft["ref_twd"], dft["now_twd"],
                                     tws_change_kn=dft.get("tws_kn"), pos=dft.get("pos"))
        else:   # fallback if the from→to pair isn't carried (e.g. an older signal)
            read = f"forecast has shifted {dft.get('dir', '')} ~{round(dft.get('deg') or 0)}° since the plan was frozen"
        out.append({"signal": "forecast", "read": read, "grounded_in": ["get_drift"],
                    "confidence": "med" if dft.get("status") == "act" else "low"})

    dev = sig.get("deviation") or {}
    if dev.get("status") in ("watch", "act"):
        behind = dev.get("time_behind_s")
        btxt = f", {round(behind / 60)} min behind the plan" if behind and behind > 0 else ""
        out.append({"signal": "deviation",
                    "read": f"{abs(dev.get('xte_nm') or 0)} nm {dev.get('side')} of the plan's "
                            f"line{btxt}",
                    "grounded_in": ["get_deviation"],
                    "confidence": "med" if dev.get("status") == "act" else "low"})

    def _fleet_position(row):
        """Where a rival is RELATIVE to us — cross-track side + on-water ahead/behind."""
        lev, lead = row.get("leverage_nm"), row.get("on_water_lead_nm")
        bits = []
        if lev:
            bits.append(f"{abs(round(lev, 1))} nm to our {'right' if lev > 0 else 'left'}")
        if lead:
            bits.append("ahead" if lead > 0 else "behind")
        return f" — {' and '.join(bits)}, " if bits else " "

    if flt.get("available") and (flt.get("fleet") or []):
        top = flt["fleet"][0]
        d = top.get("corrected_delta_s")
        if d is not None:
            m, s = divmod(abs(int(d)), 60)
            who = "beating us" if d < 0 else "behind us"
            out.append({"signal": "fleet",
                        "read": f"{top.get('boat', 'top rival')}{_fleet_position(top)}projected {who} "
                                f"by {m}:{s:02d} on corrected time",
                        "grounded_in": ["get_fleet"],
                        "confidence": _conf_label(top.get("confidence") or 0.4)})

    if conc.get("strength") not in (None, "none"):
        out.append({"signal": "concordance", "read": conc["note"],
                    "grounded_in": ["get_selector", "get_tactics"],
                    "confidence": {"strong": "high", "weak": "low", "split": "med"}.get(conc["strength"], "low")})
    return out


def _caveats(sig, flt, has_playbook):
    """Engine-authored uncertainty — each contributing stream's honest caveat (never LLM free-text)."""
    cav = []
    if not has_playbook:
        cav.append("No gameplan aboard — this reasons from live facts only, with nothing to check it "
                   "against; treat as low-confidence.")
    if (sig.get("drift") or {}).get("status") in ("watch", "act"):
        cav.append("Forecast drift is a forecast vs a forecast — an early warning, not an on-water fact.")
    if flt.get("available"):
        cav.append("Fleet corrected-time is a projection to the finish and AIS/tracker coverage is "
                   "partial — a soft, confidence-flagged signal.")
    return cav


def _recommendation(sel, conc, flt):
    """Fold the selector's HOLD/SWITCH/OFF-SCRIPT backbone + the concordance + the fleet threat into
    one recommendation. Deterministic — the LLM layer may later re-word or extend it."""
    action = sel.get("action")
    conf = sel.get("confidence") or 0.5
    strength = conc.get("strength")
    threat = bool(flt.get("available") and (flt.get("fleet") or [])
                  and (flt["fleet"][0].get("corrected_delta_s") or 0) < 0)   # a rival beating us

    if action == "switch":
        urg = "now" if sel.get("status") == "act" else "soon"
        rat = sel.get("why", "")
        if strength == "strong":
            conf = min(0.95, conf + 0.05); rat += " The forecast, fleet and your position agree — high concordance."
        elif strength == "split":
            conf = max(0.4, conf - 0.1); urg = "soon"; rat += " But the signals are split — confirm before you commit."
        return {"action": f"Switch → {sel.get('target_label')}", "vs_playbook": "on-plan",
                "target_variant": sel.get("target_variant"), "rationale": rat,
                "grounded_in": sel.get("driven_by", []) + ["get_selector"],
                "urgency": urg, "confidence": _conf_label(conf)}, conf

    if action == "off_script":
        return {"action": f"Off-book: {sel.get('value', 'sail the favoured side')}",
                "vs_playbook": "departs", "target_variant": None,
                "rationale": sel.get("why", "") + " No pre-authored branch for this side — the onboard "
                             "re-route (GET /reoptimize) is the fallback.",
                "grounded_in": sel.get("driven_by", []) + ["get_selector"],
                "urgency": "now" if sel.get("status") == "act" else "soon",
                "confidence": _conf_label(min(0.7, conf))}, min(0.7, conf)

    # HOLD (ok or watch/reassess)
    rat = sel.get("why", "")
    urg = "monitor"
    if sel.get("status") == "watch":
        urg = "soon"
    if threat:
        rat += (" A rival is projected ahead on corrected time — if you have leverage to play, this is "
                "where to press.")
        urg = "soon"
    return {"action": f"Hold: {sel.get('recommended_label') or 'the plan'}", "vs_playbook": "on-plan",
            "target_variant": sel.get("target_variant") or sel.get("recommended"),
            "rationale": rat, "grounded_in": (sel.get("driven_by") or []) + ["get_selector"],
            "urgency": urg, "confidence": _conf_label(conf)}, conf


def _no_playbook_recommendation(sig, flt):
    """No frozen gameplan aboard (practice, or a race with no loaded playbook) — there's nothing to
    HOLD or SWITCH within, but the boat's own wind read still guides. Lead with the favoured side when
    the shift is persistent; else sail the phase / reason from the fleet. `vs_playbook: "no-plan"` so
    the card knows this isn't a playbook departure. Grounded in the tools that actually spoke."""
    sh = sig.get("shift") or {}
    fav = sh.get("favored_side")
    if sh.get("persistent") and fav in ("left", "right"):
        return ({"action": f"Work the {fav} — favoured, but no gameplan aboard to branch within",
                 "vs_playbook": "no-plan", "target_variant": None,
                 "rationale": (f"A persistent shift favours the {fav} side. No playbook is loaded, so "
                               "there's no frozen plan to hold or switch — sail your own best read to "
                               "that side."),
                 "grounded_in": ["get_tactics"], "urgency": "soon", "confidence": "low"}, 0.4)
    if "shift" in sig:
        return ({"action": "Sail your phase — oscillating, no gameplan aboard",
                 "vs_playbook": "no-plan", "target_variant": None,
                 "rationale": ("The breeze is oscillating with no persistent shift and no playbook is "
                               "loaded — work the shifts on their merits."),
                 "grounded_in": ["get_tactics"], "urgency": "monitor", "confidence": "low"}, 0.35)
    return ({"action": "Reason from live facts — no gameplan aboard to switch within",
             "vs_playbook": "no-plan", "target_variant": None,
             "rationale": ("No playbook loaded; the fleet/handicap picture is shown but there is no "
                           "frozen plan to hold or branch."),
             "grounded_in": ["get_fleet"], "urgency": "monitor", "confidence": "low"}, 0.35)


def _reoptimize_offer(route=None):
    """OFF-BOOK CHAINING (docs/STRATEGY_SYNTHESIS.md Phase 3). When the synthesis departs the frozen
    playbook, hand the crew a CONCRETE fresh route — not just "go off-book". Chains the already-built
    onboard RE-OPTIMIZER (`GET /reoptimize`): a fresh isochrone through the remaining marks on the
    boat's own polars + the common Open-Meteo forecast, avoiding the frozen island/zone homework —
    legal in-race, flagged off-playbook. Compact: strips the heavy `path`/`legs` arrays (the card
    fetches the full track from /reoptimize on demand); keeps eta/tacks/sail-plan/divergence so the
    card + coach can offer it inline. `available:False` when there's no fix / no course to route."""
    ro = reoptimize_mod.get_reoptimize(route)
    if not ro.get("available"):
        return {"available": False, "note": ro.get("note", "no onboard re-route available")}
    return {k: v for k, v in ro.items() if k not in ("path", "legs")}


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
                "assessment": "No gameplan aboard and no fleet/signal data — nothing to synthesise yet.",
                "picture": [], "concordance": {"strength": "none"}, "recommendation": None,
                "caveats": ["Load a playbook (POST /playbook/load) and/or a fleet roster "
                            "(POST /fleet/load) to enable strategy synthesis."],
                "confidence": "low", "disclaimer": "Advisory. The crew decides."}

    lean = (sig.get("shift") or {}).get("favored_side")
    fleet_side, fleet_strength, n_threats = _fleet_lean(flt)
    conc = _concordance(lean if lean in ("left", "right") else None, sig, fleet_side)
    picture = _picture(sig, flt, conc)

    if has_playbook:
        rec, conf = _recommendation(sel, conc, flt)
    else:
        rec, conf = _no_playbook_recommendation(sig, flt)

    # assessment headline: lead with the concordance verdict when there is one.
    if conc.get("strength") == "strong":
        assessment = f"Signals converging {conc['lean']} — {rec['action'].lower()}."
    elif conc.get("strength") == "split":
        assessment = f"Signals split — {rec['action'].lower()}; one read is about to be wrong."
    elif not has_playbook:
        sh = sig.get("shift") or {}
        fav = sh.get("favored_side")
        if sh.get("persistent") and fav in ("left", "right"):
            assessment = (f"No gameplan aboard — but the breeze favours the {fav}; "
                          "sail your own read to that side.")
        else:
            assessment = ("No gameplan aboard — reasoning from the live "
                          + ("wind + fleet" if flt.get("available") else "wind")
                          + " picture only.")
    else:
        extra = sel.get("value", "") or ""
        assessment = (rec["action"] if not extra or extra.lower() in rec["action"].lower()
                      else f"{rec['action']} — {extra}")

    conf = min(conf, 0.7 if not has_playbook else 0.95)
    if len(picture) <= 1:
        conf = min(conf, 0.5)      # thin picture → cap confidence honestly

    out = {"available": True, "mode": "deterministic", "assessment": assessment,
           "picture": picture, "concordance": conc, "recommendation": rec,
           "caveats": _caveats(sig, flt, has_playbook),
           "confidence": _conf_label(conf), "confidence_value": round(conf, 2),
           "recommended_label": sel.get("recommended_label"),
           "disclaimer": "Advisory — the boat's own read of its own data + common public info. "
                         "The crew decides."}

    # OFF-BOOK CHAINING: a recommendation that DEPARTS the frozen playbook needs a concrete route,
    # not just "sail your own side". Chain the onboard re-optimizer only when off-book (it's a heavy
    # isochrone — cached, but never run on an on-plan hold).
    if rec and rec.get("vs_playbook") == "departs":
        offer = _reoptimize_offer(route)
        out["reoptimize"] = offer
        if offer.get("available"):
            rec["reoptimize"] = "ready"
            if offer.get("eta_min") is not None:
                h, m = divmod(int(round(offer["eta_min"])), 60)
                eta = (f"{h}h {m:02d}m" if h else f"{m}m")
                tk = f", {offer['tacks']} tacks" if offer.get("tacks") is not None else ""
                rec["rationale"] = (rec.get("rationale", "").rstrip()
                                    + f" A fresh onboard re-route is ready (~{eta}{tk}).").strip()
    return out
