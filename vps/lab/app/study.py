"""Gameplan "Study" batch (#378): route the same course many times, one thing changed at a time.

Axes (each optional): every weather model on its own, every ensemble run on its own, start-time
offsets, boat speed % (the polar), wind speed % and wind direction ± degrees. The plan varies ONE
axis at a time around the baseline (the Gameplan's own settings), so the run count is
1 + the values on each axis, never a cross product, and is capped.

The weather, current, sea state and chart are loaded ONCE for the widest window the plan needs;
each run routes a view of that one wind field (`subset` for a model or ensemble run alone,
`Nudged` for a speed or direction change) through `routerun.run_course`, the Gameplan's own route
run. Runs one after another inside the optimizer's background job (single-flight with Optimize).

Each model or ensemble run routed alone carries its model, cycle and blend weight: routing them
apart is the check on the blend, which can smear a front into weather no model predicts (#380).
How many runs take each side is AGREEMENT, not accuracy, and the result says so.

Pure: no FastAPI, no labstate. The caller picks the boat and the chart.
"""
from __future__ import annotations

import copy
import math
import time

from . import optimizer, routerun

MAX_RUNS = 40                 # hard ceiling, whatever the caller asks
DEFAULT_CAP = 24
# Two routes closer than this many minutes count as a tie. A rough constant for now: the
# optimizer validation suite (#407) is to measure the router's own noise and replace it.
TIE_MIN = 5.0
TIE_NOTE = "Within about {m:g} min counts as a tie. Rough: the router's own noise isn't measured yet."
AGREEMENT_NOTE = "Agreement, not accuracy: models can all be wrong together."
SIDE_DEG = 10.0               # first heading this far off the rhumb line = a side (as the model fan)
PATH_MAX_PTS = 200            # each run's track is thinned to this for the map

# axis key -> (lo, hi, baseline value dropped from the list)
_LIMITS = {"start_min": (-720, 720, 0), "polar_pct": (50, 150, 100),
           "tws_pct": (50, 200, 100), "twd_deg": (-90, 90, 0)}


def _nums(raw, key):
    """A list (or comma/space text) of numbers → sorted unique ints inside the axis limits, the
    baseline value dropped (it is the baseline run)."""
    lo, hi, base = _LIMITS[key]
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    out = set()
    for x in raw or []:
        try:
            v = int(round(float(x)))
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi and v != base:
            out.add(v)
    return sorted(out)


def parse_axes(body):
    """The request's axes, cleaned: {"models": bool, "members": bool, "start_min": [...],
    "polar_pct": [...], "tws_pct": [...], "twd_deg": [...]}."""
    b = body or {}
    out = {"models": bool(b.get("models")), "members": bool(b.get("members"))}
    for k in _LIMITS:
        out[k] = _nums(b.get(k), k)
    return out


def run_count(axes, n_models, n_members):
    """How many runs a plan makes: the baseline + each value on each axis. `n_models` = models in
    the blend (the model axis needs two or more), `n_members` = ensemble runs loaded."""
    n = 1
    if axes.get("models") and n_models >= 2:
        n += n_models
    if axes.get("members") and n_members >= 1:
        n += n_members
    return n + sum(len(axes.get(k) or []) for k in _LIMITS)


def label(axis, value):
    """The row's name, short and plain."""
    if axis == "start_min":
        return f"Start {value:+d} min"
    if axis == "polar_pct":
        return f"Boat {value}%"
    if axis == "tws_pct":
        return f"Wind {value}%"
    if axis == "twd_deg":
        return f"Wind {abs(value)}° {'right' if value > 0 else 'left'}"
    return str(value)


def plan(axes, model_keys=(), member_keys=()):
    """The runs, baseline first. `model_keys` = the models in the loaded field; `member_keys` =
    the (model, member) ensemble runs in it."""
    runs = [{"id": "base", "axis": "base", "label": "Baseline"}]
    if axes.get("models") and len(model_keys) >= 2:
        runs += [{"id": f"model:{m}", "axis": "model", "label": f"{m.upper()} alone", "model": m}
                 for m in model_keys]
    if axes.get("members"):
        runs += [{"id": f"member:{m}:{e}", "axis": "member", "label": f"{m.upper()} {e}",
                  "model": m, "member": e} for m, e in member_keys]
    for k in _LIMITS:
        runs += [{"id": f"{k}:{v}", "axis": k, "label": label(k, v), k: v}
                 for v in axes.get(k) or []]
    return runs


# ---- views of one wind field -------------------------------------------------------------------
def subset(wf, keep):
    """The same wind field holding only the series `keep((model, member))` accepts (a model or an
    ensemble run alone). Shares the frames; corrections and de-bias ride along."""
    v = copy.copy(wf)
    v.series = {k: fr for k, fr in wf.series.items() if keep(k)}
    names = {k[0] for k in v.series}
    v.meta = [m for m in wf.meta if m.get("model") in names]
    return v


class Nudged:
    """A wind field with every wind sped up/down by `tws_scale` and turned `twd_shift` degrees
    (+ = shifted right, clockwise). Everything else passes through."""

    def __init__(self, wf, tws_scale=1.0, twd_shift=0.0):
        self._wf = wf
        self.tws_scale = float(tws_scale)
        self.twd_shift = float(twd_shift)

    def __getattr__(self, name):
        return getattr(self._wf, name)

    def detail_at(self, lat, lon, epoch):
        d = self._wf.detail_at(lat, lon, epoch)
        if not d:
            return d
        return dict(d, tws=round(d["tws"] * self.tws_scale, 2),
                    twd=round((d["twd"] + self.twd_shift) % 360.0, 1))

    def wind_at(self, lat, lon, epoch):
        d = self.detail_at(lat, lon, epoch)
        return (d["tws"], d["twd"]) if d else None

    def sample_grid(self, epoch, step_deg, bbox=None):
        from .wind.windfield import WindField
        return WindField.sample_grid(self, epoch, step_deg, bbox)

    def status(self):
        s = dict(self._wf.status())
        s["study"] = {"tws_scale": self.tws_scale, "twd_shift": self.twd_shift}
        return s


def blend_weights(wf):
    """Each series' share of the blend in % → ({model: pct}, {(model, member): pct}). Rough: the
    static priority × the venue's skill weight; where a model doesn't cover a spot the shares there
    differ."""
    from .wind.models import MODELS
    raw = {}
    for (model, member) in wf.series:
        src = MODELS.get(model)
        raw[(model, member)] = ((src.priority if src else 1.0)
                                * (getattr(wf, "model_weights", None) or {}).get(model, 1.0))
    tot = sum(raw.values()) or 1.0
    per_series = {k: round(100.0 * w / tot, 1) for k, w in raw.items()}
    per_model = {}
    for (model, _m), pct in per_series.items():
        per_model[model] = round(per_model.get(model, 0.0) + pct, 1)
    return per_model, per_series


# ---- reading a route ---------------------------------------------------------------------------
def side_of(result):
    """Which side of the first leg's rhumb line the route starts on: left / right / middle."""
    marks = result.get("marks") or []
    legs = result.get("legs") or []
    if len(marks) < 2 or not legs:
        return None
    fh = legs[0].get("first_heading")
    a, b = marks[0], marks[1]
    if fh is None or a.get("lat") is None or b.get("lat") is None:
        return None
    rhumb = optimizer._bearing(a["lat"], a["lon"], b["lat"], b["lon"])
    d = ((fh - rhumb + 540) % 360) - 180
    return "left" if d < -SIDE_DEG else "right" if d > SIDE_DEG else "middle"


def thin(path, n=PATH_MAX_PTS):
    """[[lat, lon], ...] at most `n` long, ends kept."""
    pts = [[p["lat"], p["lon"]] for p in path or []]
    if len(pts) <= n:
        return pts
    step = (len(pts) - 1) / (n - 1)
    return [pts[round(i * step)] for i in range(n)]


def row_of(spec, r):
    """One results-table row from a run's spec and its route."""
    row = {k: spec[k] for k in ("id", "axis", "label") if k in spec}
    for k in ("model", "member", "start_min", "polar_pct", "tws_pct", "twd_deg", "cycle",
              "weight_pct", "correction"):
        if spec.get(k) is not None:
            row[k] = spec[k]
    ok = bool(r.get("available")) and bool(r.get("path")) and r.get("complete", True)
    row["ok"] = ok
    if not ok:
        row["note"] = (r.get("note") or (r.get("incomplete") or {}).get("reason")
                       or "No full route.")
        return row
    row["finish_epoch"] = r.get("finish_epoch")
    row["hours"] = r.get("total_hours")
    row["minutes"] = r.get("total_minutes")
    row["side"] = side_of(r)
    row["degraded"] = bool(r.get("degraded") or r.get("timed_out"))
    row["path"] = thin(r.get("path"))
    return row


def summarize(rows, tie_min=TIE_MIN):
    """Spread and sides over the runs that finished, each row's time on the course vs the
    baseline's (`delta_min`), and which rows tie the baseline (within `tie_min`). The spread is
    of finish times among runs that start with the baseline."""
    good = [r for r in rows if r.get("ok") and r.get("finish_epoch") is not None]
    base = next((r for r in good if r["id"] == "base"), None)
    for r in good:          # time on the course vs the baseline (a later start isn't "slower")
        if base is not None and r.get("minutes") is not None and base.get("minutes") is not None:
            dm = r["minutes"] - base["minutes"]
            r["delta_min"] = round(dm, 1)
            r["tie"] = abs(dm) <= tie_min
    fin = [r["finish_epoch"] for r in good if r.get("start_min") is None]  # same start only
    sides = {}
    for r in good:
        if r.get("side"):
            sides[r["side"]] = sides.get(r["side"], 0) + 1
    out = {"finished": len(good), "failed": len(rows) - len(good), "sides": sides,
           "spread_min": round((max(fin) - min(fin)) / 60.0, 1) if len(fin) >= 2 else None,
           "tie_min": tie_min, "tie_note": TIE_NOTE.format(m=tie_min),
           "agreement_note": AGREEMENT_NOTE}
    return out


# ---- the batch ---------------------------------------------------------------------------------
def run(definition, course_id, start_epoch, models, axes, *, cap=DEFAULT_CAP, ensemble_members=0,
        avoid=True, resolution="auto", use_waves=True, use_current=True, boat=None,
        chart_source=None, on_progress=None, model_skill=True, tie_min=TIE_MIN,
        corrections=None, time_budget_s=None):
    """Blocking: load the weather once, route every run in the plan, return the table.

    Refuses (before any download) a plan over `cap` (itself held to MAX_RUNS). The baseline
    result rides along whole under `baseline` (the map draws it, the fan on top)."""
    from .wind import build_windfield
    from .wind.models import MODELS
    from shared.boat_polar import NO_POLAR
    say = on_progress or (lambda *_: None)
    try:
        cap = max(1, min(int(cap or DEFAULT_CAP), MAX_RUNS))
    except (TypeError, ValueError):
        cap = DEFAULT_CAP
    if not routerun.boat_inputs(boat)["boat_polar"]:
        return {"available": False, "note": NO_POLAR}
    names = [m if isinstance(m, str) else m.name for m in models]
    n_members = sum(int(ensemble_members or 0) for m in names
                    if getattr(MODELS.get(m), "kind", "") == "ensemble")
    planned = run_count(axes, len(names), n_members)
    if planned > cap:
        return {"available": False, "planned": planned, "cap": cap,
                "note": f"{planned} runs is over the limit of {cap}. Take values off or raise the limit."}
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "No mark positions yet. Fix them in Course & Marks."}
    offs = axes.get("start_min") or []
    hours = optimizer.estimate_hours(definition, course_id)
    t0 = start_epoch + min([0] + offs) * 60
    t_end = start_epoch + max([0] + offs) * 60 + hours * 3600
    log = routerun._Log(say)
    log.append("Study: getting the weather once for every run")
    skill, mweights, mbias = (routerun.venue_skill(definition, course_id, bbox, start_epoch,
                                                   models, log.append)
                              if model_skill else ({"enabled": False}, None, None))
    extra = {"corrections": corrections} if corrections else {}
    wf = build_windfield(bbox, t0, t_end, models=models, ensemble_members=ensemble_members,
                         on_progress=log.append, model_weights=mweights, model_bias=mbias,
                         **extra)
    if not wf.loaded:
        return {"available": False, "note": "No forecast could be loaded (not out yet, or no "
                "internet).", "windfield": wf.status(), "log": log}
    cur, waves = routerun.sea_fields(bbox, t0, t_end, use_current=use_current,
                                     use_waves=use_waves, say=log.append)
    obstacles = leg_obstacles = None
    if avoid:
        try:
            from .geo import build_leg_fields
            from shared import race_def
            _m, _s, cid = race_def.course_to_marks(definition, course_id)
            obstacles, leg_obstacles = build_leg_fields(
                definition, cid or course_id, bbox, source=chart_source,
                safety_depth=routerun.boat_inputs(boat)["safety_depth"])
        except Exception as exc:  # noqa: BLE001 — each run then builds (and reports) its own
            log.append(f"chart: built per run ({type(exc).__name__})")
            obstacles = leg_obstacles = None

    model_keys = sorted({k[0] for k in wf.series})
    member_keys = sorted(k for k in wf.series
                         if getattr(MODELS.get(k[0]), "kind", "") == "ensemble" and k[1] != "det")
    specs = plan(axes, model_keys, member_keys)[:cap]
    per_model, per_series = blend_weights(wf)
    cycles = {m.get("model"): m.get("cycle") for m in wf.meta if isinstance(m, dict)}
    corr = {}                         # #384: each model's manual correction, in words
    if getattr(wf, "corrections", None):
        st = wf.status().get("corrections") or {}
        for it in (st.get("items") or []) if isinstance(st, dict) else []:
            corr[it.get("model")] = it.get("text")

    rows, baseline = [], None
    t_started = time.time()
    for i, spec in enumerate(specs):
        view, start, scale = wf, start_epoch, 1.0
        if spec["axis"] == "model":
            m = spec["model"]
            view = subset(wf, lambda k, m=m: k[0] == m)
            spec.update(cycle=cycles.get(m), weight_pct=per_model.get(m))
        elif spec["axis"] == "member":
            key = (spec["model"], spec["member"])
            view = subset(wf, lambda k, key=key: k == key)
            spec.update(cycle=cycles.get(spec["model"]), weight_pct=per_series.get(key))
        elif spec["axis"] == "start_min":
            start = start_epoch + spec["start_min"] * 60
        elif spec["axis"] == "polar_pct":
            scale = spec["polar_pct"] / 100.0
        elif spec["axis"] == "tws_pct":
            view = Nudged(wf, tws_scale=spec["tws_pct"] / 100.0)
        elif spec["axis"] == "twd_deg":
            view = Nudged(wf, twd_shift=spec["twd_deg"])
        if spec.get("model") and corr.get(spec["model"]):
            spec["correction"] = corr[spec["model"]]
        eta = ""
        if i:
            per = (time.time() - t_started) / i
            eta = f", about {max(1, math.ceil(per * (len(specs) - i) / 60))} min left"
        say(f"Study run {i + 1} of {len(specs)}: {spec['label']}{eta}")
        is_base = spec["id"] == "base"
        try:
            r = routerun.run_course(
                definition, course_id, start, models, avoid=avoid, resolution=resolution,
                boat=boat, chart_source=chart_source, model_skill=False,
                obstacles=obstacles, leg_obstacles=leg_obstacles, time_budget_s=time_budget_s,
                briefing=False, grids=is_base, windfield=view, currents=cur, seastate=waves,
                speed_scale=scale)
        except Exception as exc:  # noqa: BLE001 — one bad run must not sink the batch
            r = {"available": False, "note": f"{type(exc).__name__}: {exc}"}
        rows.append(row_of(spec, r))
        if is_base:
            baseline = r
    out = summarize(rows, tie_min=tie_min)
    if isinstance(baseline, dict) and baseline.get("available"):
        baseline.pop("log", None)
        baseline["model_skill"] = skill
        baseline["candidate_paths"] = fan(rows)
    out.update({"available": True, "kind": "study", "axes": axes, "rows": rows,
                "baseline": baseline, "windfield": wf.status(), "model_skill": skill,
                "seconds": round(time.time() - t_started), "log": log})
    return out


def fan(rows):
    """The runs as the map's route fan (mapview's candidate_paths): one line per finished run
    other than the baseline, coloured by axis."""
    out = []
    for r in rows:
        if r["id"] == "base" or not r.get("ok") or not r.get("path"):
            continue
        out.append({"model": r.get("model") or r["axis"], "label": r["label"],
                    "axis": r["axis"], "total_hours": r.get("hours"),
                    "favored_side": r.get("side") or "", "path": r["path"]})
    return out
