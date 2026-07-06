"""Synthetic strategy digests across the judgment space — the offline pilot data source.

The Lab-4 archive stores PERFORMANCE bins, not tactical decision states, so there's no ready corpus
of real strategy digests yet (capturing live `/strategy` reads in-race is a future logger). For the
pilot we author digests directly, weighted toward the HARD cases (signals fight → split concordance,
a rival ahead, drift-vs-shift disagreement) where judgment separates from the deterministic default.

Fidelity: the concordance / picture / recommendation logic below is COPIED VERBATIM from
`vps/agent/app/strategy.py` (its pure helpers), because that module can't be imported here — it
pulls the datasource-bound selector/fleet/tactics at import time. So a synthetic digest is what the
real Tier-1 engine WOULD produce for the given synthetic signals. (Real snapshots, when we capture
them from a live `/strategy`, use the engine directly — see gen_snapshots.py.) If strategy.py's
helpers change, re-sync the copies below.
"""
import os
import random
import sys

# Reach the repo-root `shared` package so the training corpus speaks the EXACT language the engine
# emits (shared/windphrase.py is the single source of truth). Running `python3 -m training.<x>` from
# pi/orin doesn't put the repo root on the path, so add it.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from shared import windphrase as wp   # noqa: E402

# =============================================================================================
# VERBATIM COPIES from vps/agent/app/strategy.py (pure helpers) — keep in sync.
# =============================================================================================
# compass-shift direction -> signed rotation (+1 = shifted RIGHT/clockwise, -1 = LEFT). The favoured
# SIDE is derived per-leg via wp.favored_side (point-of-sail aware) — never hardcoded here.
_DRIFT_SIGN = {"right": 1, "left": -1, "veered": 1, "backed": -1}
_OPP = {"left": "right", "right": "left"}


def _conf_label(c):
    return "high" if c >= 0.7 else "med" if c >= 0.45 else "low"


def _fleet_position(row) -> str:
    """Where a rival is RELATIVE to us — cross-track side (leverage_nm, + = our right) and on-water
    along-track (lead_nm, + = ahead). Returns ' — <side> and <ahead|behind>, ' (or a bare space)."""
    lev, lead = row.get("leverage_nm"), row.get("lead_nm")
    bits = []
    if lev:
        bits.append(f"{abs(round(lev, 1))} nm to our {'right' if lev > 0 else 'left'}")
    if lead:
        bits.append("ahead" if lead > 0 else "behind")
    return f" — {' and '.join(bits)}, " if bits else " "


def _fleet_lean(flt):
    if not flt.get("available"):
        return None, 0.0, 0
    threats = [f for f in (flt.get("fleet") or [])
               if f.get("tag") in ("rival", "ahead_corrected") and f.get("leverage_nm") is not None]
    if not threats:
        return None, 0.0, 0
    num = sum((f["leverage_nm"]) * (f.get("confidence") or 0.5) for f in threats)
    den = sum(abs(f["leverage_nm"]) * (f.get("confidence") or 0.5) for f in threats) or 1.0
    signed = num / den
    side = "right" if signed > 0.15 else "left" if signed < -0.15 else None
    return side, min(1.0, abs(signed)), len(threats)


def _concordance(lean, sig, fleet_side):
    votes = []
    dft = sig.get("drift") or {}
    dev = sig.get("deviation") or {}
    if dft.get("status") in ("watch", "act"):
        votes.append(("forecast", dft.get("favored")))   # point-of-sail-aware, set in _build_sig
    if dev.get("status") in ("watch", "act"):
        votes.append(("deviation", dev.get("side")))
    if fleet_side:
        votes.append(("fleet", fleet_side))

    if lean not in ("left", "right"):
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
    out = []
    sh = sig.get("shift") or {}
    if sh.get("favored_side") or sh.get("persistent") is not None:
        read = wp.describe_shift(sh.get("base_twd", 0), sh.get("now_twd", 0),
                                 tack=sh.get("tack"), pos=sh.get("pos"),
                                 persistent=bool(sh.get("persistent")),
                                 oscillation_deg=sh.get("oscillation_deg"))
        out.append({"signal": "shift", "read": read, "grounded_in": ["get_tactics"],
                    "confidence": "high" if sh.get("persistent") else "med"})

    dft = sig.get("drift") or {}
    if dft.get("status") in ("watch", "act"):
        read = wp.describe_drift(dft.get("ref_twd", 0), dft.get("now_twd", 0),
                                 tws_change_kn=dft.get("tws_kn"), pos=dft.get("pos"))
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

    if flt.get("available") and (flt.get("fleet") or []):
        top = flt["fleet"][0]
        d = top.get("corrected_delta_s")
        if d is not None:
            m, s = divmod(abs(int(d)), 60)
            who = "beating us" if d < 0 else "behind us"
            postxt = _fleet_position(top)
            out.append({"signal": "fleet",
                        "read": f"{top.get('boat', 'top rival')}{postxt}projected {who} by "
                                f"{m}:{s:02d} on corrected time",
                        "grounded_in": ["get_fleet"],
                        "confidence": _conf_label(top.get("confidence") or 0.4)})

    if conc.get("strength") not in (None, "none"):
        out.append({"signal": "concordance", "read": conc["note"],
                    "grounded_in": ["get_selector", "get_tactics"],
                    "confidence": {"strong": "high", "weak": "low", "split": "med"}.get(conc["strength"], "low")})
    return out


def _caveats(sig, flt, has_playbook):
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
    action = sel.get("action")
    conf = sel.get("confidence") or 0.5
    strength = conc.get("strength")
    threat = bool(flt.get("available") and (flt.get("fleet") or [])
                  and (flt["fleet"][0].get("corrected_delta_s") or 0) < 0)

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


# =============================================================================================
# SCENARIO → synthetic (sel, sig, flt) inputs, then the same assembly get_strategy_signals does.
# =============================================================================================
_SIDE_VARIANT = {"left": ("Left ladder", "v_left"), "right": ("Right side", "v_right")}
_RIVAL_NAMES = ["Bravado", "Illuminati", "Windquest", "Natalie J", "Meridian X"]


def _build_sig(sc: dict) -> dict:
    """Synthetic `signals` dict as selector/tactics would carry — now with CONCRETE wind numbers
    (baseline TWD -> now TWD, current tack) so the reads carry the from->to degrees, and with the
    favoured side derived POINT-OF-SAIL aware from the actual leg (never hardcoded)."""
    sig = {}
    i = sc.get("_i", 0)
    pos = wp.point_of_sail((sc.get("cond") or {}).get("leg"))
    base = 200 + (i * 17) % 90              # on-water baseline TWD (deterministic, varied)
    mag = 12 + (i * 5) % 8                  # 12..19° persistent shift
    tack = (sc.get("cond") or {}).get("tack") or ("port", "starboard")[i % 2]

    shift = sc.get("shift")
    if shift in ("persist_left", "persist_right"):
        sign = 1 if shift == "persist_right" else -1
        now = base + sign * mag
        sig["shift"] = {"persistent": True, "favored_side": wp.favored_side(sign, pos),
                        "base_twd": base, "now_twd": now, "tack": tack, "pos": pos,
                        "oscillation_deg": 0}
    elif shift == "osc":
        sig["shift"] = {"persistent": False, "favored_side": None, "base_twd": base,
                        "now_twd": base, "tack": tack, "pos": pos, "oscillation_deg": 16}
    elif shift == "steady":
        sig["shift"] = {"persistent": False, "favored_side": None, "base_twd": base,
                        "now_twd": base, "tack": tack, "pos": pos, "oscillation_deg": 0}

    drift = sc.get("drift")
    if drift:
        dir_, status = drift
        deg = 22 if status == "watch" else 34
        sign = _DRIFT_SIGN.get(dir_, 1)
        ref = 200 + (i * 23) % 90           # frozen forecast direction the plan rested on
        sig["drift"] = {"status": status, "deg": deg, "tws_kn": 3.0 if status == "watch" else 5.0,
                        "ref_twd": ref, "now_twd": ref + sign * deg, "pos": pos,
                        "favored": wp.favored_side(sign, pos)}
    dev = sc.get("deviation")
    if dev:
        side, status = dev
        sig["deviation"] = {"status": status, "side": side,
                            "xte_nm": 0.7 if status == "watch" else 1.4,
                            "time_behind_s": 140 if status == "watch" else 260}
    return sig


def _build_fleet(sc: dict) -> dict:
    fleet = sc.get("fleet")
    if not fleet:
        return {"available": False}
    name = _RIVAL_NAMES[sc.get("_i", 0) % len(_RIVAL_NAMES)]
    # leverage_nm = cross-track side (+ = to our right); lead_nm = on-water along-track (+ = ahead of us)
    if fleet == "rival_left":
        row = {"boat": name, "tag": "rival", "leverage_nm": -0.6, "lead_nm": 0.3, "confidence": 0.6, "corrected_delta_s": -75}
    elif fleet == "rival_right":
        row = {"boat": name, "tag": "rival", "leverage_nm": 0.6, "lead_nm": 0.3, "confidence": 0.6, "corrected_delta_s": -75}
    elif fleet == "ahead":
        row = {"boat": name, "tag": "ahead_corrected", "leverage_nm": 0.3, "lead_nm": 0.5, "confidence": 0.5, "corrected_delta_s": -120}
    else:  # behind
        row = {"boat": name, "tag": "rival", "leverage_nm": 0.2, "lead_nm": -0.4, "confidence": 0.5, "corrected_delta_s": 90}
    return {"available": True, "fleet": [row]}


def _build_sel(sc: dict, sig: dict) -> dict:
    """Synthetic selector verdict (HOLD/SWITCH/OFF-SCRIPT) as selector.get_selector would return."""
    sh = sig.get("shift") or {}
    fav = sh.get("favored_side")
    persistent = sh.get("persistent")
    fired = (sig.get("drift", {}).get("status") in ("watch", "act")
             or sig.get("deviation", {}).get("status") in ("watch", "act"))
    status = "act" if (persistent and fired) else "watch" if (persistent or fired) else "ok"
    driven = ["get_tactics"]
    if sig.get("drift", {}).get("status") in ("watch", "act"):
        driven.append("get_drift")
    if sig.get("deviation", {}).get("status") in ("watch", "act"):
        driven.append("get_deviation")

    if persistent and fav in ("left", "right") and sc.get("variant_for_side", True):
        label, vid = _SIDE_VARIANT[fav]
        return {"available": True, "action": "switch", "status": status, "confidence": 0.65,
                "why": f"A persistent shift favours the {fav}; the pre-authored {label} branch covers it.",
                "target_label": label, "target_variant": vid, "driven_by": driven,
                "recommended_label": "Middle start", "recommended": "v_mid"}
    if persistent and fav in ("left", "right"):
        return {"available": True, "action": "off_script", "status": status, "confidence": 0.55,
                "why": f"A persistent shift favours the {fav}, but no branch covers that side.",
                "value": f"sail the {fav} side", "driven_by": driven,
                "recommended_label": "Middle start", "recommended": "v_mid"}
    return {"available": True, "action": "hold", "status": status, "confidence": 0.6,
            "why": "No persistent shift has committed — hold the recommended start and keep options.",
            "target_variant": "v_mid", "driven_by": driven,
            "recommended_label": "Middle start", "recommended": "v_mid"}


def build_digest(sc: dict) -> dict:
    """Assemble the deterministic StrategyBrief for a scenario — mirrors get_strategy_signals'
    assembly (lines ~279-320) minus the network reoptimize offer."""
    has_playbook = sc.get("has_playbook", True)
    sig = _build_sig(sc)
    flt = _build_fleet(sc)
    sel = _build_sel(sc, sig) if has_playbook else {"available": False}

    lean = (sig.get("shift") or {}).get("favored_side")
    fleet_side, _fs, _n = _fleet_lean(flt)
    conc = _concordance(lean if lean in ("left", "right") else None, sig, fleet_side)
    picture = _picture(sig, flt, conc)

    if has_playbook:
        rec, conf = _recommendation(sel, conc, flt)
    else:
        rec, conf = _no_playbook_recommendation(sig, flt)

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
                          + ("wind + fleet" if flt.get("available") else "wind") + " picture only.")
    else:
        assessment = rec["action"]

    conf = min(conf, 0.7 if not has_playbook else 0.95)
    if len(picture) <= 1:
        conf = min(conf, 0.5)

    return {"available": True, "mode": "deterministic", "assessment": assessment,
            "picture": picture, "concordance": conc, "recommendation": rec,
            "caveats": _caveats(sig, flt, has_playbook),
            "confidence": _conf_label(conf), "confidence_value": round(conf, 2),
            "recommended_label": sel.get("recommended_label"),
            "disclaimer": "Advisory — the boat's own read of its own data + common public info. "
                          "The crew decides."}


# =============================================================================================
# The scenario space — curated HARD cases + random combinations (Plan: weight toward the fights).
# =============================================================================================
def _cond_for(sc: dict, i: int) -> dict:
    """Deterministic plausible instrument context for the labeling UI (NOT scored)."""
    tws = 8 + (i * 3) % 12
    legs = ["beat to Windward", "run to Leeward", "beat to the weather gate"]
    leg = legs[i % len(legs)]
    pos = wp.point_of_sail(leg)
    tack = ("port", "starboard")[i % 2]
    board = "gybe" if pos == "downwind" else "tack"     # sailors gybe downwind, tack upwind
    return {"tws": tws, "leg": leg, "tack": tack, "board": board, "point_of_sail": pos,
            "next_mark": ["Windward", "Leeward", "Cove Gate"][i % 3],
            "distance_nm": round(1.5 + (i % 5) * 0.6, 1)}


# Curated cases that exercise the judgment axes we most want ranked.
_CURATED = [
    # strong concordance switch (all agree right) — should be a confident SWITCH
    {"tag": "strong_switch_right", "has_playbook": True, "shift": "persist_right",
     "drift": ("right", "act"), "deviation": ("right", "watch"), "fleet": "rival_right"},
    # SPLIT: shift left but forecast shifted right — the hardest call
    {"tag": "split_shift_vs_drift", "has_playbook": True, "shift": "persist_left",
     "drift": ("right", "act"), "deviation": None, "fleet": None},
    # SPLIT: shift right but the fleet is committed left
    {"tag": "split_shift_vs_fleet", "has_playbook": True, "shift": "persist_right",
     "drift": None, "deviation": None, "fleet": "rival_left"},
    # off-script: persistent favoured side with NO variant → departs
    {"tag": "offscript_no_variant", "has_playbook": True, "shift": "persist_left",
     "variant_for_side": False, "drift": ("left", "act"), "deviation": ("left", "act"), "fleet": None},
    # hold with a rival ahead — press if you have leverage
    {"tag": "hold_rival_ahead", "has_playbook": True, "shift": "osc",
     "drift": None, "deviation": None, "fleet": "ahead"},
    # clean hold, nothing firing
    {"tag": "clean_hold", "has_playbook": True, "shift": "steady",
     "drift": None, "deviation": None, "fleet": None},
    # weak: deviation + fleet agree a side with no persistent shift yet
    {"tag": "weak_cluster_right", "has_playbook": True, "shift": "steady",
     "drift": ("right", "watch"), "deviation": ("right", "watch"), "fleet": "rival_right"},
    # no playbook, persistent shift — sail your own read to the side
    {"tag": "noplaybook_persist", "has_playbook": False, "shift": "persist_right",
     "drift": None, "deviation": None, "fleet": None},
    # no playbook, oscillating — sail your phase
    {"tag": "noplaybook_osc", "has_playbook": False, "shift": "osc",
     "drift": None, "deviation": None, "fleet": "behind"},
    # switch but split by deviation — confirm before committing
    {"tag": "switch_split_dev", "has_playbook": True, "shift": "persist_right",
     "drift": ("right", "watch"), "deviation": ("left", "act"), "fleet": None},
]


def scenarios(random_n: int, seed: int) -> list[dict]:
    """The curated hard cases + `random_n` random combinations (reproducible via `seed`)."""
    out = []
    for i, sc in enumerate(_CURATED):
        s = dict(sc); s["_i"] = i; s["cond"] = _cond_for(s, i)
        out.append(s)

    rng = random.Random(seed)
    shifts = ["persist_left", "persist_right", "osc", "steady"]
    drifts = [None, ("right", "watch"), ("right", "act"), ("left", "watch"), ("left", "act")]
    devs = [None, ("left", "watch"), ("left", "act"), ("right", "watch"), ("right", "act")]
    fleets = [None, "rival_left", "rival_right", "ahead", "behind"]
    for j in range(random_n):
        i = len(_CURATED) + j
        s = {"tag": f"rand_{j}", "_i": i,
             "has_playbook": rng.random() > 0.25,
             "shift": rng.choice(shifts),
             "drift": rng.choice(drifts),
             "deviation": rng.choice(devs),
             "fleet": rng.choice(fleets),
             "variant_for_side": rng.random() > 0.3}
        s["cond"] = _cond_for(s, i)
        out.append(s)
    return out
