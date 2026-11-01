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
                    "note": f"no lasting shift yet, but {len(sides)} signals lean {sides[0]}"}
        return {"agree": None, "lean": None, "strength": "none", "concur": 0, "conflict": 0,
                "note": "no clear side yet"}

    concur = [n for n, s in votes if s == lean]
    conflict = [n for n, s in votes if s == _OPP.get(lean)]
    if conflict:
        strength = "split"
        note = f"shift says {lean}, but " + " and ".join(conflict) + " say the other way"
    elif len(concur) >= 2:
        strength = "strong"
        note = f"shift, {concur[0]} and {concur[1]} all say {lean}"
    elif concur:
        strength = "weak"
        note = f"shift says {lean}, {concur[0]} agrees"
    else:
        strength = "weak"
        note = f"shift says {lean}, nothing else confirms yet"
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
            read = f"forecast shifted {dft.get('dir', '')} ~{round(dft.get('deg') or 0)}° since the plan"
        out.append({"signal": "forecast", "read": read, "grounded_in": ["get_drift"],
                    "confidence": "med" if dft.get("status") == "act" else "low"})

    dev = sig.get("deviation") or {}
    if dev.get("status") in ("watch", "act"):
        behind = dev.get("time_behind_s")
        btxt = f", {round(behind / 60)} min behind plan" if behind and behind > 0 else ""
        out.append({"signal": "deviation",
                    "read": f"{abs(dev.get('xte_nm') or 0)} nm {dev.get('side')} of the plan{btxt}",
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
                                f"by {m}:{s:02d} corrected",
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
        cav.append("No gameplan loaded. Live data only, low confidence.")
    if (sig.get("drift") or {}).get("status") in ("watch", "act"):
        cav.append("Forecast change is an early warning, not what's on the water.")
    if flt.get("available"):
        cav.append("Fleet times are projections and tracking is patchy. Treat as soft.")
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
            conf = min(0.95, conf + 0.05); rat += " Forecast, fleet and position all agree."
        elif strength == "split":
            conf = max(0.4, conf - 0.1); urg = "soon"; rat += " Signals are split. Confirm before you commit."
        return {"action": f"Switch → {sel.get('target_label')}", "vs_playbook": "on-plan",
                "target_variant": sel.get("target_variant"), "rationale": rat,
                "grounded_in": sel.get("driven_by", []) + ["get_selector"],
                "urgency": urg, "confidence": _conf_label(conf)}, conf

    if action == "off_script":
        return {"action": f"Off-book: {sel.get('value', 'sail the favoured side')}",
                "vs_playbook": "departs", "target_variant": None,
                "rationale": sel.get("why", "") + " No plan for this side.",
                "grounded_in": sel.get("driven_by", []) + ["get_selector"],
                "urgency": "now" if sel.get("status") == "act" else "soon",
                "confidence": _conf_label(min(0.7, conf))}, min(0.7, conf)

    # HOLD (ok or watch/reassess)
    rat = sel.get("why", "")
    urg = "monitor"
    if sel.get("status") == "watch":
        urg = "soon"
    if threat:
        rat += " A rival is ahead on corrected time. Press if you have leverage."
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
        return {"available": False, "note": ro.get("note", "No new route available.")}
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

    # assessment headline: lead with the concordance verdict when there is one.
    if conc.get("strength") == "strong":
        assessment = f"Signals agree {conc['lean']}. {rec['action']}"
    elif conc.get("strength") == "split":
        assessment = f"Signals split. {rec['action']}"
    elif not has_playbook:
        sh = sig.get("shift") or {}
        fav = sh.get("favored_side")
        if sh.get("persistent") and fav in ("left", "right"):
            assessment = f"No gameplan loaded. Wind favours the {fav}."
        else:
            assessment = ("No gameplan loaded. Using live "
                          + ("wind and fleet" if flt.get("available") else "wind")
                          + " only.")
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
           "disclaimer": "Advice only. The crew decides."}

    # UP-COURSE LEADING INDICATOR — a buoy ahead reporting a material difference from the boat's
    # own wind joins the picture (live common-data observation, the oldest leading indicator)
    try:
        from . import buoys as buoys_mod
        upc = (buoys_mod.get_buoys(route) or {}).get("upcourse") or {}
        dtws, dtwd = upc.get("tws_delta_kn"), upc.get("twd_shift_deg")
        if upc and ((abs(dtws or 0) >= 3) or (abs(dtwd or 0) >= 15)):
            bits = []
            if abs(dtws or 0) >= 3:
                bits.append(f"{abs(dtws):g} kn {'MORE' if dtws > 0 else 'LESS'} pressure")
            if abs(dtwd or 0) >= 15:
                bits.append(f"breeze {abs(dtwd):g}° {'right' if dtwd > 0 else 'left'} of here")
            picture.append({"signal": "buoys",
                            "read": (f"Up-course buoy {upc.get('name') or upc.get('station')} "
                                     f"({upc.get('range_nm')} nm ahead): " + ", ".join(bits)
                                     + f" ({upc.get('age_min')} min old)"),
                            "grounded_in": "get_buoys", "confidence": "engine"})
    except Exception:
        pass

    # PLAN GAP — own observed wind vs the promise the plan was built on (drift stays quiet when
    # the real wind busts before the forecast catches up; this read doesn't)
    try:
        from . import plangap as plangap_mod
        pg = plangap_mod.get_plangap(route)
        if pg.get("available") and pg.get("status") in ("watch", "act"):
            picture.append({"signal": "plan_gap",
                            "read": (f"Plan said {pg['plan_tws_kn']:g} kn at "
                                     f"{pg['plan_twd']}°, you have "
                                     f"{pg['obs_tws_kn']:g} kn at {pg['obs_twd']}°"),
                            "grounded_in": ["get_plangap"],
                            "confidence": "med" if pg["status"] == "act" else "low"})
    except Exception:
        pass

    # WIND TREND — a material build/fade or a walking breeze joins the picture (own archive)
    try:
        from . import trend as trend_mod
        tr = trend_mod.get_trend(route)
        if (tr.get("available") and tr.get("read")
                and (abs(tr.get("tws_trend_kn_per_hr") or 0) >= 1.0
                     or abs(tr.get("twd_trend_deg_per_hr") or 0) >= 5.0)):
            picture.append({"signal": "trend", "read": tr["read"][0].upper() + tr["read"][1:],
                            "grounded_in": ["get_trend"], "confidence": "engine"})
    except Exception:
        pass

    # PLAYBOOK v2 Phase D — the ARMED PLAYS ride on the digest (deterministic matcher; the
    # recommendation above stays the selector's — an armed play is a pointer, not an order),
    # plus the WATCHLIST: quiet plays measurably close to arming ("what flips the plan")
    try:
        from . import matcher
        pl = matcher.get_plays(route)
        if pl.get("available") and (pl.get("armed") or pl.get("arming")):
            out["plays"] = {"armed": pl["armed"], "arming": pl["arming"],
                            "detail": [{k: r.get(k) for k in
                                        ("id", "name", "status", "guidance", "response_type",
                                         "stakes_min", "summary", "corroborated",
                                         "corroborated_by")}
                                       for r in pl["plays"] if r["status"] != "quiet"][:6]}
            if pl["armed"]:
                names = [r["name"] for r in pl["plays"] if r["status"] == "armed"][:3]
                picture.append({"signal": "plays",
                                "read": "Armed play(s): " + "; ".join(names),
                                "grounded_in": "get_plays", "confidence": "engine"})
        if pl.get("available") and pl.get("watchlist"):
            out.setdefault("plays", {})["watchlist"] = pl["watchlist"]
    except Exception:
        pass

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
                                    + f" New route ready (~{eta}{tk}).").strip()
    return out
