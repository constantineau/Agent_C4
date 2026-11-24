"""Historical archive-backed model sources — the wind as it was KNOWN at a past moment.

For the fleet-retro study (docs/RETRO_STUDY.md §4.3): reconstruct the forecast a navigator could
have pulled at the gun of a PAST race. Same `ModelSource` interface as the live NOMADS sources, so
`build_windfield` / `GribFrame` / `optimize_course` run unchanged — only `fetch()` differs: the 10 m
UGRD/VGRD messages are pulled by `.idx` byte-range from the public AWS open-data buckets (the
`deepfc` machinery, with its retry+backoff), and every fetched file is **pinned into the durable
retro archive** (`retrostore.pin_grib`) so the study's inputs survive any cache eviction.

`asof` (epoch) freezes the cycle picker: `pick_cycle` returns the freshest cycle that was POSTED by
that moment (lag-aware), exactly what was knowable at the gun. Buckets:
  - GFS  0.25°  `noaa-gfs-bdp-pds`   (complete since ~2021-02)
  - HRRR CONUS  `noaa-hrrr-bdp-pds`  (complete since 2014-08; f48 on synoptic cycles from ~2019)
For a race that is OVER, `analysis_field` gives the wind that actually blew instead: each hour's
own HRRR 0-h analysis, chained over the window (#404). It costs about what the gun cycle costs (one
small byte-range fetch per hour) and is US-only like HRRR itself.
RRFS (NOAA's NAM replacement, operational 2026-11, #360) has no source here on purpose: its AWS
bucket holds only the pre-operational rrfs_a prototype, so a past race replays on GFS + HRRR.

Note: archive messages are WHOLE-DOMAIN (byte-range selects the variable, not a bbox) — heavier in
RAM than the live bbox-subset path but correct; crop-at-parse is a future optimization if fleet
batches need it.
"""
import datetime as dt
import math
import os

from .. import atomicio, deepfc
from . import models as live

GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
_LEVEL = "10 m above ground"


def _pin(path, model, cycle, fhr, member, bbox, url, context):
    """Best-effort durable pin — a pin failure never fails the fetch."""
    try:
        from .. import retrostore
        retrostore.pin_grib(path, model=model, cycle=f"{cycle:%Y%m%d%H}", fhr=fhr, member=member,
                            bbox=bbox, source_url=url, context=context)
    except Exception:
        pass


class _ArchiveMixin:
    asof = None            # epoch seconds — "the gun": only cycles posted by then are eligible
    pin_context = None     # e.g. 'retro:bayviewmack2025' — recorded in the GRIB registry

    def _asof_dt(self):
        if not self.asof:
            return None
        return dt.datetime.fromtimestamp(self.asof, dt.timezone.utc).replace(tzinfo=None)

    def pick_cycle(self, now=None, min_horizon_h: int = 0):
        return super().pick_cycle(now=now or self._asof_dt(), min_horizon_h=min_horizon_h)

    def fetch(self, cycle, fhr, member, bbox, timeout=90):
        path = self._cache_path(cycle, fhr, member, bbox)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
        base = self._aws_object(cycle, fhr)
        recs = deepfc._idx(base + ".idx")
        iu = deepfc._find(recs, "UGRD", _LEVEL)
        iv = deepfc._find(recs, "VGRD", _LEVEL)
        if iu is None or iv is None:
            return None
        try:
            blob = (deepfc._get(base, timeout=timeout, rng=deepfc._msg_range(recs, iu))
                    + deepfc._get(base, timeout=timeout, rng=deepfc._msg_range(recs, iv)))
        except Exception:
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomicio.write_bytes(path, blob)
        _pin(path, self.name, cycle, fhr, member, bbox, base, self.pin_context)
        return path


class ArchiveGFS(_ArchiveMixin, live.GFS):
    """Same name/priority/grid as live GFS so blend weights + model-skill weighting apply."""

    def _aws_object(self, cycle, fhr):
        return (f"{GFS_BASE}/gfs.{cycle:%Y%m%d}/{cycle.hour:02d}/atmos/"
                f"gfs.t{cycle.hour:02d}z.pgrb2.0p25.f{fhr:03d}")


class ArchiveHRRR(_ArchiveMixin, live.HRRR):
    def _aws_object(self, cycle, fhr):
        return (f"{deepfc.HRRR_BASE}/hrrr.{cycle:%Y%m%d}/conus/"
                f"hrrr.t{cycle.hour:02d}z.wrfsfcf{fhr:02d}.grib2")


def gun_sources(asof_epoch: float, context: str | None = None) -> list:
    """The archive sources for 'what was knowable at `asof_epoch`' — pass to build_windfield's
    `models` list (instances pass through). GFS carries the whole race window; HRRR sharpens the
    early hours where its horizon reaches."""
    out = []
    for cls in (ArchiveGFS, ArchiveHRRR):
        s = cls()
        s.asof = asof_epoch
        s.pin_context = context
        out.append(s)
    return out


def _inside(bbox, coverage):
    """Is the whole race box (n, s, w, e) inside a model's area (s, n, w, e)? None = everywhere."""
    if coverage is None:
        return True
    n, s, w, e = bbox
    cs, cn, cw, ce = coverage
    return cs <= s and n <= cn and cw <= w and e <= ce


ANALYSIS_CYCLE = "analysis-chain"     # the meta "cycle" of a field made of hourly analyses


def analysis_field(bbox, t0, t1, context=None, on_progress=None):
    """The wind that actually blew over [t0, t1]: hourly HRRR f00 (analysis) frames, chained.
    Returns a WindField; an EMPTY one (loaded False) when the race box is not wholly inside HRRR
    (an analysis covering half the course would leave the oracle borrowing wind for the rest) or
    nothing could be fetched. Same pinned-archive fetch path as the gun cycle."""
    from . import grib
    from .windfield import WindField, not_here_meta
    log = on_progress or (lambda *_: None)
    src = ArchiveHRRR()
    if not _inside(bbox, src.coverage):
        log(src.not_here_note())
        return WindField({}, [not_here_meta(src)], bbox, t0, t1)
    src.asof = t1 + 7200                      # cycle picks are explicit below; asof just must not veto
    src.pin_context = context
    parser = grib.IsolatedGribParser() if getattr(grib, "ISOLATE", False) else None
    frames = []
    try:
        h = math.floor(t0 / 3600.0) * 3600.0
        while h <= t1 + 3600.0:
            cycle = dt.datetime.fromtimestamp(h, tz=dt.timezone.utc)
            try:
                path = src.fetch(cycle, 0, "det", bbox)
                if path:
                    frames.append(grib.GribFrame.from_file(
                        path, "hrrr", "det", h, parser=parser).crop(bbox))
            except Exception as e:
                log(f"analysis {cycle:%Y-%m-%d %HZ} skipped: {type(e).__name__}")
            h += 3600.0
    finally:
        if parser is not None:
            parser.close()
    frames.sort(key=lambda fr: fr.valid_time)
    series = {("hrrr", "det"): frames} if frames else {}
    meta = [{"model": "hrrr", "cycle": ANALYSIS_CYCLE, "members": 1,
             "frames": len(frames), "expected_frames": int((t1 - t0) / 3600) + 2,
             "cycle_fallbacks": 0, "priority": 1, "kind": "deterministic"}]
    log(f"hrrr: {len(frames)} hourly analyses")
    return WindField(series, meta, bbox, t0, t1)
