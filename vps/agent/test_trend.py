"""Wind-trend core — unit test for the bucket-mean rates, circular TWD handling, thin-archive
honesty, and the matcher-signal fields. Stubs the datasource so it runs standalone.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_trend.py
"""
import math

from app import trend

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = 2_000_000_000.0
KN = 1 / 1.943844          # kn → m/s
DEG = math.pi / 180.0


def use_series(tws_fn, twd_fn, minutes_covered=180, step_s=60):
    """Stub the archive: tws_fn/twd_fn(age_s) → kn / deg at that age (age 0 = now)."""
    def series(self, path, minutes):
        rows = []
        for age in range(0, int(minutes_covered * 60), step_s):
            t = NOW - age
            if path == "environment.wind.speedTrue":
                rows.append((t, tws_fn(age) * KN))
            else:
                rows.append((t, (twd_fn(age) % 360) * DEG))
        rows.sort()
        return rows
    trend.datasource.active = lambda: type("S", (), {"series": series})()


# --- steady breeze → available, ~0 rates, "steady" -----------------------------------------------
print("steady:")
use_series(lambda a: 12.0, lambda a: 200.0)
r = trend.get_trend()
print("  ", r.get("read"))
check("available", r["available"])
check("~0 tws rate", abs(r["h1"]["tws_rate_kn_per_hr"]) < 0.2)
check("twd steady", r["h1"]["twd_dir"] == "steady")
check("both windows present", "h1" in r and "h3" in r)

# --- building 2 kn/hr + walking right 8°/hr ------------------------------------------------------
print("building + right:")
use_series(lambda a: 12.0 - 2.0 * a / 3600, lambda a: 220.0 - 8.0 * a / 3600)
r = trend.get_trend()
print("  ", r.get("read"))
check("tws rate ~ +2 kn/hr (1h)", abs(r["h1"]["tws_rate_kn_per_hr"] - 2.0) < 0.4)
check("twd rate ~ +8°/hr, right", r["h1"]["twd_dir"] == "right"
      and abs(r["h1"]["twd_rate_deg_per_hr"] - 8.0) < 2.0)
check("matcher signals mirror the 1h window",
      r["tws_trend_kn_per_hr"] == r["h1"]["tws_rate_kn_per_hr"]
      and r["twd_trend_deg_per_hr"] == r["h1"]["twd_rate_deg_per_hr"])
check("read states from→to degrees",
      str(r["h3"]["twd_from_deg"]) in r["read"] and str(r["h3"]["twd_to_deg"]) in r["read"])

# --- fading + walking left across the 0/360 wrap -------------------------------------------------
print("fading + left across north:")
use_series(lambda a: 15.0 + 1.5 * a / 3600, lambda a: (10.0 + 12.0 * a / 3600))
r = trend.get_trend()
print("  ", r.get("read"))
check("tws rate ~ −1.5 kn/hr", abs(r["h1"]["tws_rate_kn_per_hr"] + 1.5) < 0.4)
check("left across the wrap (no ±360 blowup)", r["h1"]["twd_dir"] == "left"
      and abs(r["h1"]["twd_rate_deg_per_hr"] + 12.0) < 3.0)

# --- thin archive (20 min of data) → honest na ---------------------------------------------------
print("thin archive:")
use_series(lambda a: 12.0, lambda a: 200.0, minutes_covered=20)
r = trend.get_trend()
print("  ", r.get("note"))
check("na when window not half-covered", not r["available"])

# --- no data at all ------------------------------------------------------------------------------
trend.datasource.active = lambda: type("S", (), {"series": lambda self, p, m: []})()
r = trend.get_trend()
check("na with an empty archive", not r["available"])

# --- 1h-only coverage: h3 absent, h1 present -----------------------------------------------------
print("1h coverage:")
use_series(lambda a: 10.0 + 1.0 * a / 3600, lambda a: 200.0, minutes_covered=70)
r = trend.get_trend()
check("h1 present, h3 absent", r["available"] and "h1" in r and "h3" not in r)

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
