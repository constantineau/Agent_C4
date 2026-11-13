"""Buoy networks on ERDDAP servers: the network registry and the CSV reading (#309).

ERDDAP is the data server many ocean institutes run, with the same query language everywhere, so
one reader covers any of their buoy networks. A network is one `Network(...)` here: its kind,
server, dataset and variable names. `register()` it and both sides pick it up:

  - the Lab (vps/lab/app/obserddap.py) finds its stations for a race's area and scores model skill
    against its wind, and freezes its live stations into the playbook (obsarea.READABLE_KINDS
    holds every kind registered here);
  - the boat (vps/agent/app/buoys.py, also in the Pi engine) reads its latest wind live, one call
    per network, for the stations the frozen playbook lists.

No network code here: callers pass the text they fetched. Parsing is defensive. A row we can't
read is skipped, wind in a unit we don't know is dropped (never guessed), and a missing-flagged
row is dropped.

Registered:
  - ie_buoy: Marine Institute (Ireland) weather buoys, dataset IWBNetwork on erddap.marine.ie.
    M2-M6 and the archive buoys, hourly wind in knots. No key. CC BY 4.0.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import time
import urllib.parse

# wind speed unit (as ERDDAP's units row spells it) -> factor to knots
UNITS_KN = {"knots": 1.0, "knot": 1.0, "kn": 1.0, "kt": 1.0, "kts": 1.0,
            "m/s": 1.943844, "m s-1": 1.943844, "m.s-1": 1.943844, "meters/second": 1.943844}


def rows(text):
    """ERDDAP .csv: a header row, a units row, then data. Returns (header, units, rows)."""
    try:
        rd = list(csv.reader(io.StringIO(text or "")))
    except csv.Error:
        return [], [], []
    if len(rd) < 2:
        return [], [], []
    return [h.strip() for h in rd[0]], [u.strip() for u in rd[1]], rd[2:]


def to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def epoch(iso):
    """'2025-07-01T03:00:00Z' -> epoch seconds, or None."""
    try:
        return dt.datetime.strptime(iso.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def _iso(ep):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ep))


def safe_id(sid):
    """True for a station id we will put in a query (letters, digits, - and _, at most 16)."""
    s = str(sid or "")
    return 0 < len(s) <= 16 and all(c.isalnum() or c in "-_" for c in s)


class Network:
    """One buoy network on an ERDDAP server (a tabledap dataset, one row per station-time)."""

    def __init__(self, kind, server, dataset, id_var="station_id", speed_var="WindSpeed",
                 dir_var="WindDirection", qc_var=None, qc_bad=(), stale_min=None):
        self.kind = kind
        # how old a reading may be before the boat calls it stale (None: the boat's default). An
        # hourly network that posts late needs more than a 10-minute feed does.
        self.stale_min = stale_min
        self.server = server.rstrip("/")
        self.dataset = dataset
        self.id_var, self.speed_var, self.dir_var = id_var, speed_var, dir_var
        self.qc_var, self.qc_bad = qc_var, {str(q) for q in qc_bad}

    def url(self, query):
        return (f"{self.server}/tabledap/{self.dataset}.csv?"
                + urllib.parse.quote(query, safe="&="))

    def wind_cols(self):
        return [self.speed_var, self.dir_var] + ([self.qc_var] if self.qc_var else [])

    def recent_url(self, src_ids, since_epoch):
        """One query for the latest wind at several stations (ids as the server spells them),
        from `since_epoch` on. None when no id is usable."""
        ids = [s for s in dict.fromkeys(src_ids) if safe_id(s)]
        if not ids:
            return None
        v = self.id_var
        pick = f'{v}="{ids[0]}"' if len(ids) == 1 else f'{v}=~"({"|".join(ids)})"'
        return self.url(",".join([v, "time"] + self.wind_cols())
                        + f"&{pick}&time>={_iso(since_epoch)}")

    def parse_wind(self, text):
        """Wind rows from an ERDDAP .csv answer: [(station_id or None, epoch, tws_kn, twd_deg)].
        The station column is optional (a one-station query leaves it out). [] when the answer
        isn't a table we can read, or its wind unit is unknown."""
        header, units, data = rows(text)
        try:
            ct, cs, cd = (header.index(c) for c in ("time", self.speed_var, self.dir_var))
        except ValueError:
            return []
        k = UNITS_KN.get(units[cs].lower() if cs < len(units) else "")
        if k is None:
            return []              # wind in a unit we don't know: dropped, never guessed
        ci = header.index(self.id_var) if self.id_var in header else None
        cq = header.index(self.qc_var) if self.qc_var and self.qc_var in header else None
        need = max(c for c in (ct, cs, cd, ci, cq) if c is not None)
        out = []
        for r in data:
            if len(r) <= need:
                continue
            if cq is not None and r[cq].strip() in self.qc_bad:
                continue
            ep, spd, wd = epoch(r[ct]), to_float(r[cs]), to_float(r[cd])
            if ep is None or spd is None or wd is None:
                continue
            spd *= k
            if not (0 <= spd <= 150 and 0 <= wd <= 360):
                continue
            out.append((r[ci].strip() if ci is not None else None, ep, spd, wd % 360))
        return out

    def parse_recent(self, text):
        """{STATION_ID (upper case): [(epoch, tws_kn, twd_deg)] newest-first} from a recent_url
        answer. Speeds rounded to 0.1 kn, like the boat's other readers."""
        by_id: dict = {}
        for sid, ep, spd, wd in self.parse_wind(text):
            if sid:
                by_id.setdefault(sid.upper(), []).append((ep, round(spd, 1), wd))
        for r in by_id.values():
            r.sort(key=lambda x: -x[0])
        return by_id


NETWORKS: dict = {}


def register(net):
    """Add a network. Its kind becomes readable aboard and freezable in the Lab."""
    NETWORKS[net.kind] = net
    return net


def network(kind):
    """The registered network for a station kind, or None."""
    return NETWORKS.get(kind)


MARINE_IE = register(Network(
    kind="ie_buoy", server="https://erddap.marine.ie/erddap", dataset="IWBNetwork",
    qc_var="QC_Flag", qc_bad=(9,),       # 0 unknown (all data since 2018), 1 good, 9 missing
    stale_min=150))                       # hourly, posted up to ~1.5 h after the hour
