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

Note: archive messages are WHOLE-DOMAIN (byte-range selects the variable, not a bbox) — heavier in
RAM than the live bbox-subset path but correct; crop-at-parse is a future optimization if fleet
batches need it.
"""
import datetime as dt
import os

from .. import deepfc
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
        with open(path, "wb") as f:
            f.write(blob)
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
