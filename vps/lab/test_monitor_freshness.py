"""Monitor fleet freshness — the age the Monitor shows is MEASURED from the fixes (#75).

The UI grill caught the Monitor showing a green "106 boats" pill and "~15 min delayed" over fleet
fixes that were ~102 days old: the pill was green whenever any fix existed, and "~15 min" was the
race config's `delay_min`, not anything measured. The backend now reports the newest fix's age and
a fresh / stale / very_stale verdict; this pins that a months-old feed can never read as fresh.

Run in-container (make ci does this):
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro \
    -v $PWD/vps/lab/test_monitor_freshness.py:/srv/t.py:ro -w /srv sr33-dev-lab python t.py
"""
import time

from app import monitor as M

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"tracker": {"provider": "yb", "race": "bayviewmack2026", "delay_min": 15}}


def yb_payload(ages_s):
    now_ms = time.time() * 1000
    return {"teams": [{"name": f"Boat {i}", "positions": [
        {"latitude": 44.0, "longitude": -82.5, "gpsAtMillis": now_ms - a * 1000 - 600_000},
        {"latitude": 44.1, "longitude": -82.4, "gpsAtMillis": now_ms - a * 1000}]}
        for i, a in enumerate(ages_s)]}


def fleet_with(ages_s):
    real = M._http_json
    M._http_json = lambda url, **kw: yb_payload(ages_s)
    try:
        return M.fleet(DEF)
    finally:
        M._http_json = real


print("the #75 case: a 106-boat feed whose newest fix is ~102 days old")
f = fleet_with([102 * 86400 + i * 60 for i in range(106)])
check("all 106 boats still listed", len(f["fixes"]) == 106)
check("verdict is very_stale, not fresh", f.get("freshness") == "very_stale")
check("age is the NEWEST fix's (~102 days), measured", abs((f.get("newest_age_s") or 0) - 102 * 86400) < 5)
check("configured delay_min is passed through untouched (it's a promise, not a measurement)",
      f["delay_min"] == 15)

print("a healthy race feed: ~15 min behind, newest boat pinged recently")
f = fleet_with([14 * 60, 25 * 60, 40 * 60])
check("fresh", f["freshness"] == "fresh")
check("age is the newest fix (~14 min), not the oldest", abs(f["newest_age_s"] - 14 * 60) < 5)

print("thresholds")
now = 2_000_000_000.0
fx = lambda age: [{"time": now - age}]   # noqa: E731
check("exactly 60 min → fresh", M.freshness(fx(M.FRESH_MAX_S), now)["freshness"] == "fresh")
check("61 min → stale", M.freshness(fx(M.FRESH_MAX_S + 60), now)["freshness"] == "stale")
check("3 h → stale", M.freshness(fx(M.STALE_MAX_S), now)["freshness"] == "stale")
check("3 h + 1 min → very_stale", M.freshness(fx(M.STALE_MAX_S + 60), now)["freshness"] == "very_stale")
check("no fix carries a time → no verdict (UI says 'unknown', never green)",
      M.freshness([{"time": None}], now)["freshness"] is None)
check("no fixes at all → no verdict", M.fleet({})["freshness"] is None)

print("demo fleet (fixes 15 min old) reads fresh")
check("demo fresh", M.fleet({}, demo=True)["freshness"] == "fresh")

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
