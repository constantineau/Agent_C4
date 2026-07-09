"""Plan-gap core — unit test for the observed-vs-promise math, time interpolation, clamp/spent
handling, Schmitt bands, and the position-honesty caveat. Stubs the playbook + archive + fix.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_plangap.py
"""
import math

from app import plangap, deviation, navigator

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = 2_000_000_000.0
plangap.time.time = lambda: NOW
KN = 1 / 1.943844
DEG = math.pi / 180.0


def fp_points(twd=200.0, tws=12.0, twd2=None, tws2=None):
    """4 waypoints straddling NOW (2 past, 2 future); optional different promise at the end."""
    twd2 = twd if twd2 is None else twd2
    tws2 = tws if tws2 is None else tws2
    out = []
    for i, dt in enumerate((-7200, -3600, 3600, 7200)):
        f = i / 3.0
        out.append({"lat": 44.0 + i * 0.1, "lon": -82.0, "t": NOW + dt,
                    "twd": twd + f * (twd2 - twd), "tws": tws + f * (tws2 - tws)})
    return out


# deviation and plangap share ONE datasource module — a single stub carries playbook + wind
_STATE = {"bundle": None, "tws": None, "twd": None, "n": 0}


class _Stub:
    def get_playbook(self):
        return _STATE["bundle"]

    def series(self, path, minutes):
        if _STATE["tws"] is None:
            return []
        val = _STATE["tws"] * KN if path == "environment.wind.speedTrue" else _STATE["twd"] * DEG
        return [(NOW - i * 30, val) for i in range(_STATE["n"])]

    def latest_value(self, path):
        return None


deviation.datasource.active = lambda: _Stub()


def use_bundle(fp):
    _STATE["bundle"] = {"race_id": "unit", "recommended": "middle",
                        "forecast_fingerprint": ({"source": "open-meteo-gfs",
                                                  "built_at": NOW - 6 * 3600, "points": fp}
                                                 if fp is not None else None),
                        "variants": [{"id": "middle"}]}


def use_wind(tws_kn, twd_deg, n=20):
    _STATE.update({"tws": tws_kn, "twd": twd_deg, "n": n})


def use_fix(lat, lon):
    navigator._latest = lambda: {"lat": lat, "lon": lon}


# --- promise showed up: observed == interpolated promise → ok ------------------------------------
print("promise held:")
plangap.reset_state()
use_bundle(fp_points(200, 12))          # constant promise 200°/12kn
use_wind(12, 200)
use_fix(44.15, -82.0)                   # near the plan's expected spot
r = plangap.get_plangap()
print("  ", r["status"], "|", r["value"], "|", r["sub"])
check("status ok", r["status"] == "ok")
check("~0 gaps", r["gap_twd_deg"] < 1 and abs(r["gap_tws_kn"]) < 0.5)
check("promise interpolated between brackets", abs(r["plan_twd"] - 200) <= 1)

# --- 22° right of the promise → watch, signed + ---------------------------------------------------
print("22° right of promise:")
plangap.reset_state()
use_bundle(fp_points(200, 12))
use_wind(12, 222)
r = plangap.get_plangap()
print("  ", r["status"], "|", r["value"])
check("watch", r["status"] == "watch")
check("signed + (right)", r["gap_twd_signed_deg"] > 20)
check("why states promised and actual degrees", "200" in r["why"] and "222" in r["why"])

# --- 6 kn light + 35° off → act; hysteresis holds on partial recovery -----------------------------
print("bust (35° + wind gone):")
plangap.reset_state()
use_bundle(fp_points(200, 14))
use_wind(6, 235)
r = plangap.get_plangap()
print("  ", r["status"], "|", r["value"])
check("act", r["status"] == "act")
check("tws gap ~ −8", abs(r["gap_tws_kn"] + 8) < 0.6)
use_wind(11, 214)                        # partially back: 14° / −3 kn — inside commit×rel
r2 = plangap.get_plangap()
check("hysteresis holds watch at least", r2["status"] in ("watch", "act"))

# --- time interpolation: promise rotates 200→230 across the window --------------------------------
print("interpolated promise:")
plangap.reset_state()
use_bundle(fp_points(200, 12, twd2=230, tws2=12))   # brackets 210/220 at ∓1 h → promise 215°
use_wind(12, 215)
r = plangap.get_plangap()
check("promise ~215° at NOW", abs(plangap._ang(215, r["plan_twd"])) <= 2)
check("observed on the interpolated promise → ok", r["status"] == "ok")

# --- position honesty: boat far from the plan's expected spot --------------------------------------
print("position honesty:")
plangap.reset_state()
use_bundle(fp_points(200, 12))
use_wind(12, 235)
use_fix(45.5, -82.0)                     # ~80 nm from the expected spot
r = plangap.get_plangap()
check("far-off-plan caveat in why", "position" in r["why"])
check("plan_pos_off_nm reported", (r["plan_pos_off_nm"] or 0) > 15)

# --- spent timeline → na ---------------------------------------------------------------------------
print("spent timeline:")
plangap.reset_state()
pts = [{"lat": 44, "lon": -82, "t": NOW - 7200, "twd": 200, "tws": 12},
       {"lat": 44.1, "lon": -82, "t": NOW - 3600, "twd": 200, "tws": 12}]
use_bundle(pts)
r = plangap.get_plangap()
check("na once the plan timeline is spent", not r["available"])

# --- pre-start clamp: first point 2 h ahead → compare against it ----------------------------------
print("pre-start clamp:")
plangap.reset_state()
pts = [{"lat": 44, "lon": -82, "t": NOW + 7200, "twd": 210, "tws": 10},
       {"lat": 44.1, "lon": -82, "t": NOW + 10800, "twd": 210, "tws": 10}]
use_bundle(pts)
use_wind(10, 210)
r = plangap.get_plangap()
check("clamps to the first promise pre-start", r["available"] and r["plan_twd"] == 210)

# --- no playbook / no wind → na --------------------------------------------------------------------
_STATE["bundle"] = None
check("na with no playbook", not plangap.get_plangap()["available"])
use_bundle(fp_points())
_STATE.update({"tws": None, "n": 0})
check("na with no own wind", not plangap.get_plangap()["available"])

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
