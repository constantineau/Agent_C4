"""Which weather the debrief's best route used, and how old it was (#404).

The debrief's hindsight route is only as good as the wind it routes through. Three kinds:
  - "analyses": each hour's own 0-h analysis, chained (`wind.archive.analysis_field`). The
    closest the archive gets to the wind that actually blew.
  - "archive":  the forecast cycle that was out at the gun (`wind.archive.gun_sources`). That is
    a forecast, so it can't show forecast error: the plan was made on much the same thing.
  - "live":     the latest forecast the live sources serve.

`describe` turns a wind field and its kind into what the report and the Lab show: per model the
cycle and its age over the race, and per race hour each model's age (None = no frame near).
"""
import datetime as dt
import math

from .wind.archive import ANALYSIS_CYCLE

LABELS = {"analyses": "Hourly analyses",
          "archive": "Forecast from the start",
          "live": "Latest forecast"}
NOTES = {"analyses": "Each hour's own analysis: the wind that blew, as near as the models know.",
         "archive": "The forecast out at the start, not the wind that blew. Forecast error "
                    "can't show: the plan used much the same forecast.",
         "live": "The latest forecast, not the wind that blew."}
NEAR_S = 3 * 3600          # a model "covers" an hour with a frame this close (the field holds edges)


def _cycle_epoch(s):
    """'2026-07-18 12Z' → epoch, or None (an analysis chain, a skipped model, junk)."""
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %HZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _times(wf):
    """model → sorted frame valid times (all members together)."""
    out = {}
    for key, frames in (getattr(wf, "series", None) or {}).items():
        model = key[0] if isinstance(key, tuple) else key
        out.setdefault(model, []).extend(getattr(f, "valid_time", None) for f in frames or [])
    return {m: sorted(t for t in ts if t is not None) for m, ts in out.items()}


def _age_h(t, cyc, times):
    """Age (h) at `t` of a model's wind, or None when no frame is near. A forecast: hours since
    its cycle. An analysis chain: hours to the nearest analysis."""
    if not times:
        return None
    near = min(times, key=lambda v: abs(v - t))
    if abs(near - t) > NEAR_S:
        return None
    if cyc is None:
        return round(abs(near - t) / 3600.0, 1)
    return round(max(0.0, (t - cyc) / 3600.0), 1)


def _when(cyc):
    d = dt.datetime.fromtimestamp(cyc, dt.timezone.utc)
    return f"{d:%b} {d.day} {d:%H}Z"


def describe(wf, kind, start, t_end):
    """The report's `oracle_weather` block. Safe on a stub field (no meta/series): models empty."""
    kind = kind if kind in LABELS else "live"
    times = _times(wf)
    rows = []
    for m in getattr(wf, "meta", None) or []:
        if not isinstance(m, dict) or m.get("skipped") or not m.get("frames"):
            continue
        rows.append((m.get("model"), m.get("cycle"),
                     None if m.get("cycle") == ANALYSIS_CYCLE else _cycle_epoch(m.get("cycle"))))
    n_h = max(0, int(math.ceil((float(t_end) - float(start)) / 3600.0)))
    hourly = []
    for h in range(n_h + 1):
        t = float(start) + h * 3600
        hourly.append({"h": h, "t": round(t),
                       "age_h": {name: _age_h(t, cyc, times.get(name)) for name, _, cyc in rows}})
    models = []
    for name, cycle, cyc in rows:
        ages = [r["age_h"][name] for r in hourly if r["age_h"].get(name) is not None]
        models.append({"model": name, "cycle": cycle,
                       "analysis": cycle == ANALYSIS_CYCLE,
                       "age_min_h": min(ages) if ages else None,
                       "age_max_h": max(ages) if ages else None,
                       "hours_covered": len(ages)})
    # with no model listed (a stub field) there is nothing to say about coverage
    uncovered = sum(1 for r in hourly if all(v is None for v in r["age_h"].values())) if rows else 0
    return {"kind": kind, "label": LABELS[kind], "note": NOTES[kind], "models": models,
            "hourly": hourly, "hours": len(hourly), "hours_uncovered": uncovered,
            "summary": summary(kind, models, uncovered, len(hourly))}


def summary(kind, models, uncovered=0, hours=0):
    """One plain line: "Forecast from the start: GFS Jul 18 12Z (3–33 h old), HRRR …"."""
    bits = []
    for m in models:
        name = (m.get("model") or "?").upper()
        if m.get("analysis"):
            bits.append(f"{name} analyses")
            continue
        cyc = _cycle_epoch(m.get("cycle"))
        when = _when(cyc) if cyc else (m.get("cycle") or "?")
        lo, hi = m.get("age_min_h"), m.get("age_max_h")
        age = ("" if lo is None else
               f" ({lo:.0f} h old)" if round(lo) == round(hi) else f" ({lo:.0f}–{hi:.0f} h old)")
        bits.append(f"{name} {when}{age}")
    line = LABELS.get(kind, kind) + (": " + ", ".join(bits) if bits else "")
    if uncovered:
        line += f". No wind for {uncovered} of {hours} h"
    return line + "."
