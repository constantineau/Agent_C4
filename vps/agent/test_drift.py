"""Forecast-drift core — unit test for the angular drift math, aggregation, horizon guard, and the
fuzzy Schmitt bands. Stubs the playbook source + the live forecast so it runs standalone (no DB / no
network / no playbook file).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_drift.py
"""
import time

from app import drift, deviation, weather

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

NOW = 2_000_000_000.0
drift.time.time = lambda: NOW

# a fingerprint: 4 future waypoints, reference wind from ~200° / 12 kn
def fp_points(twd=200.0, tws=12.0):
    return [{"lat": 44.0 + i * 0.1, "lon": -82.0, "t": NOW + (i + 1) * 3600, "twd": twd, "tws": tws}
            for i in range(4)]

def use_bundle(fp):
    b = {"race_id": "unit", "recommended": "middle",
         "forecast_fingerprint": ({"source": "open-meteo-gfs", "built_at": NOW - 6 * 3600,
                                   "points": fp} if fp is not None else None),
         "variants": [{"id": "middle"}]}
    deviation.datasource.active = lambda: type("S", (), {"get_playbook": lambda self: b})()

def set_live(tws, twd):
    weather.wind_at = lambda lat, lon, epoch: (tws, twd)

# --- no drift: live == reference → ok, ~0° ----------------------------------------------------
print("no drift:")
drift.reset_state(); use_bundle(fp_points(200, 12)); set_live(12, 200)
r = drift.get_drift()
print("  ", r["status"], "|", r["value"], "| twd", r["drift_twd_deg"], "| n", r["n_points"])
check("status ok", r["status"] == "ok")
check("~0° drift", r["drift_twd_deg"] < 1.0)
check("compared all 4 future points", r["n_points"] == 4)

# --- a 22° right shift (clockwise) → watch, direction right -----------------------------------------
print("22° veer:")
drift.reset_state(); use_bundle(fp_points(200, 12)); set_live(12, 222)
r = drift.get_drift()
print("  ", r["status"], "|", r["value"], "| signed", r["drift_twd_signed_deg"], "| dir", r["drift_dir"])
check("status watch (past consider band)", r["status"] == "watch")
check("~22° drift", abs(r["drift_twd_deg"] - 22) < 1)
check("right shift (clockwise, +)", r["drift_dir"] == "right" and r["drift_twd_signed_deg"] > 0)

# --- a 35° left shift (counter-clockwise) → act, direction left -----------------------------------
print("35° back:")
drift.reset_state(); use_bundle(fp_points(200, 12)); set_live(12, 165)
r = drift.get_drift()
print("  ", r["status"], "|", r["value"], "| dir", r["drift_dir"])
check("status act (past commit band)", r["status"] == "act")
check("left shift (counter-clockwise, −)", r["drift_dir"] == "left" and r["drift_twd_signed_deg"] < 0)
check("value says forecast moved", "moved" in r["value"].lower())

# --- wraparound: ref 350°, live 010° = a 20° veer, not 340° -----------------------------------
print("wraparound:")
drift.reset_state(); use_bundle(fp_points(350, 12)); set_live(12, 10)
r = drift.get_drift()
check("wrap handled (~20°, not ~340°)", abs(r["drift_twd_deg"] - 20) < 1)

# --- a big TWS change alone trips the speed band ----------------------------------------------
print("speed drift:")
drift.reset_state(); use_bundle(fp_points(200, 12)); set_live(21, 201)   # +9 kn, dir steady
r = drift.get_drift()
print("  ", r["status"], "| tws", r["drift_tws_kn"])
check("speed band trips act (+9 kn)", r["status"] == "act")
check("tws drift ~+9", abs(r["drift_tws_kn"] - 9) < 0.5)

# --- Schmitt hysteresis on direction: act holds through a mid value ----------------------------
print("hysteresis:")
drift.reset_state(); use_bundle(fp_points(200, 12))
for twd, want in [(235, "act"), (222, "act"), (214, "watch"), (203, "ok")]:
    set_live(12, twd); r = drift.get_drift()
    print(f"   +{twd-200}° → {r['status']} (want {want})")
    check(f"+{twd-200}° → {want}", r["status"] == want)

# --- horizon guard: live None for all → na, not a bogus 0 -------------------------------------
print("na paths:")
drift.reset_state(); use_bundle(fp_points()); weather.wind_at = lambda *a: None
check("forecast unreachable → na", drift.get_drift()["status"] == "na")
# all waypoints in the past → na
drift.reset_state(); set_live(12, 240)
b_past = {"race_id": "u", "forecast_fingerprint": {"built_at": NOW,
          "points": [{"lat": 44, "lon": -82, "t": NOW - 3600, "twd": 200, "tws": 12},
                     {"lat": 44.1, "lon": -82, "t": NOW - 1800, "twd": 200, "tws": 12}]},
          "variants": [{"id": "m"}]}
deviation.datasource.active = lambda: type("S", (), {"get_playbook": lambda self: b_past})()
check("all past → na", drift.get_drift()["status"] == "na")
use_bundle(None)
check("no fingerprint → na", drift.get_drift()["status"] == "na")
deviation.datasource.active = lambda: type("S", (), {"get_playbook": lambda self: {}})()
check("no playbook → na", drift.get_drift()["status"] == "na")

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
