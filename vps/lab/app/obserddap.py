"""Buoy networks served from an ERDDAP server (#309): station list and observed wind, no key.

ERDDAP is the data server many ocean institutes run, with the same query language everywhere, so
one class reads any of their buoy networks. The network itself (server, dataset, variable names)
is a `shared.erddap.Network`, which the boat reads live too. Here it gets a label and a coverage
box: one `ErddapBuoySource(...)` registered with `obsarea.register()`. Its stations then show in
the race station lookup, the Lab's finder and the venue study, model skill scores against their
wind (`observed`), and their live stations are frozen into the playbook (a network registered in
shared.erddap is in obsarea.READABLE_KINDS).

Registered here:
  - Marine Institute (Ireland) weather buoys, dataset IWBNetwork on erddap.marine.ie: M2-M6 and
    the archive buoys, hourly wind (knots) since 2001. CC BY 4.0: the source label names them.

Every read goes through obsarea._get (stubbed in tests). A network that answers with something
we can't read gives no stations or no observations, never a guess: wind in an unknown unit is
dropped, not converted.
"""
from __future__ import annotations

import time

from shared import erddap

from . import obsarea

ACTIVE_DAYS = 30           # "active": reported in the last 30 days (the automatic pick, add by id)
_rows, _float = erddap.rows, erddap.to_float


class ErddapBuoySource(obsarea.StationSource):
    """One buoy network on an ERDDAP server (a tabledap dataset with one row per station-time)."""

    def __init__(self, network, label, coverage, coverage_label):
        self.network = network
        self.name = self.kind = network.kind
        self.label = label
        self.server, self.dataset = network.server, network.dataset
        self.coverage = coverage
        self.coverage_label = coverage_label
        self.id_var = network.id_var
        self._ids = {}

    # outside its own water a regional network stays quiet: the race hears about the networks that
    # could have stations there, not a list of every network elsewhere
    quiet_outside = True

    def _url(self, query):
        return self.network.url(query)

    def server_id(self, sid):
        """The station id as the server spells it (ids are upper case here), or None."""
        self._safe_stations()
        return self._ids.get(str(sid).upper())

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
                if not sid or la is None or lo is None \
                        or not (-90 <= la <= 90 and -180 <= lo <= 180):
                    continue
                if not erddap.safe_id(sid):
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
        src_id = self.server_id(sid)
        if src_id is None:
            return {}
        net = self.network
        q = (",".join(["time"] + net.wind_cols()) + f'&{self.id_var}="{src_id}"'
             + f"&time>={sd}T00:00:00Z&time<={ed}T23:59:59Z")
        try:
            got = net.parse_wind(obsarea._get(self._url(q), timeout=60))
        except Exception:          # noqa: BLE001 — no data in the window, or the server is down
            return {}
        return {ep: (spd, wd) for _sid, ep, spd, wd in got}

    def _safe_stations(self):
        try:
            return self.stations()
        except Exception:          # noqa: BLE001
            return []


MARINE_IE = obsarea.register(ErddapBuoySource(
    erddap.MARINE_IE, label="Irish buoys (Marine Institute)",
    # the buoys sit 5.4-15.9 W, 51.2-55 N; the box is their water, the Celtic and Irish Seas
    coverage=(49.5, 56.5, -18.0, -4.5), coverage_label="Irish waters only"))
