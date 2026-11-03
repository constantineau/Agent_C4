"""The strategy digest's RENDERING — pure functions that turn engine reads into the picture rows,
concordance, recommendation and assessment the onboard LLM is seeded with.

Single source of truth for two callers (#54):
  * the engine (`vps/agent/app/strategy.py`) — what the 7B actually sees in a race;
  * the matcher-LoRA corpus generators (`pi/orin/copilot/eval/scengen.py`) — what the 7B is trained
    and evaluated on.
Before #54 the generators re-implemented the picture with their own wording and keys ("text" rows,
a sail-state row production never sends, "on_plan" instead of "on-plan"), so training and serving
saw different inputs. Now both go through these functions. Pure stdlib + windphrase so it imports
anywhere the repo is present (the engine container, the dev VM, a training box).
"""
from shared import windphrase as wp

_OPP = {"left": "right", "right": "left"}


def conf_label(c):
    return "high" if c >= 0.7 else "med" if c >= 0.45 else "low"


def concordance(lean, sig, fleet_side):
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


def picture(sig, flt, conc):
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
        """Where a rival is RELATIVE to us — cross-track side + on-water ahead/behind.
        leverage_nm is each boat's offset from the COURSE line (+ = right), so the side relative to
        us is rival minus own (#160). Own offset unknown → say it against the course instead."""
        lev, lead = row.get("leverage_nm"), row.get("on_water_lead_nm")
        own_lev = (flt.get("own") or {}).get("leverage_nm")
        bits = []
        if lev is not None and own_lev is not None:
            rel = round(lev - own_lev, 1)
            if rel:
                bits.append(f"{abs(rel)} nm to our {'right' if rel > 0 else 'left'}")
        elif lev and round(lev, 1):
            bits.append(f"{abs(round(lev, 1))} nm {'right' if lev > 0 else 'left'} of the course")
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
                        "confidence": conf_label(top.get("confidence") or 0.4)})

    if conc.get("strength") not in (None, "none"):
        out.append({"signal": "concordance", "read": conc["note"],
                    "grounded_in": ["get_selector", "get_tactics"],
                    "confidence": {"strong": "high", "weak": "low", "split": "med"}.get(conc["strength"], "low")})
    return out


def recommendation(sel, conc, flt):
    """Fold the selector's HOLD/SWITCH/OFF-SCRIPT backbone + the concordance + the fleet threat into
    one recommendation. Deterministic — the LLM layer may later re-word or extend it.
    Returns (recommendation, confidence 0..1)."""
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
                "urgency": urg, "confidence": conf_label(conf)}, conf

    if action == "off_script":
        return {"action": f"Off-book: {sel.get('value', 'sail the favoured side')}",
                "vs_playbook": "departs", "target_variant": None,
                "rationale": sel.get("why", "") + " No plan for this side.",
                "grounded_in": sel.get("driven_by", []) + ["get_selector"],
                "urgency": "now" if sel.get("status") == "act" else "soon",
                "confidence": conf_label(min(0.7, conf))}, min(0.7, conf)

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
            "urgency": urg, "confidence": conf_label(conf)}, conf


def assessment(conc, rec, sel, sig, flt, has_playbook):
    """The digest's headline: lead with the concordance verdict when there is one."""
    if conc.get("strength") == "strong":
        return f"Signals agree {conc['lean']}. {rec['action']}"
    if conc.get("strength") == "split":
        return f"Signals split. {rec['action']}"
    if not has_playbook:
        sh = sig.get("shift") or {}
        fav = sh.get("favored_side")
        if sh.get("persistent") and fav in ("left", "right"):
            return f"No gameplan loaded. Wind favours the {fav}."
        return ("No gameplan loaded. Using live "
                + ("wind and fleet" if flt.get("available") else "wind") + " only.")
    extra = sel.get("value", "") or ""
    return (rec["action"] if not extra or extra.lower() in rec["action"].lower()
            else f"{rec['action']} — {extra}")


# ---------------------------------------------------------------- the extra picture rows (optional reads)

def buoys_row(upc):
    """Up-course leading indicator: a buoy ahead reporting a material difference from own wind.
    None when the difference isn't material (|ΔTWS| < 3 kn and |ΔTWD| < 15°)."""
    dtws, dtwd = upc.get("tws_delta_kn"), upc.get("twd_shift_deg")
    if not upc or not ((abs(dtws or 0) >= 3) or (abs(dtwd or 0) >= 15)):
        return None
    bits = []
    if abs(dtws or 0) >= 3:
        bits.append(f"{abs(dtws):g} kn {'MORE' if dtws > 0 else 'LESS'} pressure")
    if abs(dtwd or 0) >= 15:
        bits.append(f"breeze {abs(dtwd):g}° {'right' if dtwd > 0 else 'left'} of here")
    return {"signal": "buoys",
            "read": (f"Up-course buoy {upc.get('name') or upc.get('station')} "
                     f"({upc.get('range_nm')} nm ahead): " + ", ".join(bits)
                     + f" ({upc.get('age_min')} min old)"),
            # a LIST like every other row (was a bare string — the copilot's grounding allow-list
            # iterated it character by character, so get_buoys never counted as grounded)
            "grounded_in": ["get_buoys"], "confidence": "engine"}


def plangap_row(pg):
    """Plan gap: own observed wind vs the promise the plan was built on. None unless watch/act."""
    if not (pg.get("available") and pg.get("status") in ("watch", "act")):
        return None
    return {"signal": "plan_gap",
            "read": (f"Plan said {pg['plan_tws_kn']:g} kn at {pg['plan_twd']}°, you have "
                     f"{pg['obs_tws_kn']:g} kn at {pg['obs_twd']}°"),
            "grounded_in": ["get_plangap"],
            "confidence": "med" if pg["status"] == "act" else "low"}


def trend_read_text(w):
    """One crew-facing sentence for a trend window record — racer wind language (right/left,
    from→to). Used by trend.get_trend and, through trend_row, by the strategy picture."""
    bits = []
    r = w.get("tws_rate_kn_per_hr")
    if r is not None:
        verb = "building" if r > 0.3 else "fading" if r < -0.3 else "holding"
        if verb == "holding":
            bits.append(f"wind holding ~{w['tws_to_kn']:g} kn")
        else:
            bits.append(f"wind {verb} {abs(r):g} kn/hr ({w['tws_from_kn']:g}→{w['tws_to_kn']:g} kn)")
    d = w.get("twd_rate_deg_per_hr")
    if d is not None and w.get("twd_dir") in ("right", "left"):
        bits.append(f"shifting {w['twd_dir']} ~{abs(d):g}°/hr (was {w['twd_from_deg']}°, "
                    f"now {w['twd_to_deg']}°)")
    hrs = w["window_min"] / 60
    tail = f" over the last {hrs:g} h" if bits else ""
    return (", ".join(bits) + tail) if bits else None


def trend_row(tr):
    """A material build/fade or a walking breeze. None below 1 kn/hr and 5°/hr."""
    if not (tr.get("available") and tr.get("read")
            and (abs(tr.get("tws_trend_kn_per_hr") or 0) >= 1.0
                 or abs(tr.get("twd_trend_deg_per_hr") or 0) >= 5.0)):
        return None
    return {"signal": "trend", "read": tr["read"][0].upper() + tr["read"][1:],
            "grounded_in": ["get_trend"], "confidence": "engine"}


def plays_block(pl):
    """The deterministic matcher's output as it rides the digest: (plays dict | None, picture row |
    None). Armed + arming plays go in `detail` (the copilot builds its [armed]/[arming] tags from
    it); an armed play also gets a picture row."""
    block, row = None, None
    if pl.get("available") and (pl.get("armed") or pl.get("arming")):
        block = {"armed": pl["armed"], "arming": pl["arming"],
                 "detail": [{k: r.get(k) for k in
                             ("id", "name", "status", "guidance", "response_type",
                              "stakes_min", "summary", "corroborated", "corroborated_by")}
                            for r in pl["plays"] if r["status"] != "quiet"][:6]}
        if pl["armed"]:
            names = [r["name"] for r in pl["plays"] if r["status"] == "armed"][:3]
            row = {"signal": "plays", "read": "Armed play(s): " + "; ".join(names),
                   "grounded_in": ["get_plays"], "confidence": "engine"}
    return block, row
