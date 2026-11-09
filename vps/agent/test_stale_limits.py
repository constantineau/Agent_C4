"""Per-channel stale limits (#247, decided by Cole 2026-11-09; docs/STALENESS.md).

Each check feeds an input just past its channel's limit and checks the advice goes while the raw
reading stays. Every one of these failed on the code before #247:

- tactics: a 20 s old TWD aboard made a shift call (no age check on the newest wind)
- plan gap: a 20 s old wind aboard was compared with the plan (window only)
- strip / sail call: a 20 s old TWS/TWA was handed out as current (5-min window), so /sail
  picked a sail from it; the strip now nulls it and keeps it in `raw` with state "dead"
- strip: a 20 s old depth with a live GPS raised no per-channel flag; now `dead_groups`
- raw readings stay until 30 min (they vanished at 5 min), then go
- navigator: a 12 s old fix aboard was used for ETA/laylines (30 s limit)
- collision watch: own fix 10 s aboard (was 30 s), link 10 s (was 30 s)
- failover: a 6 s quiet lead aboard is passed over (was 45 s); the cloud keeps 45 s
- bank: a voltage 90 s old aboard kept its warn/ok verdict (45-min window); now "unknown"
- cloud alert: a dead depth with a fresh GPS raised nothing (freshest-of-all rule)

Run:  DATA_SOURCE=onboard PYTHONPATH=vps/agent:. python3 vps/agent/test_stale_limits.py
"""
import os
import sys
import time

os.environ["DATA_SOURCE"] = "onboard"
os.environ.setdefault("ONBOARD_LIVE_WS", "false")
os.environ.pop("NAV_MAX_AGE_S", None)
os.environ.pop("AIS_OWN_FIX_MAX_AGE_S", None)
os.environ.pop("AIS_LINK_STALE_S", None)

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(os.path.dirname(_here))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import source_policy, staleness  # noqa: E402
from app import datasource, ais, navigator, onboard_conditions, plangap, power, tactics  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


TWS = "environment.wind.speedTrue"
TWD = "environment.wind.directionTrue"
TWA = "environment.wind.angleTrueWater"
LAT = "navigation.position.latitude"
LON = "navigation.position.longitude"
DEPTH = "environment.depth.belowTransducer"
SOG = "navigation.speedOverGround"
COG = "navigation.courseOverGroundTrue"
HDG = "navigation.headingTrue"
BANK = "electrical.batteries.0.voltage"


class Stub:
    """A datasource with one source per path; `ages` = {path: seconds old}."""

    VALUES = {TWS: 6.0, TWD: 3.6, TWA: 0.7, LAT: 45.0, LON: -82.4, DEPTH: 12.0,
              SOG: 3.0, COG: 1.0, HDG: 1.0, BANK: 12.4}

    def __init__(self, ages):
        self.ages = dict(ages)
        self.now = time.time()

    def latest_sample(self, path):
        if path not in self.ages:
            return None
        return (self.VALUES.get(path, 1.0), self.now - self.ages[path])

    def latest_value(self, path):
        s = self.latest_sample(path)
        return s[0] if s else None

    def series(self, path, minutes):
        """One sample a second for 12 min, ending at that path's age."""
        if path not in self.ages:
            return []
        end = self.now - self.ages[path]
        start = self.now - minutes * 60
        n = int(end - start)
        return [(start + i, self.VALUES.get(path, 1.0) + (0.01 * (i % 7))) for i in range(max(0, n))]

    def series_by_source(self, path, minutes):
        return [("n2k-socketcan.3", t, v) for t, v in self.series(path, minutes)]

    def latest_per_source(self, paths, max_age_min):
        out = []
        for p in paths:
            if p in self.ages and self.ages[p] <= max_age_min * 60:
                out.append({"path": p, "source": "n2k-socketcan.3",
                            "value": self.VALUES.get(p, 1.0), "epoch": self.now - self.ages[p]})
        return out

    def sources(self, max_age_min):
        return []

    def __getattr__(self, name):            # anything else the engine asks for: nothing
        return lambda *a, **k: None


def use(ages):
    st = Stub(ages)
    datasource.active = lambda: st
    return st


FRESH = {TWS: 0.5, TWD: 0.5, TWA: 0.5, LAT: 0.5, LON: 0.5, DEPTH: 1.0, SOG: 0.5, COG: 0.5,
         HDG: 0.5, BANK: 1.0}

print("the table (Cole's decisions):")
check("GPS dead 10 s aboard", staleness.dead_s("lat") == 10 and staleness.dead_s("sog") == 10)
check("wind, heading, depth, boat speed dead 15 s aboard",
      all(staleness.dead_s(c) == 15 for c in ("tws", "twd", "heading_true", "depth", "stw")))
check("failover 5 s aboard, 45 s in the cloud",
      staleness.failover_s("twd") == 5 and staleness.failover_s("twd", "cloud") == 45)
check("cloud dead 120 s; raw cap 30 min",
      staleness.dead_s("depth", "cloud") == 120 and staleness.RAW_CAP_S == 1800)

print("tactics:")
use(dict(FRESH, **{TWD: 20.0}))
t = tactics.get_tactics()
check("20 s old TWD aboard → no shift call", t.get("available") is False and "old" in t.get("note", ""))
use(FRESH)
t = tactics.get_tactics()
check("fresh TWD → a call", t.get("available") is True)

print("plan gap:")
use(dict(FRESH, **{TWS: 20.0, TWD: 20.0}))
check("20 s old wind aboard → no observed wind", plangap._observed_wind() == (None, None, 0))
use(FRESH)
check("fresh wind → observed wind", plangap._observed_wind()[0] is not None)

print("strip (sail call, depth, raw):")
onboard_conditions.fatigue.compute_fatigue_index = lambda: {}
use(dict(FRESH, **{TWS: 20.0, TWA: 20.0, DEPTH: 20.0}))
s = onboard_conditions.get_strip()
check("20 s old TWS/TWA → null in the strip (the sail call reads these)",
      s["tws"] is None and s["twa"] is None)
check("…but the raw TWS stays, state dead", s["raw"]["tws"]["state"] == "dead"
      and s["raw"]["tws"]["value"] is not None)
check("20 s old depth with a live GPS → depth null, a 'depth' dead group",
      s["depth"] is None and any(g["group"] == "depth" for g in s["dead_groups"]))
check("the live GPS is not flagged", s["lat"] is not None and "lat" not in s["dead"])
use(dict(FRESH, **{DEPTH: 10 * 60.0}))
s = onboard_conditions.get_strip()
check("depth 10 min old: still on screen (raw), danger", s["raw"].get("depth", {}).get("state") == "dead"
      and next(g for g in s["dead_groups"] if g["group"] == "depth")["severity"] == "danger")
use(dict(FRESH, **{DEPTH: 31 * 60.0}))
s = onboard_conditions.get_strip()
check("depth 31 min old: gone ('—')", "depth" not in s["raw"])

print("navigator:")
use(dict(FRESH, **{LAT: 12.0, LON: 12.0}))
f = navigator._fresh(navigator._latest())
check("12 s old fix aboard → no position", f["lat"] is None and f["lon"] is None)
use(dict(FRESH, **{TWD: 12.0}))
f = navigator._fresh(navigator._latest())
check("12 s old wind aboard → still used (limit 15 s)", f["twd"] is not None)

print("collision watch:")
check("own fix limit 10 s aboard", ais.OWN_FIX_MAX_AGE_S == 10)
check("link limit 10 s", ais.AIS_LINK_STALE_S == 10)
check("cloud own fix 60 s", staleness.COLLISION_FIX_S["cloud"] == 60)

print("failover:")
devices = {}
pick = source_policy.preferred("twd", [("orca", time.time() - 6, 1.0), ("derived", time.time() - 0.2, 2.0)],
                               devices, priority={"twd": ["orca", "derived"]})
check("6 s quiet lead aboard → next rank", pick and pick[0] == "derived")
os.environ["DATA_SOURCE"] = "cloud"
pick = source_policy.preferred("twd", [("orca", time.time() - 6, 1.0), ("derived", time.time() - 0.2, 2.0)],
                               devices, priority={"twd": ["orca", "derived"]})
check("…the cloud keeps the lead until 45 s", pick and pick[0] == "orca")
os.environ["DATA_SOURCE"] = "onboard"

print("bank:")


class Bank:
    def __init__(self, age):
        self.end = time.time() - age

    def series(self, path, minutes):
        return [(self.end - 1200 + i * 10, 12.3) for i in range(121)]

    def latest_sample(self, path):
        return (12.3, self.end)


b = power.assess(Bank(90))
check("90 s old bank aboard → unknown, last volts kept, no projection",
      b.get("status") == "unknown" and b.get("stale") is True and b.get("volts_last") == 12.3
      and b.get("hours_to_floor") is None)
b = power.assess(Bank(5))
check("5 s old bank → judged as before", b.get("status") == "ok" and not b.get("stale"))

print("cloud stale alert per sensor group:")
os.environ["DATA_SOURCE"] = "cloud"
groups = staleness.dead_groups({"lat": 3.0, "sog": 3.0, "depth": 150.0, "tws": 3.0})
check("depth 150 s with a fresh GPS → one 'depth' warn", [g["group"] for g in groups] == ["depth"]
      and groups[0]["severity"] == "warn")
groups = staleness.dead_groups({"depth": 400.0})
check("depth 400 s → danger", groups and groups[0]["severity"] == "danger")
os.environ["DATA_SOURCE"] = "onboard"

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
