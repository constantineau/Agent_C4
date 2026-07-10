"""Scenario generator — engine digests synthesized FROM target verdicts (MATCHER_LORA_PLAN §3.2-3.3).

Pick which plays should arm / near-miss, then build a signals snapshot consistent with that and
render the same digest seed shape `strategy_brief` feeds the 7B in production (train == inference).
The label exists before the example does; oracle.py re-scores the finished snapshot so any signal
conflict between targeted plays (e.g. one play needs tws>=20, another tws<=9) degrades the TARGET,
never the LABEL — the recorded oracle verdict is always the truth of the snapshot as built.

Near-miss modes (§3.3 — teaching "no-match, and why" is most of the value):
  threshold  — the play's weakest predicate lands JUST below (14° of a 15° predicate)
  sustain    — conditions all hold but the sustain window hasn't been met (live status: arming)
  wrong_leg  — conditions hold but the boat is off the play's applicable leg (hard gate)
  confounder — signals stay quiet; the DIGEST talks in the play's vocabulary without its condition
"""

import json
import random

from . import oracle

# A becalmed-but-healthy baseline every scenario starts from — nothing arms on these.
_BASE = {
    "time_behind_min": 2.0, "xte_nm": 0.4,
    "drift_twd_deg": 3.0, "drift_twd_signed_deg": 3.0, "drift_tws_kn": 0.5,
    "shift_persistent": False, "tws_kn": 12.0, "fatigue_index": 0.3,
    "hoisted_sail": ["J1"], "reef": 0, "sail_out_of_service": [],
    "upcourse_tws_delta_kn": 0.5, "upcourse_twd_shift_deg": 2.0, "_upcourse_name": "45008",
    "polar_pct": 97.0, "current_leg": 1,
    "tws_trend_kn_per_hr": 0.2, "twd_trend_deg_per_hr": 1.0,
    "plangap_twd_deg": 2.0, "plangap_twd_signed_deg": 2.0, "plangap_tws_kn": -0.5,
}

_MODES = ("threshold", "sustain", "wrong_leg", "confounder")


def _satisfying(op, value, margin=0.18):
    if op == "==":
        return value
    v = float(value)
    pad = max(abs(v) * margin, 0.8)
    return round(v + pad, 1) if op == ">=" else round(v - pad, 1)


def _just_missing(op, value):
    if op == "==":
        return None                                   # a discrete miss is simply "not set"
    v = float(value)
    pad = max(abs(v) * 0.07, 0.3)                     # "14 of a 15° predicate"
    return round(v - pad, 1) if op == ">=" else round(v + pad, 1)


def _set_signal(sig, pinned, name, value):
    """Set a signal unless a previously-targeted play already pinned it to something else."""
    if name in pinned and sig.get(name) != value:
        return False
    if name == "hoisted_sail":
        cur = sig.get(name) or []
        if value not in cur:
            sig[name] = cur + [value]
    elif name == "sail_out_of_service":
        cur = sig.get(name) or []
        if value not in cur:
            sig[name] = cur + [value]
    else:
        sig[name] = value
    pinned.add(name)
    # keep the signed/unsigned drift + plangap pairs coherent (gather() derives one from the other)
    if name == "drift_twd_signed_deg":
        sig["drift_twd_deg"] = abs(sig[name])
    if name == "plangap_twd_signed_deg":
        sig["plangap_twd_deg"] = abs(sig[name])
    return True


def _target_play(sig, pinned, play, miss_pred=None):
    """Drive one play's predicates into (or just short of) holding. Returns False on a pin
    conflict — the caller then simply drops that play from the target."""
    preds = (play.get("conditions") or {}).get("predicates") or []
    for i, p in enumerate(preds):
        if miss_pred == i:
            v = _just_missing(p["op"], p["value"])
            if v is None:
                continue
            if not _set_signal(sig, pinned, p["signal"], v):
                return False
        else:
            if not _set_signal(sig, pinned, p["signal"], _satisfying(p["op"], p["value"])):
                return False
    return True


def _on_leg(sig, pinned, play, on=True):
    legs = (play.get("applicability") or {}).get("legs")
    if not legs:
        return True
    target = legs[0] if on else next(l for l in range(6) if l not in legs)
    return _set_signal(sig, pinned, "current_leg", target)


# ---------------------------------------------------------------------------- digest rendering

def _picture(sig, confounder_plays):
    """The engine-picture lines the seed carries, rendered from the snapshot the way
    strategy._picture phrases them — numbers first, one grounded tool per line."""
    rows = []

    def add(text, tool):
        rows.append({"text": text, "grounded_in": [tool]})

    d = sig["drift_twd_signed_deg"]
    if abs(d) >= 5:
        side = "right" if d > 0 else "left"
        add(f"Forecast drift: live GRIB has walked {side} {abs(d):g}° vs the frozen promise"
            + (" and the shift reads persistent" if sig["shift_persistent"] else
               " but it still reads oscillating"), "get_drift")
    if abs(sig["plangap_tws_kn"]) >= 2:
        word = "under" if sig["plangap_tws_kn"] < 0 else "over"
        add(f"Plan gap: own wind {abs(sig['plangap_tws_kn']):g} kn {word} the promised pressure "
            f"for here/now", "get_plangap")
    if abs(sig["tws_trend_kn_per_hr"]) >= 1:
        word = "building" if sig["tws_trend_kn_per_hr"] > 0 else "fading"
        add(f"Trend: breeze {word} {abs(sig['tws_trend_kn_per_hr']):g} kn/hr over the last hour "
            f"({sig['tws_kn']:g} kn now)", "get_trend")
    if sig["time_behind_min"] >= 5 or sig["xte_nm"] >= 1:
        add(f"Deviation: {sig['time_behind_min']:g} min behind plan, {sig['xte_nm']:g} nm off "
            f"track, boatspeed {sig['polar_pct']:g}% of target", "get_deviation")
    if sig["fatigue_index"] >= 0.5:
        add(f"Helm fatigue index {sig['fatigue_index']:g} (own baseline)", "get_fatigue")
    if abs(sig["upcourse_tws_delta_kn"]) >= 3:
        word = "MORE" if sig["upcourse_tws_delta_kn"] > 0 else "LESS"
        add(f"Up-course buoy {sig['_upcourse_name']}: {abs(sig['upcourse_tws_delta_kn']):g} kn "
            f"{word} pressure than our wind", "get_buoys")
    sails = "+".join(sig["hoisted_sail"]) or "none"
    oos = (", " + "/".join(sig["sail_out_of_service"]) + " out of service"
           if sig["sail_out_of_service"] else "")
    add(f"Sail state: {sails} flying, reef {sig['reef']}{oos}; TWS {sig['tws_kn']:g} kn; "
        f"leg {sig['current_leg']}", "get_tactics")

    # §3.3 confounders: lines that share a play's surface vocabulary while its condition FAILS —
    # authored into the picture so a narrative-matching model has something plausible to bite on.
    for p in confounder_plays:
        kind = (p.get("scenario") or {}).get("kind")
        if kind == "rotation":
            add("Momentary wind swings both ways this hour — oscillation, nothing persistent yet",
                "get_tactics")
        elif kind == "pressure":
            add("Crew reports the breeze 'feels soft' though the numbers sit on the promise",
                "get_tactics")
        elif kind == "pace":
            add("A slow tack cost a boatlength just now; pace otherwise on target", "get_deviation")
        elif kind == "sail_guidance":
            add("Puffs briefly touching the crossover number, lulls right back off it", "get_trend")
        elif kind == "timing":
            add("Briefly wandered off the line in traffic, back on it now", "get_deviation")
    return rows


def _seed(sig, lib, armed_ids, confounder_plays):
    conc = {"strength": "strong" if len(armed_ids) >= 2 else ("split" if armed_ids else "weak")}
    rec = {"action": "Hold the plan", "vs_playbook": "on_plan",
           "rationale": "No trigger fully met." if not armed_ids else
                        "A pre-authored condition looks met — see the armed plays."}
    assessment = ("Signals quiet — sailing the plan." if not armed_ids else
                  "Live picture is meeting pre-race conditions; consolidate deliberately.")
    return ("STRATEGIC PICTURE (engine-computed facts — reuse these, invent nothing):\n"
            + json.dumps({"assessment": assessment,
                          "picture": _picture(sig, confounder_plays),
                          "concordance": conc, "recommendation": rec},
                         separators=(",", ":")))


# ---------------------------------------------------------------------------- scenario assembly

def make_scenario(rng, lib, n_armed=1, near_modes=("threshold",)):
    """One labeled example. Targets n_armed plays + one near-miss per requested mode; whatever
    survives pin conflicts is re-scored by the oracle, and THAT verdict is the label."""
    plays = list(lib.get("plays") or [])
    rng.shuffle(plays)
    sig = dict(_BASE)
    sig["hoisted_sail"] = list(_BASE["hoisted_sail"])
    sig["sail_out_of_service"] = list(_BASE["sail_out_of_service"])
    pinned, sustained, near, confounders = set(), {}, {}, []

    queue = list(plays)
    for _ in range(n_armed):
        while queue:
            p = queue.pop(0)
            if _target_play(sig, pinned, p) and _on_leg(sig, pinned, p, on=True):
                sustained[p["id"]] = True
                break

    for mode in near_modes:
        while queue:
            p = queue.pop(0)
            pid = p["id"]
            if mode == "threshold":
                miss = rng.randrange(len(p["conditions"]["predicates"]))
                if _target_play(sig, pinned, p, miss_pred=miss):
                    near[pid] = mode
                    break
            elif mode == "sustain":
                if _target_play(sig, pinned, p) and _on_leg(sig, pinned, p, on=True):
                    sustained[pid] = False
                    near[pid] = mode
                    break
            elif mode == "wrong_leg":
                if ((p.get("applicability") or {}).get("legs")
                        and _target_play(sig, pinned, p) and _on_leg(sig, pinned, p, on=False)):
                    near[pid] = mode
                    break
            elif mode == "confounder":
                confounders.append(p)
                near[pid] = mode
                break

    # The oracle scores the snapshot AS BUILT — pin conflicts degrade targets, never labels.
    smap = oracle.status_map(lib, sig, sustained)
    armed = sorted(pid for pid, st in smap.items() if st == "armed")
    near = {pid: m for pid, m in near.items() if smap.get(pid) != "armed"}
    return {
        "signals": sig, "sustained": sustained, "status_map": smap,
        "oracle": {"armed": armed, "near": near,
                   "quiet": sorted(pid for pid, st in smap.items()
                                   if st != "armed" and pid not in near)},
        "seed": _seed(sig, lib, armed, confounders),
    }


def sample_modes(rng):
    """1-2 near-miss modes per scenario, all four represented across a corpus."""
    return tuple(rng.sample(_MODES, rng.choice([1, 1, 2])))
