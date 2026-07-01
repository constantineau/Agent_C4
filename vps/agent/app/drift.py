"""Forecast-drift — has the common forecast the playbook rests on MOVED since it was frozen?

Lab-3 onboard executor, branch trigger (b) — the companion to route-deviation (a, `deviation.py`).
The frozen bundle carries a `forecast_fingerprint`: the Open-Meteo forecast the plan was built on,
sampled along the route at each waypoint's ETA (see `vps/lab/app/forecast_ref.py`). Onboard, this
re-samples the SAME common source (Open-Meteo via `weather.wind_at` — the legal common-data forecast
already served at `/forecast`) for the SAME (place, target-time) and reports how far the forecast has
DRIFTED: the directional shift (veered/backed) + the speed change, aggregated over the still-future
waypoints, with fuzzy Schmitt consider/commit bands (perflab §5). A large sustained drift means the
wind picture the recommended variant was optimized for has changed → reconsider the plan.

Same-source by design (Open-Meteo both ends) so the signal is genuine forecast EVOLUTION, not a
cross-model offset. Deterministic, TIER-1 (own computer + common public data, frozen homework), legal
in-race. It FLAGS drift and says which way; re-optimizing the route onboard is a later slice. No LLM.
"""
import os
import time

from . import deviation, weather

# fuzzy Schmitt bands (perflab §5a) — a directional consider/commit band + a speed band.
TWD_CONSIDER = float(os.environ.get("DRIFT_TWD_CONSIDER_DEG", "15"))   # forecast wind-dir drift
TWD_COMMIT = float(os.environ.get("DRIFT_TWD_COMMIT_DEG", "30"))
TWS_CONSIDER = float(os.environ.get("DRIFT_TWS_CONSIDER_KN", "4"))     # forecast wind-speed drift
TWS_COMMIT = float(os.environ.get("DRIFT_TWS_COMMIT_KN", "8"))
MIN_LEAD_S = float(os.environ.get("DRIFT_MIN_LEAD_S", "600"))          # ignore already-here waypoints
DIR_TOL_DEG = float(os.environ.get("DRIFT_DIR_TOL_DEG", "5"))          # veered/backed vs "shifted"

_band = deviation._band          # reuse the exact Schmitt double-band + hysteresis
_BANDS = deviation._BANDS
_state: dict = {}                # per-race hysteresis memory (a Schmitt trigger remembers)


def reset_state(key=None):
    if key is None:
        _state.clear()
    else:
        _state.pop(key, None)


def _ang(a, b):
    """Signed shortest angle from a to b (deg), + = b is clockwise (veered) of a."""
    return ((b - a + 180) % 360) - 180


def _na(note, extra=None):
    out = {"available": False, "status": "na", "value": "—", "sub": note, "why": note,
           "consider": "—", "clears": "—", "based": [], "conf": "engine"}
    if extra:
        out.update(extra)
    return out


def get_drift(route=None):
    """Forecast-drift tile payload. Compares the live common forecast to the bundle's frozen
    reference at each still-future route waypoint. `na` with no playbook / no reference / nothing
    comparable (all ETAs passed, or the forecast is unreachable)."""
    bundle = deviation._load_playbook()
    if not bundle:
        return _na("no playbook aboard")
    fp = bundle.get("forecast_fingerprint") or {}
    pts = fp.get("points") or []
    if len(pts) < 2:
        return _na("no forecast reference in the playbook (re-freeze to enable drift)",
                   {"race_id": bundle.get("race_id")})

    now = time.time()
    diffs = []          # (dtwd_signed, dtws_signed)
    n_future = 0
    worst = None
    for p in pts:
        t = p.get("t")
        if t is None or t < now + MIN_LEAD_S:      # spent / imminent — the plan for it is done
            continue
        n_future += 1
        live = weather.wind_at(p["lat"], p["lon"], t)
        if live is None:                            # beyond horizon / no network
            continue
        dtwd = _ang(p["twd"], live[1])          # live = (tws_kn, twd_deg)
        dtws = live[0] - p["tws"]
        diffs.append((dtwd, dtws))
        if worst is None or abs(dtwd) > abs(worst["dtwd"]):
            worst = {"dtwd": round(dtwd), "dtws": round(dtws, 1),
                     "in_h": round((t - now) / 3600, 1)}

    base = {"race_id": bundle.get("race_id"),
            "built_ago_s": (now - fp["built_at"]) if fp.get("built_at") else None,
            "source": fp.get("source")}
    if not diffs:
        if n_future == 0:
            return _na("all forecast-reference waypoints are in the past — drift no longer meaningful", base)
        return _na("forecast unreachable / beyond horizon — can't measure drift right now", base)

    mean_abs_twd = sum(abs(d[0]) for d in diffs) / len(diffs)
    max_abs_twd = max(abs(d[0]) for d in diffs)
    mean_signed_twd = sum(d[0] for d in diffs) / len(diffs)
    mean_tws = sum(d[1] for d in diffs) / len(diffs)

    key = str(bundle.get("race_id") or route or "race")
    st = _state.get(key, {})
    twd_b = _band(mean_abs_twd, st.get("twd_band", 0), TWD_CONSIDER, TWD_COMMIT)
    tws_b = _band(abs(mean_tws), st.get("tws_band", 0), TWS_CONSIDER, TWS_COMMIT)
    _state[key] = {"twd_band": twd_b, "tws_band": tws_b}
    band = max(twd_b, tws_b)
    status = _BANDS[band]

    direction = ("veered" if mean_signed_twd > DIR_TOL_DEG else
                 "backed" if mean_signed_twd < -DIR_TOL_DEG else "shifted")
    twd_txt = f"{round(mean_abs_twd)}° {direction}"
    tws_txt = (f"{'+' if mean_tws >= 0 else '−'}{abs(round(mean_tws,1))} kn"
               if abs(mean_tws) >= 1 else None)
    based = ["playbook:forecast_fingerprint", "Open-Meteo live (common data)"]

    if band == 0:
        value = "Forecast holding"
        sub = f"{round(mean_abs_twd)}° drift · {len(diffs)} pts"
        why = (f"The live forecast is within ~{round(mean_abs_twd)}° of what the plan was built on "
               f"across {len(diffs)} route waypoints" + (f" (speed {tws_txt})" if tws_txt else "")
               + " — the wind picture the recommended variant assumes still holds.")
        consider = "No forecast drift — the plan's assumptions still stand."
        clears = "—"
    else:
        lead = "Forecast moved" if band == 2 else "Forecast drifting"
        value = f"{lead} · {twd_txt}"
        sub = f"{twd_txt}" + (f" · {tws_txt}" if tws_txt else "") + f" · max {round(max_abs_twd)}°"
        why = (f"Since the plan was frozen the common forecast has {direction} ~{round(mean_abs_twd)}° "
               f"(worst {worst['dtwd']:+d}° ~{worst['in_h']} h out)"
               + (f" and changed {tws_txt} in strength" if tws_txt else "")
               + (". This is a material move — the recommended variant was optimized for a different "
                  "wind picture." if band == 2 else ". Keep watching — it may firm up into a real change."))
        consider = ("Reassess whether the recommended variant still pays — the forecast it was built "
                    "on has moved. (Onboard re-optimize is not automatic; this is a heads-up.)"
                    if band == 2 else
                    "Forecast is drifting from the plan — watch it; be ready to revisit the variant.")
        clears = "the forecast settles back toward what the plan assumed"

    return {
        "available": True, "status": status, "value": value, "sub": sub,
        "why": why, "consider": consider, "clears": clears, "based": based, "conf": "engine",
        "drift_twd_deg": round(mean_abs_twd, 1), "drift_twd_max_deg": round(max_abs_twd, 1),
        "drift_twd_signed_deg": round(mean_signed_twd, 1), "drift_dir": direction,
        "drift_tws_kn": round(mean_tws, 1), "n_points": len(diffs), "n_future": n_future,
        "worst": worst, "built_ago_s": (round(now - fp["built_at"]) if fp.get("built_at") else None),
        "source": fp.get("source"), "race_id": bundle.get("race_id"),
    }
