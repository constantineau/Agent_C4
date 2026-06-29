"""WindField — the multi-model, blended wind field the optimizer routes through.

`build_windfield(bbox, t_start, t_end, models, ...)` ingests each selected model's 10 m wind over
the race bbox + time window into per-(model, member) frame series. `WindField.wind_at(lat, lon,
epoch)` then samples every series (bilinear/nearest in space, linear in time), blends them by model
priority into one (tws_kn, twd_deg), and — crucially — reports the SPREAD across models/members as
a confidence (the fuzzy-adherence principle: models disagree → lower confidence → the optimizer and
the strategy card should be more conservative).

Drop-in for the agent's `weather.wind_at`, so the isochrone optimizer can route through it
unchanged. Ingestion is best-effort: a model/field that isn't posted yet is skipped, and the field
still works on whatever did load (degrades to fewer models, honestly reflected in the spread).
"""
from __future__ import annotations

import datetime as dt
import math
import os

from . import grib
from .models import MODELS, DEFAULT_MODELS

KN_PER_MS = grib.KN_PER_MS
MAX_FRAMES_PER_MEMBER = 64        # safety cap on downloads per series
CYCLE_FALLBACK_TRIES = int(os.environ.get("GRIB_CYCLE_FALLBACK", "2"))   # step back N cycles if sparse
MIN_FRAME_FRAC = float(os.environ.get("GRIB_MIN_FRAME_FRAC", "0.5"))     # "enough coverage" threshold
_PAD = 3 * 3600                  # keep frames just outside the window so we can bracket the ends


def _members_for(source, ensemble_members):
    if source.kind == "ensemble":
        return [] if ensemble_members <= 0 else list(source.members[:ensemble_members])
    return ["det"]


def _fhrs_for_cycle(source, cycle, t_start, t_end):
    """The forecast-hours of `source` from `cycle` that fall inside [t_start, t_end] (+pad), capped
    at the horizon this cycle actually reaches (per-cycle — HRRR's off-synoptic cycles stop at 18 h)."""
    horizon = math.ceil((t_end - cycle.timestamp()) / 3600.0) + source.fhr_step
    horizon = min(max(horizon, source.fhr_step), source.horizon_for(cycle))
    fhrs = [f for f in source.fhrs(horizon)
            if t_start - _PAD <= source.valid_time(cycle, f) <= t_end + _PAD]
    return fhrs[:MAX_FRAMES_PER_MEMBER]


def _load_model(source, bbox, t_start, t_end, members, on_progress, parser=None):
    """Ingest one model's frame-series with CYCLE-FALLBACK: if the freshest cycle is too sparse (not
    fully posted yet) step back a cycle and retry, up to CYCLE_FALLBACK_TRIES. Returns (series, meta).

    `parser` (an IsolatedGribParser) runs the cfgrib parse out-of-process so a native segfault on a
    frame is survived (that frame is skipped, like any other unreadable one) instead of crashing."""
    need_h = max(0, math.ceil((t_end - dt.datetime.now(dt.timezone.utc).timestamp()) / 3600.0))
    cycle = source.pick_cycle(min_horizon_h=need_h)
    series, loaded, expected, fallbacks = {}, 0, 0, 0
    for attempt in range(CYCLE_FALLBACK_TRIES + 1):
        fhrs = _fhrs_for_cycle(source, cycle, t_start, t_end)
        expected = len(fhrs) * max(1, len(members))
        series, loaded = {}, 0
        for member in members:
            frames = []
            for fhr in fhrs:
                path = source.fetch(cycle, fhr, member, bbox)
                if not path:
                    continue
                try:
                    frames.append(grib.GribFrame.from_file(
                        path, source.name, member, source.valid_time(cycle, fhr), parser=parser))
                except Exception:
                    continue
            if frames:
                frames.sort(key=lambda fr: fr.valid_time)
                series[(source.name, member)] = frames
                loaded += len(frames)
        if expected == 0 or loaded >= MIN_FRAME_FRAC * expected or attempt == CYCLE_FALLBACK_TRIES:
            break
        if on_progress:
            on_progress(f"{source.name}: sparse ({loaded}/{expected}) — retrying previous cycle")
        cycle = source.prev_cycle(cycle)
        fallbacks += 1
    meta = {"model": source.name, "cycle": cycle.strftime("%Y-%m-%d %HZ"),
            "members": len(members), "frames": loaded, "expected_frames": expected,
            "cycle_fallbacks": fallbacks, "priority": source.priority, "kind": source.kind}
    if on_progress:
        tail = f" (after {fallbacks} cycle-fallback)" if fallbacks else ""
        on_progress(f"{source.name}: {loaded}/{expected} frames @ {cycle:%Y-%m-%d %HZ}{tail}")
    return series, meta


def build_windfield(bbox, t_start: float, t_end: float, models=DEFAULT_MODELS,
                    ensemble_members: int = 0, on_progress=None):
    """Ingest the selected models over `bbox` for valid times spanning [t_start, t_end].

    bbox = (north, south, west, east). Returns a WindField. `on_progress(msg)` is called with
    short status strings as each model loads (for UI/log feedback). Per model: picks the freshest
    cycle that reaches the race window, requests only the forecast-hours that cycle actually has, and
    falls back to the previous cycle if the freshest is still too sparse to route on."""
    series = {}                   # (model, member) -> [GribFrame sorted by valid_time]
    meta = []
    parser = grib.IsolatedGribParser() if grib.ISOLATE else None   # crash-isolated cfgrib parse (1/build)
    try:
        for name in models:
            source = MODELS.get(name)
            if source is None:
                continue
            members = _members_for(source, ensemble_members)
            m_series, m_meta = _load_model(source, bbox, t_start, t_end, members, on_progress, parser=parser)
            series.update(m_series)
            meta.append(m_meta)
    finally:
        if parser is not None:
            parser.close()
    return WindField(series, meta, bbox, t_start, t_end)


class WindField:
    def __init__(self, series, meta, bbox, t_start, t_end):
        self.series = series          # (model, member) -> [GribFrame]
        self.meta = meta
        self.bbox = bbox
        self.t_start = t_start
        self.t_end = t_end

    @property
    def loaded(self) -> bool:
        return any(self.series.values())

    def _series_uv(self, frames, lat, lon, epoch):
        """Time-interpolated (u, v) from one frame series at a position, or None."""
        if not frames:
            return None
        if epoch <= frames[0].valid_time:
            return frames[0].sample_uv(lat, lon)
        if epoch >= frames[-1].valid_time:
            return frames[-1].sample_uv(lat, lon)
        for a, b in zip(frames, frames[1:]):
            if a.valid_time <= epoch <= b.valid_time:
                ua = a.sample_uv(lat, lon)
                ub = b.sample_uv(lat, lon)
                if ua is None or ub is None:
                    return ua or ub
                f = (epoch - a.valid_time) / max(1.0, b.valid_time - a.valid_time)
                return (ua[0] + (ub[0] - ua[0]) * f, ua[1] + (ub[1] - ua[1]) * f)
        return None

    def detail_at(self, lat: float, lon: float, epoch: float):
        """Blended wind + per-member spread/confidence at a position/time, or None."""
        samples = []                  # (u, v, weight, model)
        for (model, _member), frames in self.series.items():
            uv = self._series_uv(frames, lat, lon, epoch)
            if uv is None:
                continue
            samples.append((uv[0], uv[1], MODELS[model].priority, model))
        if not samples:
            return None

        wsum = sum(s[2] for s in samples) or 1.0
        mu = sum(s[0] * s[2] for s in samples) / wsum
        mv = sum(s[1] * s[2] for s in samples) / wsum
        tws, twd = grib.uv_to_tws_twd(mu, mv)

        tws_each = [math.hypot(u, v) * KN_PER_MS for u, v, *_ in samples]
        n = len(tws_each)
        tws_mean = sum(tws_each) / n
        tws_std = math.sqrt(sum((t - tws_mean) ** 2 for t in tws_each) / n) if n > 1 else 0.0
        # circular spread of direction: mean resultant length of unit wind vectors
        cx = sum(u / max(1e-6, math.hypot(u, v)) for u, v, *_ in samples) / n
        cy = sum(v / max(1e-6, math.hypot(u, v)) for u, v, *_ in samples) / n
        R = min(1.0, math.hypot(cx, cy))
        twd_std_deg = math.degrees(math.sqrt(-2.0 * math.log(R))) if R > 1e-6 else 90.0
        # confidence 0..1: tight when models agree on speed and direction
        conf = max(0.0, min(1.0, 1.0 - tws_std / (0.4 * tws + 4.0) - twd_std_deg / 70.0))
        return {
            "tws": round(tws, 2), "twd": round(twd, 1),
            "tws_spread_kn": round(tws_std, 2), "twd_spread_deg": round(twd_std_deg, 1),
            "confidence": round(conf, 2),
            "n_samples": n, "models": sorted({s[3] for s in samples}),
        }

    def wind_at(self, lat: float, lon: float, epoch: float):
        """(tws_kn, twd_deg) — the drop-in the isochrone optimizer samples."""
        d = self.detail_at(lat, lon, epoch)
        return (d["tws"], d["twd"]) if d else None

    def sample_grid(self, epoch: float, step_deg: float, bbox=None):
        """Sample the blended wind on a lat/lon grid at one time → [{lat,lon,tws,twd,confidence}].

        For the map's wind overlay (barbs rotated by TWD, coloured by TWS, faded by confidence).
        Points with no model coverage are dropped. `bbox`=(n,s,w,e); defaults to the field bbox."""
        n, s, w, e = bbox or self.bbox
        step = max(0.02, float(step_deg))
        pts = []
        lat = s
        while lat <= n + 1e-9:
            lon = w
            while lon <= e + 1e-9:
                d = self.detail_at(lat, lon, epoch)
                if d:
                    pts.append({"lat": round(lat, 4), "lon": round(lon, 4),
                                "tws": d["tws"], "twd": d["twd"], "confidence": d["confidence"]})
                lon += step
            lat += step
        return pts

    def status(self):
        return {"loaded": self.loaded, "models": self.meta,
                "window": [round(self.t_start), round(self.t_end)], "bbox": self.bbox,
                "series": len(self.series),
                "total_frames": sum(len(f) for f in self.series.values())}
