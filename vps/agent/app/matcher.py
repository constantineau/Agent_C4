"""Tier-1 PLAY MATCHER — Playbook v2 Phase D (docs/PLAYBOOK_V2.md §6).

The v2 bundle carries PLAYS: named scenarios with machine-checkable detection predicates and a
pre-authored response. This module evaluates every play's predicates against the boat's LIVE
signals — deterministic, on the Pi engine, no LLM/Orin required, legal in-race (own instruments +
pre-loaded homework + common public data). A play whose predicates ALL hold is ARMING; hold them
for the play's sustain window and it is ARMED (raise slow); any predicate going false clears it
immediately (clear fast) — the same Schmitt discipline as the deviation/drift triggers.

Signals resolved (all existing Tier-1 reads; a signal with no live value simply doesn't arm):
  time_behind_min · xte_nm          — deviation.get_deviation (vs the frozen recommended track)
  drift_twd_deg · drift_twd_signed_deg · drift_tws_kn — drift.get_drift (vs the frozen forecast)
  shift_persistent                  — tactics.get_tactics (the engine's persistence hysteresis)
  tws_kn                            — the boat's own true wind
  fatigue_index                     — fatigue.get_fatigue
  hoisted_sail · reef · sail_out_of_service — the crew-set SAIL STATE (the dashboard's sail
                                       chips; a SET — the boat flies combinations like C0+J2 or
                                       kite+staysail; `==` predicates match membership; there is
                                       no instrument for a blown kite — the crew declares it)
  polar_pct                         — live % of the rated polar, WINDOWED (mean over the last
                                       ~10 min of STW vs the polar target at each sample's own
                                       TWS/TWA) so a tack's momentary dip can't reset a play's
                                       30-min sustain; instantaneous fallback when the archive
                                       window is thin
  current_leg                       — the leg the boat is on, from the navigator's next mark:
                                       leg N is the leg ARRIVING at the loaded course's marks[N]
                                       (1-based == the next mark's index in the ordered marks).
                                       None when no course/fix — leg gating FAILS OPEN.

LEG GATING: a play whose `applicability.legs` is a list and whose gate is HARD (bundle
`applicability.gate == "hard"`, or kind == "pace" for pre-gate bundles — a pace play's response
is a re-route FROM its specific mark, wrong anywhere else) only arms while `current_leg` is in
that list; the gate participates in the Schmitt discipline (off-leg clears fast). Advisory
applicability (sail-guidance plays authored for a forecast leg but condition-driven) never gates.

The recommendation stays the deterministic selector's; the matcher ARMS plays — the Tier-2 LLM (and
the crew) read the armed set. The auto-coach volunteers a callout when a play newly arms.
"""
from __future__ import annotations

import bisect
import os
import time

from . import buoys
from . import datasource
from . import deviation
from . import drift as drift_mod
from . import fatigue
from . import navigator
from . import tactics

# sustain windows come from each play's predicates (minutes). Scale for bench/tests (0 = instant).
SUSTAIN_SCALE = float(os.environ.get("MATCHER_SUSTAIN_SCALE", "1.0"))

# polar_pct window: long enough to ride through a tack, short enough to track a real slowdown
POLAR_PCT_WINDOW_MIN = float(os.environ.get("MATCHER_POLAR_WINDOW_MIN", "10"))
POLAR_PCT_MIN_SAMPLES = int(os.environ.get("MATCHER_POLAR_MIN_SAMPLES", "6"))
_MS_TO_KN = 1.943844
_RAD_TO_DEG = 57.29577951308232

_DRIFT_SIGN = {"right": 1, "left": -1, "veered": 1, "backed": -1}

# per-play Schmitt state: {play_id: {"since": epoch|None, "armed_at": epoch|None}}
_ST: dict = {}


def clear_state():
    """Forget arm/sustain memory — called when a new playbook is loaded aboard."""
    _ST.clear()


# ---------------------------------------------------------------------------- crew sail state

def get_sail_state():
    """The crew-set sail state: {'hoisted': 'A3'|None, 'out_of_service': ['A2', ...], 'ts': epoch}.
    Persisted in the engine kv (onboard) / app_state (cloud) so the matcher + copilot read one
    truth. Empty dict when never set."""
    try:
        return datasource.active().get_sail_state() or {}
    except Exception:
        return {}


# ordered by "which sail is driving" — the legacy single-sail mirror picks the first flying
_PRIMARY_ORDER = ("A2", "A3", "S2", "C0", "J1", "J2", "J3", "SS")


def _primary(flying):
    for s in _PRIMARY_ORDER:
        if s in flying:
            return s
    return flying[0] if flying else None


def set_sail_state(hoisted=None, flying=None, reef=None, out_of_service=None):
    """Update the crew sail state. The configuration is a SET — the boat flies combinations
    (C0 alone · C0+J2 · kite+staysail …): flying=['C0','J2'] replaces the set; reef='R1' (or ''
    to shake it out); out_of_service=['A2'] replaces the gear-out list (a sail present = crew
    declared it unusable — the gear-loss plays' arming signal). hoisted='A3' is the legacy
    single-sail setter (→ flying=['A3']); the stored `hoisted` mirrors the primary driver so
    older consumers keep working. Returns the stored state."""
    st = get_sail_state()
    if flying is not None:
        st["flying"] = sorted({str(s).upper() for s in flying if s})
    elif hoisted is not None:
        st["flying"] = [str(hoisted).upper()] if hoisted != "" else []
    if reef is not None:
        st["reef"] = (str(reef).upper() or None) if reef != "" else None
    if out_of_service is not None:
        st["out_of_service"] = sorted({str(s).upper() for s in out_of_service if s})
    st.setdefault("flying", [st["hoisted"]] if st.get("hoisted") else [])
    st["hoisted"] = _primary(st["flying"])
    st["ts"] = round(time.time())
    datasource.active().save_sail_state(st)
    return st


# ---------------------------------------------------------------------------- polar % (live)

_POLAR_TABLE = None      # [(tws_kn, twa_deg, target_stw_kn)] — static per session, fetched once


def _polar_table():
    global _POLAR_TABLE
    if _POLAR_TABLE is None:
        try:
            _POLAR_TABLE = [(t, a, s) for (t, a, s) in datasource.active().polars_stw()
                            if s is not None]
        except Exception:
            _POLAR_TABLE = []
    return _POLAR_TABLE


def _target_stw(pol, tws_kn, twa_deg):
    """Nearest polar bucket (same metric as datasource.polar_nearest) from the cached table —
    no per-sample DB round-trips."""
    if not pol:
        return None
    row = min(pol, key=lambda p: abs(p[0] - tws_kn) + abs(p[1] - abs(twa_deg)))
    return row[2]


def _scalar(v):
    if isinstance(v, (tuple, list)):
        v = v[0] if v else None
    return float(v) if v is not None else None


def _nearest_in(times, values, epoch, tol_s=20.0):
    """Value at the timestamp nearest `epoch` within tol; series is time-ordered."""
    if not times:
        return None
    i = bisect.bisect_left(times, epoch)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times) and abs(times[j] - epoch) <= tol_s:
            if best is None or abs(times[j] - epoch) < abs(times[best] - epoch):
                best = j
    return values[best] if best is not None else None


def _polar_pct():
    """Live % of the rated polar: mean over the window of STW / target(tws, twa) per sample —
    windowed so a tack's dip doesn't reset a 30-min sustain. Falls back to one instantaneous
    read when the archive window is thin (fresh boot / bench). None when it can't be known."""
    pol = _polar_table()
    if not pol:
        return None
    src = datasource.active()
    try:
        stw = src.series("navigation.speedThroughWater", POLAR_PCT_WINDOW_MIN)
        tws = src.series("environment.wind.speedTrue", POLAR_PCT_WINDOW_MIN)
        twa = src.series("environment.wind.angleTrueWater", POLAR_PCT_WINDOW_MIN)
    except Exception:
        stw = tws = twa = []
    pcts = []
    if stw and tws and twa:
        tws_t, tws_v = [t for t, _ in tws], [v for _, v in tws]
        twa_t, twa_v = [t for t, _ in twa], [v for _, v in twa]
        step = max(1, len(stw) // 120)          # cap the per-poll work
        for e, v in stw[::step]:
            w, a = _nearest_in(tws_t, tws_v, e), _nearest_in(twa_t, twa_v, e)
            if v is None or w is None or a is None:
                continue
            w_kn = w * _MS_TO_KN
            if w_kn < 3.5:
                continue                        # drifting air — the polar isn't meaningful
            tgt = _target_stw(pol, w_kn, a * _RAD_TO_DEG)
            if tgt and tgt > 0.5:
                pcts.append(v * _MS_TO_KN / tgt * 100.0)
    if len(pcts) >= POLAR_PCT_MIN_SAMPLES:
        return round(sum(pcts) / len(pcts), 1)
    try:                                        # instantaneous fallback — one read beats blind
        v = _scalar(src.latest_value("navigation.speedThroughWater"))
        w = _scalar(src.latest_value("environment.wind.speedTrue"))
        a = _scalar(src.latest_value("environment.wind.angleTrueWater"))
    except Exception:
        return None
    if v is None or w is None or a is None or w * _MS_TO_KN < 3.5:
        return None
    tgt = _target_stw(pol, w * _MS_TO_KN, a * _RAD_TO_DEG)
    return round(v * _MS_TO_KN / tgt * 100.0, 1) if tgt and tgt > 0.5 else None


# ---------------------------------------------------------------------------- current leg

def _current_leg(route=None):
    """1-based number of the leg the boat is on = the next mark's index in the loaded course's
    ordered marks (leg N arrives at marks[N] — the same convention the Lab's synthesis writes
    into `applicability.legs`, both sides flatten the course with race_def.course_to_marks).
    None when unknown (no course / no fix) — leg gating fails open."""
    try:
        nav = navigator.get_navigator(route)
        if not nav.get("available"):
            return None
        idx = (nav.get("next_mark") or {}).get("index")
        return idx if isinstance(idx, int) and idx >= 1 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------- signal gather

def _live_tws_kn():
    # latest_value returns the raw SI scalar (see navigator._latest) — tolerate a (value, ts) tuple
    # defensively since both shapes exist in test stubs
    try:
        v = datasource.active().latest_value("environment.wind.speedTrue")
        if isinstance(v, (tuple, list)):
            v = v[0] if v else None
        if v is not None:
            return round(float(v) * 1.943844, 1)
    except Exception:
        pass
    return None


def gather(route=None):
    """One read of every signal the play predicates reference. Each source module is fuzzy /
    hysteretic already and idempotent per poll; a source that's unavailable contributes None (its
    predicates simply can't hold)."""
    dev = deviation.get_deviation(route)
    dft = drift_mod.get_drift(route)
    tac = tactics.get_tactics(route)
    try:
        fat = fatigue.get_fatigue()
    except Exception:
        fat = {}
    st = get_sail_state()
    try:
        buo = buoys.get_buoys(route)
    except Exception:
        buo = {}
    upc = (buo.get("upcourse") or {}) if buo.get("available") else {}
    dev_ok, dft_ok = dev.get("available"), dft.get("available")
    sign = _DRIFT_SIGN.get(dft.get("drift_dir")) if dft_ok else None
    deg = dft.get("drift_twd_deg") if dft_ok else None
    wind = (tac.get("wind") or {}) if tac.get("available") else {}
    tb = dev.get("time_behind_s") if dev_ok else None
    return {
        "time_behind_min": round(tb / 60.0, 1) if isinstance(tb, (int, float)) else None,
        "xte_nm": abs(dev.get("xte_nm")) if (dev_ok and dev.get("xte_nm") is not None) else None,
        "drift_twd_deg": deg,
        "drift_twd_signed_deg": (sign * deg if (sign and isinstance(deg, (int, float))) else None),
        "drift_tws_kn": dft.get("drift_tws_kn") if dft_ok else None,
        "shift_persistent": bool(wind.get("persistent")) if wind else None,
        "tws_kn": _live_tws_kn(),
        "fatigue_index": fat.get("index") if isinstance(fat, dict) else None,
        # the flying SET — `==` predicates use membership on a list actual, so a play authored
        # "hoisted_sail == C0" arms whenever the C0 is among the flying combination
        "hoisted_sail": (st.get("flying") if st.get("flying") is not None
                         else ([st["hoisted"]] if st.get("hoisted") else [])),
        "reef": st.get("reef"),
        "sail_out_of_service": st.get("out_of_service") or [],
        # the up-course leading indicator (live NDBC buoy vs own wind; common public data)
        "upcourse_tws_delta_kn": upc.get("tws_delta_kn"),
        "upcourse_twd_shift_deg": upc.get("twd_shift_deg"),
        "_upcourse_name": upc.get("name") or upc.get("station"),   # crew-facing corroborator label
        "polar_pct": _polar_pct(),
        "current_leg": _current_leg(route),
    }


# ---------------------------------------------------------------------------- predicate evaluation

def _pred_ok(op, actual, value):
    if actual is None:
        return False
    try:
        if op == ">=":
            return float(actual) >= float(value)
        if op == "<=":
            return float(actual) <= float(value)
        if op == "==":
            if isinstance(actual, list):            # sail_out_of_service is a set of sails
                return str(value).upper() in [str(a).upper() for a in actual]
            if isinstance(actual, bool) or isinstance(value, bool):
                return bool(actual) == bool(value)
            return str(actual).upper() == str(value).upper()
    except (TypeError, ValueError):
        return False
    return False


def _evaluate(play, signals, now):
    """One play against the live signals with the sustain/clear-fast discipline. Returns the
    crew/Tier-2-facing record (no heavy route payload — the bundle aboard has it)."""
    preds = ((play.get("conditions") or {}).get("predicates")) or []
    rows, all_ok = [], bool(preds)
    # LEG GATE — a hard-gated play (pace: the response is a re-route FROM its mark) only arms on
    # its applicable leg(s). Participates in the Schmitt like a predicate (off-leg clears fast);
    # fails OPEN when the current leg is unknown or the applicability is advisory / "all".
    applic = play.get("applicability") or {}
    legs = applic.get("legs")
    hard = (applic.get("gate") == "hard"
            or (applic.get("gate") is None and (play.get("scenario") or {}).get("kind") == "pace"))
    applicable = True
    if hard and isinstance(legs, list) and legs:
        cur = signals.get("current_leg")
        if cur is not None:
            applicable = cur in legs
    all_ok = all_ok and applicable
    sustain_min = 0.0
    for p in preds:
        actual = signals.get(p.get("signal"))
        ok = _pred_ok(p.get("op"), actual, p.get("value"))
        all_ok = all_ok and ok
        sustain_min = max(sustain_min, float(p.get("sustain_min") or 0))
        rows.append({"signal": p.get("signal"), "op": p.get("op"), "value": p.get("value"),
                     "actual": actual, "ok": ok})
    st = _ST.setdefault(str(play.get("id")), {})
    if all_ok:
        st.setdefault("since", now)
        held_s = now - st["since"]
        armed = held_s >= sustain_min * 60.0 * SUSTAIN_SCALE
        if armed and not st.get("armed_at"):
            st["armed_at"] = now
        status = "armed" if armed else "arming"
    else:                                            # clear fast
        st.pop("since", None)
        st.pop("armed_at", None)
        status = "quiet"
        held_s = 0.0
    # CORROBORATORS (2026-07-08): confidence-raising signals that never gate — e.g. the up-course
    # buoy already reading the play's breeze. Evaluated like predicates but with NO effect on the
    # armed status (AND semantics would let a dark buoy block a real play).
    corr_rows, corroborated = [], False
    for c in ((play.get("conditions") or {}).get("corroborators")) or []:
        actual = signals.get(c.get("signal"))
        cok = _pred_ok(c.get("op"), actual, c.get("value"))
        corroborated = corroborated or cok
        corr_rows.append({"signal": c.get("signal"), "op": c.get("op"), "value": c.get("value"),
                          "actual": actual, "ok": cok, "why": c.get("why")})
    corroborated_by = None
    if corroborated:
        src = next((r for r in corr_rows if r["ok"]), {})
        upname = signals.get("_upcourse_name")
        if str(src.get("signal", "")).startswith("upcourse") and upname:
            corroborated_by = f"up-course buoy {upname}"
        else:
            corroborated_by = src.get("signal")
    resp = play.get("response") or {}
    return {
        "id": play.get("id"), "name": play.get("name"), "category": play.get("category"),
        "kind": (play.get("scenario") or {}).get("kind"),
        "status": status, "held_s": round(held_s),
        "sustain_min": sustain_min,
        "predicates": rows,
        **({"corroborators": corr_rows} if corr_rows else {}),
        "corroborated": corroborated,
        "corroborated_by": corroborated_by,
        "summary": play.get("summary") or "",
        "guidance": resp.get("guidance"),
        "response_type": resp.get("type"),
        "stakes_min": play.get("stakes_min"),
        "applicability": play.get("applicability"),
        "applicable": applicable,       # False = held quiet by the leg gate (off its leg)
        "what_flips_it": play.get("what_flips_it") or "",
    }


_ORDER = {"armed": 0, "arming": 1, "quiet": 2}


def get_plays(route=None):
    """The matcher read: every play in the frozen v2 bundle evaluated against the live signals,
    armed first. `na` with no v2 playbook aboard. Deterministic — works with no Orin."""
    bundle = deviation._load_playbook()
    if not bundle:
        return {"available": False, "note": "no playbook aboard"}
    plays = bundle.get("plays") or []
    if not plays:
        return {"available": False,
                "note": "playbook aboard has no plays (a v1 bundle) — synthesize a v2 playbook"}
    now = time.time()
    signals = gather(route)
    out = [_evaluate(p, signals, now) for p in plays]
    out.sort(key=lambda r: (_ORDER.get(r["status"], 3), -(r.get("stakes_min") or 0)))
    armed = [r["id"] for r in out if r["status"] == "armed"]
    arming = [r["id"] for r in out if r["status"] == "arming"]
    return {"available": True, "plays": out, "armed": armed, "arming": arming,
            "n_plays": len(out), "signals": signals, "sail_state": get_sail_state(),
            "based": ["get_deviation", "get_drift", "get_tactics", "get_fatigue", "sail_state",
                      "get_buoys"],
            "conf": "engine",
            "disclaimer": ("Deterministic condition-matching of the boat's own signals against the "
                           "frozen playbook — the crew judges; an armed play is a pointer, not an "
                           "order.")}
