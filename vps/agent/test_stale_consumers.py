"""The consumers #289 left on their own numbers, now on `shared/staleness.py` (#291).

Each check feeds an input just past its limit and checks the read goes. Every one failed on the
code before #291:

- matcher % of polar: a dead wind sensor (newest TWS 60 s old) still gave a % (window only), and
  6 samples in 10 min were enough; now newest within 15 s aboard and half the window filled
- fatigue: a 60 s old heading still gave an index (windows just thinned out); now 30 s aboard
- wind trend: "No live wind" at 30 s aboard (was 120 s); the cloud keeps 120 s
- navigator ETA wind: a 20-min window whose newest wind was 60 s old still drove the polar ETA
- chartplotter fix: 5 s (was 10 s), gpsout and n2kout's copy pinned together
- DATA tile source rule: a source's stale limit follows the failover limit of its fastest channel
- cloud web page: every reading carries ok / late / dead (late 45 s, dead 120 s)
- overrides: a boat's own limits from STALENESS_OVERRIDES replace the defaults; bad entries are skipped

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_stale_consumers.py
"""
import os
import re
import sys
import time

os.environ["DATA_SOURCE"] = "onboard"
os.environ.setdefault("ONBOARD_LIVE_WS", "false")
for _k in ("TREND_FRESH_S", "GPSOUT_FIX_MAX_AGE_S", "STALENESS_OVERRIDES", "NAV_MAX_AGE_S"):
    os.environ.pop(_k, None)

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
for _p in (_here, _root):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import staleness  # noqa: E402
from app import datasource, fatigue, gpsout, matcher, navigator, onboard_conditions, trend  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


STW = "navigation.speedThroughWater"
TWS = "environment.wind.speedTrue"
TWD = "environment.wind.directionTrue"
TWA = "environment.wind.angleTrueWater"
HDG = "navigation.headingTrue"
HEEL = "navigation.attitude.roll"
AWA = "environment.wind.angleApparent"
SOG = "navigation.speedOverGround"
DEPTH = "environment.depth.belowTransducer"
VALUES = {STW: 7.0 * 0.8 / 1.943844, TWS: 8.0 / 1.943844, TWA: 1.5708, TWD: 3.6,
          HDG: 1.0, HEEL: 0.1, AWA: 0.7}


class Stub:
    """One sample every `step` s over `span` s for each path, ending `ages[path]` s ago."""

    def __init__(self, ages, span=3600, step=2.0, start_age=None):
        self.ages, self.span, self.step, self.now = dict(ages), span, step, time.time()
        self.start_age = start_age          # oldest sample this many s ago (a fresh boot)

    def _rows(self, path, minutes):
        if path not in self.ages:
            return []
        end = self.now - self.ages[path]
        lo = self.now - min(minutes * 60, self.span if self.start_age is None else self.start_age)
        out, t, i = [], end, 0
        while t >= lo:
            # a little wander so stdevs are not zero
            out.append((t, VALUES.get(path, 1.0) + (0.01 if i % 2 else -0.01)))
            t -= self.step
            i += 1
        return out[::-1]

    def series(self, path, minutes):
        return self._rows(path, minutes)

    def series_by_source(self, path, minutes):
        return [("src", t, v) for t, v in self._rows(path, minutes)]

    def polars_stw(self):
        return [(8.0, 45.0, 6.0), (8.0, 90.0, 7.0), (12.0, 90.0, 8.0)]

    def latest_value(self, path):
        r = self._rows(path, 60)
        return r[-1][1] if r else None


def use(ds):
    datasource.active = lambda: ds


print("matcher % of polar:")
LIVE3 = {STW: 1, TWS: 1, TWA: 1}
use(Stub(LIVE3, span=600))
matcher._POLAR_TABLE = None
p = matcher._polar_pct()
check("live and full window → ~80%", p is not None and abs(p - 80.0) < 1.5)
use(Stub({STW: 1, TWS: 20, TWA: 1}, span=600))
matcher._POLAR_TABLE = None
check("newest TWS 20 s old aboard → no %", matcher._polar_pct() is None)
use(Stub(LIVE3, span=600, start_age=120))
matcher._POLAR_TABLE = None
check("2 min of a 10-min window (restart) → no %", matcher._polar_pct() is None)
use(Stub(LIVE3, span=600, start_age=360))
matcher._POLAR_TABLE = None
check("6 of 10 min filled → %", matcher._polar_pct() is not None)
os.environ["DATA_SOURCE"] = "cloud"
use(Stub({STW: 1, TWS: 60, TWA: 1}, span=600, step=15))
matcher._POLAR_TABLE = None
check("cloud: newest TWS 60 s old is still live (120 s)", matcher._polar_pct() is not None)
os.environ["DATA_SOURCE"] = "onboard"
matcher._POLAR_TABLE = None

print("fatigue:")
F = {HDG: 1, HEEL: 1, STW: 1, AWA: 1, TWD: 1, TWS: 1, TWA: 1}
use(Stub(F, span=3600, step=5))
r = fatigue._compute()
check("live helm data → an index", r.get("available") is True)
use(Stub(dict(F, **{HDG: 60}), span=3600, step=5))
r = fatigue._compute()
check("heading 60 s old aboard → no index, says so",
      r.get("available") is False and r.get("status") == "stale" and "heading" in r.get("note", ""))
use(Stub(dict(F, **{HEEL: 60}), span=3600, step=5))
r = fatigue._compute()
check("heel 60 s old → heel drops out of the index",
      "heel_instability" not in (r.get("components") or {}))
use(Stub(dict(F, **{HDG: 20}), span=3600, step=5))
check("heading 20 s old (under 30 s) → still an index", fatigue._compute().get("available") is True)

print("wind trend:")
use(Stub({TWS: 45, TWD: 45}, span=4 * 3600, step=30))
r = trend.get_trend()
check("wind 45 s old aboard → 'No live wind' (limit 30 s)",
      r.get("available") is False and "No live wind" in r.get("note", ""))
use(Stub({TWS: 5, TWD: 5}, span=4 * 3600, step=30))
check("wind 5 s old → a trend", trend.get_trend().get("available") is True)
os.environ["DATA_SOURCE"] = "cloud"
use(Stub({TWS: 45, TWD: 45}, span=4 * 3600, step=30))
check("cloud keeps 120 s: 45 s old wind → a trend", trend.get_trend().get("available") is True)
os.environ["DATA_SOURCE"] = "onboard"

print("navigator ETA wind:")
use(Stub({TWS: 5, TWD: 5}, span=1200, step=5))
w = navigator._smoothed_wind()
check("newest wind 5 s old → smoothed wind", w[0] is not None and w[1] is not None)
use(Stub({TWS: 60, TWD: 5}, span=1200, step=5))
check("newest TWS 60 s old aboard → no ETA wind", navigator._smoothed_wind() == (None, None))
os.environ["DATA_SOURCE"] = "cloud"
check("cloud: 60 s old is live (120 s)", navigator._smoothed_wind()[0] is not None)
os.environ["DATA_SOURCE"] = "onboard"

print("chartplotter fix:")
check("gpsout fix limit 5 s", gpsout.FIX_MAX_AGE_S == 5 == staleness.CHARTPLOTTER_FIX_S)
src = open(os.path.join(_root, "pi", "n2kout", "n2kout_app.py"), encoding="utf-8").read()
m = re.search(r'N2KOUT_FIX_MAX_AGE_S", "([0-9.]+)"', src)
check("n2kout's own default matches the table", m and float(m.group(1)) == staleness.CHARTPLOTTER_FIX_S)

print("DATA tile source rule:")
check("a GPS (SOG, 10 Hz) is stale after 5 s aboard", onboard_conditions.source_stale_s([SOG]) == 5)
check("a depth sounder is stale after 10 s", onboard_conditions.source_stale_s([DEPTH]) == 10)
check("a source sending a fast channel goes by it", onboard_conditions.source_stale_s([DEPTH, SOG]) == 5)
check("unknown paths → the default (10 s aboard)", onboard_conditions.source_stale_s(None) == 10)


class SrcDS:
    def sources(self, max_age_min):
        return [{"source": "gps", "last_epoch": time.time() - 7, "paths": 1, "samples": 9,
                 "path_names": [SOG]}]


use(SrcDS())
s = onboard_conditions.get_sources()["sources"][0]
check("/sources carries stale_after_s", s.get("stale_after_s") == 5 and s["last_seen_s"] > 5)
os.environ["DATA_SOURCE"] = "cloud"
check("cloud: sources stay at 45 s", onboard_conditions.source_stale_s([SOG]) == 45)
os.environ["DATA_SOURCE"] = "onboard"

print("cloud web page (per-reading state):")
try:
    from datetime import datetime, timedelta, timezone
    from app import tools

    def _ts(age):
        return datetime.now(timezone.utc) - timedelta(seconds=age)
    ROWS = [{"path": TWS, "source": "a", "value": 5.0, "time": _ts(10)},
            {"path": TWS, "source": "b", "value": 5.0, "time": _ts(50)},
            {"path": TWS, "source": "c", "value": 5.0, "time": _ts(130)}]
    tools._latest_per_source = lambda paths, m: ROWS
    tools._load_priority = lambda: {}
    tools.n2k_sources.devices_for = lambda boat: {}
    os.environ["DATA_SOURCE"] = "cloud"
    cc = tools.get_current_conditions()
    ch = next(c for c in cc["channels"].values() if len(c["readings"]) == 3)
    st = {r["source"]: r["state"] for r in ch["readings"]}
    check("cloud readings 10 / 50 / 130 s → ok / late / dead",
          st == {"a": "ok", "b": "late", "c": "dead"})
finally:
    os.environ["DATA_SOURCE"] = "onboard"
web = open(os.path.join(_root, "vps", "web", "public", "app.js"), encoding="utf-8").read()
check("web page marks readings by state, not a fixed 30 s", "staleMark(r)" in web and "age_s > 30" not in web)

print("overrides (another boat's sensors):")
saved = (dict(staleness.ONBOARD), dict(staleness.CONSUMER_S), staleness.CHARTPLOTTER_FIX_S)
bad = staleness.apply_overrides({"onboard": {"depth": [5, 10, 40], "tws": [1, 2]},
                                 "consumer": {"trend": [45, 120], "nope": [1, 1]},
                                 "chartplotter_fix_s": 3})
check("a boat's slow sounder: depth dead at 40 s", staleness.dead_s(DEPTH, "onboard") == 40)
check("trend limit overridden", staleness.consumer_s("trend", "onboard") == 45)
check("chartplotter limit overridden", staleness.CHARTPLOTTER_FIX_S == 3)
check("bad entries skipped, defaults kept", sorted(bad) == ["consumer.nope", "onboard.tws"]
      and staleness.dead_s(TWS, "onboard") == 15)
staleness.ONBOARD.clear()
staleness.ONBOARD.update(saved[0])
staleness.CONSUMER_S.clear()
staleness.CONSUMER_S.update(saved[1])
staleness.CHARTPLOTTER_FIX_S = saved[2]
check("non-object config is refused", staleness.apply_overrides([1]) == ["not a JSON object"])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
