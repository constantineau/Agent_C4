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
  hoisted_sail · sail_out_of_service — the crew-set SAIL STATE (the console's hoisted selector +
                                       the Phase-D out-of-service toggle; there is no instrument
                                       for a blown kite — the crew arms it)
  polar_pct                         — not yet wired onboard (None → the sea_state play stays quiet)

HONEST v1 LIMIT: `applicability.legs` (a pace play pinned to its mark) is carried in the payload
for the crew/Tier-2 but NOT enforced as a gate — the pace predicates are global (time-behind), so
the worst case is a play arming a leg early, clearly labelled with its mark.

The recommendation stays the deterministic selector's; the matcher ARMS plays — the Tier-2 LLM (and
the crew) read the armed set. The auto-coach volunteers a callout when a play newly arms.
"""
from __future__ import annotations

import os
import time

from . import buoys
from . import datasource
from . import deviation
from . import drift as drift_mod
from . import fatigue
from . import tactics

# sustain windows come from each play's predicates (minutes). Scale for bench/tests (0 = instant).
SUSTAIN_SCALE = float(os.environ.get("MATCHER_SUSTAIN_SCALE", "1.0"))

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


def set_sail_state(hoisted=None, out_of_service=None):
    """Update the crew sail state. Pass only what changed: hoisted='A3' (or '' to clear);
    out_of_service=['A2'] replaces the out list (a sail present = crew declared it unusable —
    the gear-loss plays' arming signal). Returns the stored state."""
    st = get_sail_state()
    if hoisted is not None:
        st["hoisted"] = (str(hoisted).upper() or None) if hoisted != "" else None
    if out_of_service is not None:
        st["out_of_service"] = sorted({str(s).upper() for s in out_of_service if s})
    st["ts"] = round(time.time())
    datasource.active().save_sail_state(st)
    return st


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
        "hoisted_sail": st.get("hoisted"),
        "sail_out_of_service": st.get("out_of_service") or [],
        # the up-course leading indicator (live NDBC buoy vs own wind; common public data)
        "upcourse_tws_delta_kn": upc.get("tws_delta_kn"),
        "upcourse_twd_shift_deg": upc.get("twd_shift_deg"),
        "polar_pct": None,          # not wired onboard yet — the sea_state play stays quiet
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
    resp = play.get("response") or {}
    return {
        "id": play.get("id"), "name": play.get("name"), "category": play.get("category"),
        "kind": (play.get("scenario") or {}).get("kind"),
        "status": status, "held_s": round(held_s),
        "sustain_min": sustain_min,
        "predicates": rows,
        "summary": play.get("summary") or "",
        "guidance": resp.get("guidance"),
        "response_type": resp.get("type"),
        "stakes_min": play.get("stakes_min"),
        "applicability": play.get("applicability"),
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
