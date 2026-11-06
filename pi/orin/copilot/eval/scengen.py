"""Scenario generator — engine digests synthesized FROM target verdicts (MATCHER_LORA_PLAN §3.2-3.3).

Pick which plays should arm / near-miss, then build a signals snapshot consistent with that. The
label exists before the example does; oracle.py re-scores the finished snapshot so any signal
conflict between targeted plays (e.g. one play needs tws>=20, another tws<=9) degrades the TARGET,
never the LABEL — the recorded oracle verdict is always the truth of the snapshot as built.

Train == serve (#54). The snapshot is turned back into the engine READS it stands for (tactics,
forecast drift, route deviation, plan gap, trend, up-course buoy, matcher), and the strategy digest
is rendered by the ENGINE'S OWN `strategy.get_strategy_signals` with those reads stubbed in — the
same selector, picture wording, keys and recommendation the boat sends. The seed and the play-status
map then go through `copilot.strategy_seed` / `strategy_status_map`, the functions the copilot uses
in a race. Before #54 this file wrote its own picture rows ("text" keys, a sail-state row production
never sends, "on_plan"), so the model was trained on inputs it never sees on the boat. Generating
therefore needs the engine code importable (the dev VM / CI: `PYTHONPATH=vps/agent:.`); the Orin
only RUNS a corpus generated elsewhere.

Two views of every example:
  hinted — the deterministic matcher's armed/arming status rides along (tags + an "Armed play(s)"
           picture row), as in a normal race;
  blind  — the matcher reports nothing; the model must match from the picture alone. This is the
           real matching test, and the only regime the eval gates on (see metrics.py).
What the picture SHOWS is recorded per example (`visible`): the engine only adds a row once its read
crosses its watch band, and some signals (sail state, polar %, fatigue, leg) never reach the seed.
A play whose evidence is not in the seed can't be matched blind; metrics.py leaves those out
instead of scoring a guess.

Near-miss modes (§3.3 — teaching "no-match, and why" is most of the value):
  threshold  — one predicate lands JUST past its threshold (14° of a 15° predicate)
  sustain    — conditions all hold but the sustain window hasn't been met (live status: arming);
               only plays that HAVE a sustain window (an all-zero window arms at once)
  wrong_leg  — conditions hold but the boat is off the play's applicable leg (hard gate)
  confounder — the play's own signal is visibly up, in the OPPOSITE direction (e.g. a plan-gap
               surplus for a "promised breeze missing" play): right vocabulary, wrong condition
sustain and wrong_leg can't be told apart from an armed play without the matcher's tags (nothing
in the seed carries the sustain clock or the leg), so blind views never score them.
"""

import contextlib
import os
import sys

from .. import copilot
from . import oracle

# A becalmed-but-healthy baseline every scenario starts from — nothing arms on these.
_BASE = {
    "time_behind_min": 1.5, "xte_nm": 0.3,
    "drift_twd_deg": 3.0, "drift_twd_signed_deg": 3.0, "drift_tws_kn": 0.5,
    "shift_persistent": False, "tws_kn": 12.0, "fatigue_index": 0.3,
    "hoisted_sail": ["J1"], "reef": 0, "sail_out_of_service": [],
    "upcourse_tws_delta_kn": 0.5, "upcourse_twd_shift_deg": 2.0, "_upcourse_name": "45008",
    "polar_pct": 97.0, "current_leg": 1,
    "tws_trend_kn_per_hr": 0.2, "twd_trend_deg_per_hr": 1.0,
    "plangap_twd_deg": 2.0, "plangap_twd_signed_deg": 2.0, "plangap_tws_kn": -0.5,
}

MODES = ("threshold", "sustain", "wrong_leg", "confounder")
BLIND_MODES = ("threshold", "confounder")          # the near-miss modes a blind view can score

# Signals with a meaningful sign — a confounder flips one of these to the opposite direction.
_SIGNED = {"drift_twd_signed_deg", "plangap_tws_kn", "plangap_twd_signed_deg",
           "tws_trend_kn_per_hr", "twd_trend_deg_per_hr", "upcourse_tws_delta_kn",
           "upcourse_twd_shift_deg"}
# The seed shows these in whole units (degrees, minutes): a near-miss must miss by at least one
# step or the rendered number reads as meeting the threshold.
_DISPLAY_STEP = {"drift_twd_signed_deg": 1.0, "time_behind_min": 1.0,
                 "plangap_twd_signed_deg": 1.0, "upcourse_twd_shift_deg": 1.0}


def _satisfying(op, value, margin=0.18):
    if op == "==":
        return value
    v = float(value)
    pad = max(abs(v) * margin, 0.8)
    return round(v + pad, 1) if op == ">=" else round(v - pad, 1)


def _just_missing(op, value, signal=None):
    if op == "==":
        return None                                   # a discrete miss is simply "not set"
    v = float(value)
    pad = max(abs(v) * 0.07, 0.3, _DISPLAY_STEP.get(signal, 0.0))   # "14 of a 15° predicate"
    return round(v - pad, 1) if op == ">=" else round(v + pad, 1)


def _set_signal(sig, pinned, name, value):
    """Set a signal unless a previously-targeted play already pinned it to something else."""
    if name in pinned and sig.get(name) != value:
        return False
    if name in ("hoisted_sail", "sail_out_of_service"):
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


def _target_play(sig, pinned, play, miss_pred=None, flip_pred=None):
    """Drive one play's predicates into (or just short of) holding. Returns False on a pin
    conflict — the caller then simply drops that play from the target. `flip_pred` sets that
    predicate to the opposite-signed satisfying value (the confounder)."""
    preds = (play.get("conditions") or {}).get("predicates") or []
    for i, p in enumerate(preds):
        if miss_pred == i:
            v = _just_missing(p["op"], p["value"], p["signal"])
            if v is None:
                continue
        elif flip_pred == i:
            v = -_satisfying(p["op"], p["value"])
        else:
            v = _satisfying(p["op"], p["value"])
        if not _set_signal(sig, pinned, p["signal"], v):
            return False
    return True


def _on_leg(sig, pinned, play, on=True):
    legs = (play.get("applicability") or {}).get("legs")
    if not legs:
        return True
    target = legs[0] if on else next(l for l in range(6) if l not in legs)
    return _set_signal(sig, pinned, "current_leg", target)


def _has_sustain(play):
    return any((p.get("sustain_min") or 0) > 0
               for p in ((play.get("conditions") or {}).get("predicates") or []))


def _signed_pred(play):
    """Index of a predicate on a signed signal (a confounder can flip it), else None."""
    for i, p in enumerate((play.get("conditions") or {}).get("predicates") or []):
        if p["signal"] in _SIGNED and p["op"] in (">=", "<="):
            return i
    return None


# ---------------------------------------------------------------------------- engine reads

def _band(x, consider, commit):
    """deviation/drift/plangap banding from a fresh state (no hysteresis carried over)."""
    x = abs(x or 0)
    return 2 if x >= commit else 1 if x >= consider else 0


_STATUS = {0: "ok", 1: "watch", 2: "act"}


def engine_reads(sig, rng):
    """The per-module engine reads that `matcher.gather` would have turned into `sig`. Thresholds
    are the engine modules' defaults (drift/plangap 15°/30°, 4/8 kn; deviation 0.4/1.0 nm,
    120/300 s; trend row at 1 kn/hr or 5°/hr; buoy row at 3 kn or 15°)."""
    _engine()                                   # puts the repo root on sys.path for `shared`
    from shared import strategy_render as sr
    from shared import windphrase as wp

    pos = rng.choice(["upwind", "upwind", "downwind"])
    tack = rng.choice(["port", "starboard"])
    d = sig["drift_twd_signed_deg"]
    ref = rng.randrange(180, 300)
    sign = (1 if d > 0 else -1) if abs(d) >= 5 else rng.choice([1, -1])
    base_twd = ref + rng.randrange(-4, 5)
    if sig["shift_persistent"]:
        now_twd = base_twd + sign * rng.choice([8, 10, 12, 15])
    else:
        now_twd = base_twd + rng.choice([-2, -1, 0, 1, 2])
    tactics = {"available": True, "tack": tack, "point_of_sail": pos,
               "favored_side": wp.favored_side(wp.shift_sign(base_twd, now_twd), pos) or "either",
               "wind": {"persistent": bool(sig["shift_persistent"]),
                        "mean_12min": base_twd % 360, "now": now_twd % 360,
                        "oscillation_deg": rng.choice([6, 8, 10, 12])}}

    dband = max(_band(d, 15, 30), _band(sig["drift_tws_kn"], 4, 8))
    drift = {"available": True, "status": _STATUS[dband],
             "drift_dir": "right" if d > 5 else "left" if d < -5 else "shifted",
             "drift_twd_deg": abs(d), "drift_twd_signed_deg": d,
             "drift_tws_kn": sig["drift_tws_kn"],
             "ref_twd": ref % 360, "now_twd": round(ref + d) % 360}

    xte, behind_s = sig["xte_nm"], round(sig["time_behind_min"] * 60)
    vband = max(_band(xte, 0.4, 1.0), _band(behind_s, 120, 300))
    deviation = {"available": True, "status": _STATUS[vband],
                 "xte_status": _STATUS[_band(xte, 0.4, 1.0)],   # only XTE votes a side (#43)
                 "xte_side": rng.choice(["left", "right"]), "xte_nm": xte,
                 "time_behind_s": behind_s, "variant": None}

    tws, gap, gap_twd = sig["tws_kn"], sig["plangap_tws_kn"], sig["plangap_twd_signed_deg"]
    obs_twd = rng.randrange(180, 300)
    gband = max(_band(gap_twd, 15, 30), _band(gap, 4, 8))
    plangap = {"available": True, "status": _STATUS[gband],
               "plan_tws_kn": round(tws - gap, 1), "obs_tws_kn": round(tws, 1),
               "plan_twd": round(obs_twd - gap_twd) % 360, "obs_twd": obs_twd % 360,
               "gap_tws_kn": gap, "gap_twd_deg": abs(gap_twd), "gap_twd_signed_deg": gap_twd}

    r, dr = sig["tws_trend_kn_per_hr"], sig["twd_trend_deg_per_hr"]
    twd_to = rng.randrange(180, 300)
    window = {"window_min": 60, "tws_rate_kn_per_hr": r,
              "tws_from_kn": round(tws - r, 1), "tws_to_kn": round(tws, 1),
              "twd_rate_deg_per_hr": dr,
              "twd_dir": "right" if dr > 1 else "left" if dr < -1 else "steady",
              "twd_from_deg": round(twd_to - dr) % 360, "twd_to_deg": twd_to}
    trend = {"available": True, "tws_trend_kn_per_hr": r, "twd_trend_deg_per_hr": dr,
             "read": sr.trend_read_text(window)}

    upcourse = {"station": sig["_upcourse_name"], "name": sig["_upcourse_name"],
                "tws_delta_kn": sig["upcourse_tws_delta_kn"],
                "twd_shift_deg": sig["upcourse_twd_shift_deg"],
                "range_nm": rng.choice([8.5, 14.0, 22.3]), "age_min": rng.choice([12, 25, 40])}
    return {"tactics": tactics, "drift": drift, "deviation": deviation, "plangap": plangap,
            "trend": trend, "upcourse": upcourse}


# Which oracle signals each picture row puts in front of the model.
_ROW_SIGNALS = {
    "shift": {"shift_persistent"},
    "forecast": {"drift_twd_signed_deg", "drift_twd_deg", "drift_tws_kn"},
    "deviation": {"xte_nm", "time_behind_min"},
    "plan_gap": {"plangap_tws_kn", "plangap_twd_deg", "plangap_twd_signed_deg", "tws_kn"},
    "trend": {"tws_trend_kn_per_hr", "twd_trend_deg_per_hr", "tws_kn"},
    "buoys": {"upcourse_tws_delta_kn", "upcourse_twd_shift_deg"},
}


def visible_signals(picture):
    """The oracle signals whose values the rendered picture shows."""
    out = set()
    for row in picture:
        out |= _ROW_SIGNALS.get(row.get("signal"), set())
    return out


# ---------------------------------------------------------------------------- the engine renderer

_ENG = None


def _engine():
    """The engine modules (vps/agent/app). Importable on the dev VM and in CI; not on the Orin —
    generate corpora there and copy them over."""
    global _ENG
    if _ENG is None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
        for p in (os.path.join(root, "vps", "agent"), root):
            if p not in sys.path:
                sys.path.append(p)
        try:
            from app import buoys, matcher, plangap, selector, strategy, trend
        except Exception as e:      # say what's missing and how to fix it
            raise RuntimeError(
                "corpus generation renders seeds with the engine's own strategy code; run it where "
                "vps/agent imports (dev VM: PYTHONPATH=vps/agent:.) — "
                f"{type(e).__name__}: {e}") from e
        _ENG = {"strategy": strategy, "selector": selector, "buoys": buoys,
                "plangap": plangap, "trend": trend, "matcher": matcher}
    return _ENG


@contextlib.contextmanager
def _stubbed(bundle, reads, plays):
    eng = _engine()
    sel = eng["selector"]
    patches = [
        (sel.deviation, "_load_playbook", lambda: bundle),
        (sel.deviation, "get_deviation", lambda route=None: reads["deviation"]),
        (sel.drift_mod, "get_drift", lambda route=None: reads["drift"]),
        (sel.tactics, "get_tactics", lambda route=None: reads["tactics"]),
        (eng["strategy"].fleet_mod, "get_fleet",
         lambda max_range_nm=40.0: {"available": False, "fleet": [], "traffic": []}),
        (eng["buoys"], "get_buoys",
         lambda route=None: {"available": True, "upcourse": reads["upcourse"]}),
        (eng["plangap"], "get_plangap", lambda route=None: reads["plangap"]),
        (eng["trend"], "get_trend", lambda route=None: reads["trend"]),
        (eng["matcher"], "get_plays", lambda route=None: plays),
    ]
    saved = [(m, n, getattr(m, n)) for m, n, _ in patches]
    try:
        for m, n, f in patches:
            setattr(m, n, f)
        sel._SETTLE.clear()
        sel._CONFIRM.clear()
        yield eng
    finally:
        for m, n, f in saved:
            setattr(m, n, f)
        sel._SETTLE.clear()
        sel._CONFIRM.clear()


def _matcher_out(lib, smap):
    """What matcher.get_plays returns for this status map."""
    rows = [{"id": str(p["id"]), "name": p["name"], "status": smap.get(str(p["id"]), "quiet"),
             "guidance": (p.get("response") or {}).get("guidance"),
             "response_type": (p.get("response") or {}).get("type"),
             "stakes_min": p.get("stakes_min"), "summary": p.get("summary"),
             "corroborated": None, "corroborated_by": []} for p in lib.get("plays") or []]
    return {"available": True, "plays": rows,
            "armed": [r["id"] for r in rows if r["status"] == "armed"],
            "arming": [r["id"] for r in rows if r["status"] == "arming"], "watchlist": []}


def render_digest(lib, reads, smap):
    """The strategy digest exactly as the engine builds it from these reads."""
    with _stubbed(lib, reads, _matcher_out(lib, smap)) as eng:
        return eng["strategy"].get_strategy_signals(None)


def render_views(lib, sig, smap, rng):
    """{hinted, blind}: each {seed, status_map, picture_signals} as the copilot would send it, plus
    `visible`: the oracle signals the picture shows."""
    reads = engine_reads(sig, rng)
    views = {}
    for name, sm in (("hinted", smap), ("blind", {})):
        digest = render_digest(lib, reads, sm)
        views[name] = {"seed": copilot.strategy_seed(digest),
                       "status_map": copilot.strategy_status_map(digest),
                       "picture_signals": [r.get("signal") for r in digest.get("picture", [])]}
        if name == "blind":
            views["visible"] = sorted(visible_signals(digest.get("picture", [])))
    return views


# ---------------------------------------------------------------------------- scenario assembly

def blind_label(lib, oracle_label, visible):
    """The blind-view truth: armed plays whose every predicate signal is visible in the seed;
    near-misses of the modes a blind view can decide. Everything else armed or near is EXCLUDED
    (a prediction for it is neither credited nor penalized)."""
    preds = {str(p["id"]): {q["signal"]
                            for q in (p.get("conditions") or {}).get("predicates") or []}
             for p in lib.get("plays") or []}
    armed = [pid for pid in oracle_label["armed"] if preds.get(pid, set()) <= set(visible)]
    near = {pid: m for pid, m in oracle_label["near"].items() if m in BLIND_MODES}
    excluded = sorted((set(oracle_label["armed"]) - set(armed))
                      | {pid for pid, m in oracle_label["near"].items() if m not in BLIND_MODES})
    return {"armed": sorted(armed), "near": near, "excluded": excluded}


def make_scenario(rng, lib, n_armed=1, near_modes=("threshold",)):
    """One labeled example. Targets n_armed plays + one near-miss per requested mode; whatever
    survives pin conflicts is re-scored by the oracle, and THAT verdict is the label."""
    plays = list(lib.get("plays") or [])
    rng.shuffle(plays)
    sig = dict(_BASE)
    sig["hoisted_sail"] = list(_BASE["hoisted_sail"])
    sig["sail_out_of_service"] = list(_BASE["sail_out_of_service"])
    pinned, sustained, near = set(), {}, {}

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
                hit = _target_play(sig, pinned, p, miss_pred=miss)
            elif mode == "sustain":
                hit = (_has_sustain(p) and _target_play(sig, pinned, p)
                       and _on_leg(sig, pinned, p, on=True))
                if hit:
                    sustained[pid] = False
            elif mode == "wrong_leg":
                hit = bool((p.get("applicability") or {}).get("legs")
                           and _target_play(sig, pinned, p) and _on_leg(sig, pinned, p, on=False))
            else:   # confounder
                i = _signed_pred(p)
                hit = i is not None and _target_play(sig, pinned, p, flip_pred=i)
            if hit:
                near[pid] = mode
                break

    # The oracle scores the snapshot AS BUILT — pin conflicts degrade targets, never labels.
    smap = oracle.status_map(lib, sig, sustained)
    armed = sorted(pid for pid, st in smap.items() if st == "armed")
    near = {pid: m for pid, m in near.items() if smap.get(pid) != "armed"}
    label = {"armed": armed, "near": near,
             "quiet": sorted(pid for pid, st in smap.items() if st != "armed" and pid not in near)}
    views = render_views(lib, sig, smap, rng)
    return {
        "signals": sig, "sustained": sustained, "status_map": smap,
        "oracle": label,
        "blind_oracle": blind_label(lib, label, views["visible"]),
        "views": {"hinted": views["hinted"], "blind": views["blind"]},
        "visible": views["visible"],
    }


def sample_modes(rng, modes=MODES):
    """1-2 near-miss modes per scenario, every mode represented across a corpus."""
    return tuple(rng.sample(modes, min(len(modes), rng.choice([1, 1, 2]))))
