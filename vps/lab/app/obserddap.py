"""Buoy networks served from an ERDDAP server (#309): station list and observed wind, no key.

ERDDAP is the data server many ocean institutes run, with the same query language everywhere, so
one class reads any of their buoy networks. Each network is one `ErddapBuoySource(...)` with its
server, dataset, variable names and coverage box, registered with `obsarea.register()`. Its
stations then show in the race station lookup, the Lab's finder and the venue study, and model
skill scores against their wind (`observed`). The boat does not read these live (obsarea's
READABLE_KINDS), so they never go into the frozen station list.

Registered here:
  - Marine Institute (Ireland) weather buoys, dataset IWBNetwork on erddap.marine.ie: M2-M6 and
    the archive buoys, hourly wind (knots) since 2001. CC BY 4.0: the source label names them.

Every read goes through obsarea._get (stubbed in tests). A network that answers with something
we can't read gives no stations or no observations, never a guess: wind in an unknown unit is
dropped, not converted.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import time
import urllib.parse

from . import obsarea

ACTIVE_DAYS = 30           # "active": reported in the last 30 days (the automatic pick, add by id)
_KN = {"knots": 1.0, "knot": 1.0, "kn": 1.0, "kt": 1.0, "kts": 1.0,
       "m/s": 1.943844, "m s-1": 1.943844, "m.s-1": 1.943844, "meters/second": 1.943844}


def _rows(text):
    """ERDDAP .csv: a header row, a units row, then data. Returns (header, units, rows)."""
    rd = list(csv.reader(io.StringIO(text or "")))
    if len(rd) < 2:
        return [], [], []
    return [h.strip() for h in rd[0]], [u.strip() for u in rd[1]], rd[2:]


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _epoch(iso):
    try:
        return dt.datetime.strptime(iso.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


class ErddapBuoySource(obsarea.StationSource):
    """One buoy network on an ERDDAP server (a tabledap dataset with one row per station-time)."""

    def __init__(self, name, label, server, dataset, coverage, coverage_label, id_var="station_id",
                 speed_var="WindSpeed", dir_var="WindDirection", qc_var=None, qc_bad=()):
        self.name = self.kind = name
        self.label = label
        self.server = server.rstrip("/")
        self.dataset = dataset
        self.coverage = coverage
        self.coverage_label = coverage_label
        self.id_var, self.speed_var, self.dir_var = id_var, speed_var, dir_var
        self.qc_var, self.qc_bad = qc_var, {str(q) for q in qc_bad}
        self._ids = {}

    # outside its own water a regional network stays quiet: the race hears about the networks that
    # could have stations there, not a list of every network elsewhere
    quiet_outside = True

    def _url(self, query):
        return (f"{self.server}/tabledap/{self.dataset}.csv?"
                + urllib.parse.quote(query, safe="&="))

    def stations(self):
        """Every station the dataset has ever had: [{id, kind, lat, lon, name, end}], at its
        position when it last reported; `end` = that date (YYYY-MM-DD). Cached a day."""
        def load():
            v = self.id_var
            header, _units, rows = _rows(obsarea._get(self._url(
                f'{v},longitude,latitude,time&orderByMax("{v},time")')))
            try:
                ci, clo, cla, ct = (header.index(c) for c in (v, "longitude", "latitude", "time"))
            except ValueError:
                return []
            out = []
            for r in rows:
                if len(r) <= max(ci, clo, cla, ct):
                    continue
                sid = r[ci].strip()
                la, lo = _float(r[cla]), _float(r[clo])
                if not sid or len(sid) > 16 or la is None or lo is None \
                        or not (-90 <= la <= 90 and -180 <= lo <= 180):
                    continue
                if not all(c.isalnum() or c in "-_" for c in sid):
                    continue
                self._ids[sid.upper()] = sid          # ids are upper case here, as-is on the server
                out.append({"id": sid.upper(), "kind": self.kind, "lat": la, "lon": lo,
                            "name": sid, "end": r[ct].strip()[:10]})
            return out
        return obsarea._cached(f"erddap:{self.name}:stations", load)

    def find_all(self, box):
        """Every station in box=(n, s, w, e), active or not (a past window can still use them)."""
        return [st for st in self.stations() if obsarea._in_box(st, box)]

    def find(self, box):
        """The active stations (reported in the last ACTIVE_DAYS days) in box=(n, s, w, e)."""
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - ACTIVE_DAYS * 86400))
        return [st for st in self.find_all(box) if st["end"] >= since]

    def observed(self, sid, sd, ed):
        """Observed wind at station `sid` over [sd, ed] (YYYY-MM-DD, UTC days, inclusive):
        {epoch: (tws_kn, twd_deg)}. {} when the network has nothing (ERDDAP answers an empty
        window with an error) or can't be read."""
        self._safe_stations()
        src_id = self._ids.get(str(sid).upper())
        if src_id is None:
            return {}
        cols = ["time", self.speed_var, self.dir_var] + ([self.qc_var] if self.qc_var else [])
        q = (",".join(cols) + f'&{self.id_var}="{src_id}"'
             + f"&time>={sd}T00:00:00Z&time<={ed}T23:59:59Z")
        try:
            header, units, rows = _rows(obsarea._get(self._url(q), timeout=60))
            ct, cs, cd = (header.index(c) for c in cols[:3])
        except Exception:          # noqa: BLE001 — no data in the window, or the server is down
            return {}
        k = _KN.get(units[cs].lower() if cs < len(units) else "")
        if k is None:
            return {}              # wind in a unit we don't know: dropped, never guessed
        cq = header.index(self.qc_var) if self.qc_var and self.qc_var in header else None
        out = {}
        for r in rows:
            if len(r) <= max(ct, cs, cd, cq or 0):
                continue
            if cq is not None and r[cq].strip() in self.qc_bad:
                continue
            ep, spd, wd = _epoch(r[ct]), _float(r[cs]), _float(r[cd])
            if ep is None or spd is None or wd is None:
                continue
            spd *= k
            if not (0 <= spd <= 150 and 0 <= wd <= 360):
                continue
            out[ep] = (spd, wd % 360)
        return out

    def _safe_stations(self):
        try:
            return self.stations()
        except Exception:          # noqa: BLE001
            return []


MARINE_IE = obsarea.register(ErddapBuoySource(
    name="ie_buoy", label="Irish buoys (Marine Institute)",
    server="https://erddap.marine.ie/erddap", dataset="IWBNetwork",
    # the buoys sit 5.4-15.9 W, 51.2-55 N; the box is their water, the Celtic and Irish Seas
    coverage=(49.5, 56.5, -18.0, -4.5), coverage_label="Irish waters only",
    qc_var="QC_Flag", qc_bad=(9,)))      # 0 unknown (all data since 2018), 1 good, 9 missing
