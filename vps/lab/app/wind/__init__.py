"""Lab-1 multi-model wind field.

The C4 Performance Lab optimizer needs a spatially- and temporally-varying wind field over a
race course, blended from several public weather models. This package provides:

  - `grib`      — download a 10 m UGRD/VGRD GRIB2 subset and parse it into a samplable grid frame;
  - `models`    — per-model sources (GFS / GEFS / NAM / HRRR / ECMWF) that know their NOMADS /
                  open-data layout, run cadence, forecast-hour grid and availability lag;
  - `windfield` — `WindField`, which ingests selected models over a bbox + time window and exposes
                  `wind_at(lat, lon, epoch) -> (tws_kn, twd_deg)` (a drop-in for the agent's
                  `weather.wind_at`) plus a per-sample model/ensemble SPREAD used as confidence.

All of this is CLOUD / between-races work (the homework pattern, RRS 41): the frozen output is
loaded onboard before the start, never re-derived from the cloud mid-race.
"""
from .windfield import WindField, build_windfield        # noqa: F401
from .models import MODELS, available_models             # noqa: F401
