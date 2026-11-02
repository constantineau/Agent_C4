"""Crew alerts (app/alerts.py): which conditions fire, at what severity, and the active-list order.

Covers the rule set `_conditions()` (AIS guard, stale data, depth, polar deficit, persistent wind
shift, helm fatigue) with the tool reads stubbed, and the most-severe-first ordering of
`active_alerts()`. The debounce + DB write path (`evaluate`) needs Postgres and is not covered here.
No engine, DB or network.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_alerts.py
"""
import time

from app import alerts, race_mode, tools

# Practice mode: every rule may fire. With no DB, race mode would fail closed to RACE and withhold
# the advice rules (polar, shift, fatigue); that is pinned in test_alerts_race_mode.py.
race_mode.racing = lambda: False

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


QUIET = {
    "get_ais_targets": lambda *a, **k: {"own_fix": True, "targets": []},
    "get_strip": lambda *a, **k: {"available": True, "data_age_seconds": 2, "depth": 30.0,
                                  "stw": 6.0, "tws": 12.0, "twa": 45.0},
    "get_polar_target": lambda *a, **k: {"target_stw": 6.2},
    "get_tactics": lambda *a, **k: {"available": True, "wind": {"persistent": False, "shift_deg": 2}},
    "get_fatigue": lambda *a, **k: {"available": True, "level": "ok", "index": 20},
}


def stub(**over):
    for name, fn in {**QUIET, **over}.items():
        setattr(tools, name, fn)


def fire(**over):
    stub(**over)
    return {c["key"]: c for c in alerts._conditions()}


def strip(**kw):
    base = dict(QUIET["get_strip"]())
    base.update(kw)
    return lambda *a, **k: base


def ais(*targets, own_fix=True):
    return lambda *a, **k: {"own_fix": own_fix, "targets": list(targets)}


def tgt(mmsi, cpa, tcpa, closing=True, name=None):
    return {"mmsi": mmsi, "name": name, "cpa_nm": cpa, "tcpa_min": tcpa, "closing": closing,
            "bearing": 270}


print("a quiet boat:")
check("nothing fires", fire() == {})

print("AIS guard:")
c = fire(get_ais_targets=ais(tgt("111", 0.8, 15, name="Laker")))
check("closing, CPA 0.8 nm in 15 min → warn", c.get("ais:111", {}).get("severity") == "warn")
check("…named, with CPA and time", "Laker closing" in c["ais:111"]["message"]
      and "0.8 nm in 15 min" in c["ais:111"]["message"])
check("CPA 0.3 nm → danger", fire(get_ais_targets=ais(tgt("1", 0.3, 15)))["ais:1"]["severity"] == "danger")
check("CPA reached in 5 min → danger", fire(get_ais_targets=ais(tgt("1", 0.9, 5)))["ais:1"]["severity"] == "danger")
check("unnamed target falls back to its MMSI", "222 closing" in
      fire(get_ais_targets=ais(tgt("222", 0.5, 10)))["ais:222"]["message"])
check("opening target → nothing", fire(get_ais_targets=ais(tgt("1", 0.2, 5, closing=False))) == {})
check("CPA outside the guard → nothing", fire(get_ais_targets=ais(tgt("1", 1.5, 5))) == {})
check("CPA too far ahead in time → nothing", fire(get_ais_targets=ais(tgt("1", 0.5, 45))) == {})
check("CPA already passed (tcpa ≤ 0) → nothing", fire(get_ais_targets=ais(tgt("1", 0.5, 0))) == {})
check("no own fix → no AIS alerts", fire(get_ais_targets=ais(tgt("1", 0.2, 5), own_fix=False)) == {})
check("one alert per target", set(fire(get_ais_targets=ais(tgt("1", 0.5, 5), tgt("2", 0.5, 5)))) ==
      {"ais:1", "ais:2"})

print("stale data:")
c = fire(get_strip=lambda *a, **k: {"available": False})
check("no live data → danger", c.get("stale_telemetry", {}).get("severity") == "danger")
check("90 s old → warn", fire(get_strip=strip(data_age_seconds=90))["stale_telemetry"]["severity"] == "warn")
check("200 s old → danger", fire(get_strip=strip(data_age_seconds=200))["stale_telemetry"]["severity"] == "danger")
check("30 s old → nothing", "stale_telemetry" not in fire(get_strip=strip(data_age_seconds=30)))

print("depth:")
check("6 m → warn", fire(get_strip=strip(depth=6.0))["depth_shoaling"]["severity"] == "warn")
check("3 m → danger", fire(get_strip=strip(depth=3.0))["depth_shoaling"]["severity"] == "danger")
check("12 m → nothing", "depth_shoaling" not in fire(get_strip=strip(depth=12.0)))
check("no depth reading → nothing", "depth_shoaling" not in fire(get_strip=strip(depth=None)))

print("polar deficit:")
c = fire(get_strip=strip(stw=5.0), get_polar_target=lambda *a, **k: {"target_stw": 6.2})
check("5.0 kn vs 6.2 target (81%) → warn", c.get("polar_deficit", {}).get("severity") == "warn")
check("…says the percent", "81% of 6.2 kts" in c["polar_deficit"]["message"])
check("5.6 vs 6.2 (90%) → nothing", "polar_deficit" not in
      fire(get_strip=strip(stw=5.6), get_polar_target=lambda *a, **k: {"target_stw": 6.2}))
check("no target → nothing", "polar_deficit" not in
      fire(get_strip=strip(stw=1.0), get_polar_target=lambda *a, **k: {}))

print("wind shift:")
shift = {"available": True, "favored_side": "right",
         "wind": {"persistent": True, "shift_deg": 14, "trend": "right"}}
c = fire(get_tactics=lambda *a, **k: shift)
check("persistent 14° → warn", c.get("wind_shift", {}).get("severity") == "warn")
check("…in the crew's words", c["wind_shift"]["message"] == "Wind shifted right 14°. Favour the right side.")
check("negative shift counts by size", "wind_shift" in fire(get_tactics=lambda *a, **k: {
    **shift, "wind": {"persistent": True, "shift_deg": -13, "trend": "left"}}))
check("oscillating 20° → nothing", "wind_shift" not in fire(get_tactics=lambda *a, **k: {
    **shift, "wind": {"persistent": False, "shift_deg": 20}}))
check("persistent 8° → nothing", "wind_shift" not in fire(get_tactics=lambda *a, **k: {
    **shift, "wind": {"persistent": True, "shift_deg": 8}}))

print("fatigue:")
check("rotate_now → warn", fire(get_fatigue=lambda *a, **k: {"available": True, "level": "rotate_now",
                                                             "index": 88.4})["fatigue"]["severity"] == "warn")
check("watch level → nothing", "fatigue" not in
      fire(get_fatigue=lambda *a, **k: {"available": True, "level": "watch", "index": 60}))


def boom(*a, **k):
    raise RuntimeError("engine down")


print("one broken read never hides the others:")
c = fire(get_ais_targets=boom, get_tactics=boom, get_fatigue=boom, get_strip=strip(depth=3.0))
check("depth still fires with AIS, tactics and fatigue throwing", set(c) == {"depth_shoaling"})
c = fire(get_strip=boom, get_ais_targets=ais(tgt("1", 0.3, 4)))
check("AIS still fires with the strip throwing", set(c) == {"ais:1"})

print("active_alerts order:")
now = time.time()
alerts._active_rows = lambda: {
    "a": {"key": "a", "kind": "fatigue", "severity": "warn", "message": "", "raised_epoch": now - 10},
    "b": {"key": "b", "kind": "depth_shoaling", "severity": "danger", "message": "", "raised_epoch": now - 5},
    "c": {"key": "c", "kind": "x", "severity": "info", "message": "", "raised_epoch": now - 1},
    "d": {"key": "d", "kind": "ais", "severity": "warn", "message": "", "raised_epoch": now - 300},
}
order = [a["key"] for a in alerts.active_alerts()]
check(f"danger first, then warn, then info: {order}", order[0] == "b" and order[-1] == "c"
      and set(order[1:3]) == {"a", "d"})
check("get_alerts counts them", alerts.get_alerts()["count"] == 4)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
