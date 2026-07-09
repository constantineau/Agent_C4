"""PLAN GAP — is the breeze the boat actually HAS the one the plan PROMISED for here, now?

The companion to `drift.py`, closing its deliberate blind spot: drift compares the live forecast
to the frozen forecast (same-source evolution — it stays quiet when the real wind busts but the
forecast hasn't caught up). This module compares the boat's OWN OBSERVED wind to the bundle's
frozen `forecast_fingerprint`, time-interpolated to "now" — the promise the recommended variant
was optimized around. A large sustained gap means the plan is running on weather that didn't
show up, whatever the current forecast claims.

Observed wind = a short windowed mean of own instruments (`PLANGAP_WINDOW_MIN`, TWD circular) so
a single gust or a tack can't fake a gap; instantaneous fallback when the archive is thin. The
promise = the fingerprint bracketing pair around now (TWD via shortest angle); before the first
point it clamps forward up to `PLANGAP_CLAMP_LEAD_H` (pre-start: the promise for the start), and
it goes `na` once the fingerprint timeline is spent. Position honesty: the gap is reported with
the boat's distance from where the plan expected it — far off-plan, part of the gap can be
geography, and the payload says so rather than overclaiming a forecast bust.

Same fuzzy Schmitt consider/commit bands as drift (`PLANGAP_*` tunables). Matcher signals:
`plangap_twd_deg` (abs), `plangap_twd_signed_deg` (+ = observed RIGHT of promised),
`plangap_tws_kn` (signed, observed − promised). TIER-1: own instruments + frozen homework,
legal in-race. No LLM.
"""
from __future__ import annotations

import math
import os
import time

from . import datasource, deviation, navigator

TWD_CONSIDER = float(os.environ.get("PLANGAP_TWD_CONSIDER_DEG", "15"))
TWD_COMMIT = float(os.environ.get("PLANGAP_TWD_COMMIT_DEG", "30"))
TWS_CONSIDER = float(os.environ.get("PLANGAP_TWS_CONSIDER_KN", "4"))
TWS_COMMIT = float(os.environ.get("PLANGAP_TWS_COMMIT_KN", "8"))
WINDOW_MIN = float(os.environ.get("PLANGAP_WINDOW_MIN", "20"))
MIN_SAMPLES = int(os.environ.get("PLANGAP_MIN_SAMPLES", "5"))
CLAMP_LEAD_H = float(os.environ.get("PLANGAP_CLAMP_LEAD_H", "6"))
FAR_NM = float(os.environ.get("PLANGAP_FAR_NM", "15"))

_band = deviation._band
_BANDS = deviation._BANDS
_state: dict = {}

_MS_TO_KN = 1.943844
_RAD_TO_DEG = 57.29577951308232


def reset_state(key=None):
    if key is None:
        _state.clear()
    else:
        _state.pop(key, None)


def _ang(a, b):
    return ((b - a + 180) % 360) - 180


def _na(note, extra=None):
    out = {"available": False, "status": "na", "value": "—", "sub": note, "why": note,
           "consider": "—", "clears": "—", "based": [], "conf": "engine"}
    if extra:
        out.update(extra)
    return out


def _observed_wind():
    """(tws_kn, twd_deg, n_samples) — windowed mean of own instruments, TWD circular;
    instantaneous fallback. (None, None, 0) when the boat has no wind data at all."""
    src = datasource.active()
    try:
        tws_rows = src.series("environment.wind.speedTrue", WINDOW_MIN) or []
        twd_rows = src.series("environment.wind.directionTrue", WINDOW_MIN) or []
    except Exception:
        tws_rows, twd_rows = [], []
    tws_v = [v for _, v in tws_rows if v is not None]
    twd_v = [v for _, v in twd_rows if v is not None]
    if len(tws_v) >= MIN_SAMPLES and len(twd_v) >= MIN_SAMPLES:
        x = sum(math.cos(v) for v in twd_v)
        y = sum(math.sin(v) for v in twd_v)
        twd = math.degrees(math.atan2(y, x)) % 360
        return (sum(tws_v) / len(tws_v) * _MS_TO_KN, twd, min(len(tws_v), len(twd_v)))
    try:
        w = src.latest_value("environment.wind.speedTrue")
        d = src.latest_value("environment.wind.directionTrue")
        w = w[0] if isinstance(w, (tuple, list)) else w
        d = d[0] if isinstance(d, (tuple, list)) else d
        if w is not None and d is not None:
            return (float(w) * _MS_TO_KN, float(d) * _RAD_TO_DEG % 360, 1)
    except Exception:
        pass
    return (None, None, 0)


def _promise_at(pts, now):
    """The fingerprint's promised (tws_kn, twd_deg, lat, lon) time-interpolated to `now`.
    None when the timeline is spent (or hasn't begun within the clamp window)."""
    pts = sorted((p for p in pts if p.get("t") is not None), key=lambda p: p["t"])
    if not pts:
        return None
    if now < pts[0]["t"]:
        return dict(pts[0]) if pts[0]["t"] - now <= CLAMP_LEAD_H * 3600 else None
    if now > pts[-1]["t"]:
        return None
    for a, b in zip(pts, pts[1:]):
        if a["t"] <= now <= b["t"]:
            span = b["t"] - a["t"]
            f = (now - a["t"]) / span if span else 0.0
            return {"tws": a["tws"] + f * (b["tws"] - a["tws"]),
                    "twd": (a["twd"] + f * _ang(a["twd"], b["twd"])) % 360,
                    "lat": a["lat"] + f * (b["lat"] - a["lat"]),
                    "lon": a["lon"] + f * (b["lon"] - a["lon"]),
                    "t": now}
    return dict(pts[-1])


def get_plangap(route=None):
    """Observed-vs-plan wind gap payload (drift-shaped tile fields + signed gap numbers).
    `na` with no playbook / no fingerprint / no own wind / a spent plan timeline."""
    bundle = deviation._load_playbook()
    if not bundle:
        return _na("no playbook aboard")
    fp = bundle.get("forecast_fingerprint") or {}
    pts = fp.get("points") or []
    if len(pts) < 2:
        return _na("no forecast reference in the playbook (re-freeze to enable the plan-gap read)",
                   {"race_id": bundle.get("race_id")})

    now = time.time()
    base = {"race_id": bundle.get("race_id"), "source": fp.get("source"),
            "built_ago_s": (round(now - fp["built_at"]) if fp.get("built_at") else None)}
    promise = _promise_at(pts, now)
    if promise is None:
        return _na("the plan's forecast timeline is spent (or too far ahead) — no promise to "
                   "compare against", base)

    obs_tws, obs_twd, n = _observed_wind()
    if obs_tws is None:
        return _na("no own wind data — can't measure the gap", base)

    gap_twd = _ang(promise["twd"], obs_twd)         # + = observed RIGHT of the promise
    gap_tws = obs_tws - promise["tws"]

    key = str(bundle.get("race_id") or route or "race")
    st = _state.get(key, {})
    twd_b = _band(abs(gap_twd), st.get("twd_band", 0), TWD_CONSIDER, TWD_COMMIT)
    tws_b = _band(abs(gap_tws), st.get("tws_band", 0), TWS_CONSIDER, TWS_COMMIT)
    _state[key] = {"twd_band": twd_b, "tws_band": tws_b}
    band = max(twd_b, tws_b)
    status = _BANDS[band]

    # position honesty: how far is the boat from where the plan expected it right now?
    far_nm = None
    s = navigator._latest()
    if s.get("lat") is not None and s.get("lon") is not None:
        far_nm = round(deviation._hav_nm(s["lat"], s["lon"], promise["lat"], promise["lon"]), 1)
    far_txt = (f" (the plan expected you ~{far_nm:g} nm from here — part of this can be "
               "position, not weather)" if far_nm is not None and far_nm > FAR_NM else "")

    side = "right of" if gap_twd > 5 else "left of" if gap_twd < -5 else "on"
    twd_txt = f"breeze {abs(round(gap_twd))}° {side} the promise" if side != "on" else "direction on the promise"
    tws_txt = (f"{'+' if gap_tws >= 0 else '−'}{abs(round(gap_tws, 1)):g} kn vs promised"
               if abs(gap_tws) >= 1 else None)
    based = ["playbook:forecast_fingerprint", "own wind instruments"]

    if band == 0:
        value = "Plan's breeze showed up"
        sub = f"promised {round(promise['twd'])}°/{round(promise['tws'], 1):g} kn · have {round(obs_twd)}°/{round(obs_tws, 1):g} kn"
        why = (f"You have {round(obs_tws, 1):g} kn at {round(obs_twd)}° against a promise of "
               f"{round(promise['tws'], 1):g} kn at {round(promise['twd'])}° for here/now — the plan is "
               "running on the weather it was built for.")
        consider = "The promised breeze is the one you have — the plan's assumptions hold on deck."
        clears = "—"
    else:
        lead = "Promised breeze is NOT this one" if band == 2 else "Breeze straying from the promise"
        value = f"{lead}"
        sub = (f"promised {round(promise['twd'])}°/{round(promise['tws'], 1):g} kn · "
               f"have {round(obs_twd)}°/{round(obs_tws, 1):g} kn")
        why = (f"The plan promised {round(promise['tws'], 1):g} kn at {round(promise['twd'])}° for "
               f"here/now; you have {round(obs_tws, 1):g} kn at {round(obs_twd)}° — {twd_txt}"
               + (f", {tws_txt}" if tws_txt else "") + f".{far_txt}"
               + (" The wind the variant was optimized for hasn't shown up — whatever the live "
                  "forecast says." if band == 2 else " Keep watching — this may be a local hole/puff."))
        consider = ("Treat the plan's wind assumptions as broken on deck — weigh the drift and "
                    "deviation reads together and consider the playbook's pace/pressure plays."
                    if band == 2 else
                    "Own wind is straying from what the plan promised — watch it against the drift read.")
        clears = "own wind settles back onto what the plan promised"

    return {
        "available": True, "status": status, "value": value, "sub": sub,
        "why": why, "consider": consider, "clears": clears, "based": based, "conf": "engine",
        "plan_twd": round(promise["twd"]) % 360, "plan_tws_kn": round(promise["tws"], 1),
        "obs_twd": round(obs_twd) % 360, "obs_tws_kn": round(obs_tws, 1),
        "gap_twd_signed_deg": round(gap_twd, 1), "gap_twd_deg": round(abs(gap_twd), 1),
        "gap_tws_kn": round(gap_tws, 1), "n_samples": n,
        "plan_pos_off_nm": far_nm,
        **base,
    }
