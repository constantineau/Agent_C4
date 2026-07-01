"""Route-deviation core — unit test for the projection math (XTE/side, along-track, time-behind,
VMC) and the fuzzy Schmitt bands + hysteresis. Stubs the data source + live position so it runs
standalone (no DB / no live GPS / no playbook file).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_deviation.py
  or inside the engine container:  docker ... exec -w /srv engine python test_deviation.py
"""
import time

from app import deviation, navigator

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# A straight 10 nm course due NORTH from (44.0, -82.0), planned at 6 kn → 100 min, a point / 2 nm.
LAT0, LON0 = 44.0, -82.0
def nm_north(nm): return LAT0 + nm / 60.0
SPEED = 6.0                       # kn → 2 nm per 1200 s
NOW = 1_700_000_000.0
T0 = NOW                          # plan start epoch (overridden per case via monkeypatched time)
PATH = [{"lat": nm_north(2 * i), "lon": LON0, "t": T0 + (2 * i / SPEED) * 3600} for i in range(6)]

BUNDLE = {
    "race_id": "unit_race", "recommended": "middle", "headline": "test gameplan",
    "variants": [{"id": "middle", "name": "Middle start",
                  "what_flips_it": "a persistent right shift past 020°",
                  "route": {"path": PATH}}],
}

class FakeSource:
    def __init__(self, blob): self._blob = blob
    def get_playbook(self): return self._blob

def use_bundle(b): deviation.datasource.active = lambda: FakeSource(b)
def set_boat(lat, lon, sog=SPEED, cog=0.0):
    navigator._latest = lambda: {"lat": lat, "lon": lon, "sog": sog, "cog": cog,
                                 "tws": None, "twd": None, "heading": cog}
def set_now(t): deviation.time.time = lambda: t

use_bundle(BUNDLE)
# east/west offset in degrees lon for a given nm at this latitude
import math
def nm_east(nm): return nm / (60.0 * math.cos(math.radians(LAT0)))

# --- projection: ON the track at 3 nm along, on plan pace -------------------------------------
print("on-track:")
deviation.reset_state()
set_boat(nm_north(3), LON0, sog=SPEED, cog=0.0)
set_now(T0 + (3 / SPEED) * 3600)         # plan says 30 min in; make now exactly that → 0 behind
r = deviation.get_deviation()
print("  ", r["status"], "|", r["value"], "| xte", r["xte_nm"], "| along%", r["along_pct"],
      "| behind", r["time_behind_s"], "| vmc", r["vmc_kn"], "/", r["vmc_optimal_kn"])
check("status ok on the line", r["status"] == "ok")
check("xte ~0", r["xte_nm"] < 0.05)
check("along ~30%", abs(r["along_pct"] - 30) <= 1)
check("time_behind ~0", abs(r["time_behind_s"]) <= 2)
check("vmc == optimal (6 kn)", abs(r["vmc_kn"] - 6.0) < 0.1 and abs(r["vmc_optimal_kn"] - 6.0) < 0.1)
check("carries the variant + trigger", r["variant"] == "middle" and "right shift" in r["what_flips_it"])

# --- off to the EAST (= right of a northbound track) by ~1.2 nm → commit band ------------------
print("off-track east:")
deviation.reset_state()
set_boat(nm_north(3), LON0 + nm_east(1.2), sog=SPEED, cog=0.0)
set_now(T0 + (3 / SPEED) * 3600)
r = deviation.get_deviation()
print("  ", r["status"], "|", r["value"], "| xte", r["xte_nm"], r["xte_side"])
check("xte ~1.2 nm", abs(r["xte_nm"] - 1.2) < 0.1)
check("east = right side", r["xte_side"] == "right")
check("status act (past commit band)", r["status"] == "act")
check("value names the side", "right" in r["value"])

# --- Schmitt hysteresis: act holds through a mid-band, relaxes only well below -----------------
print("hysteresis:")
deviation.reset_state()
set_now(T0 + (3 / SPEED) * 3600)
for xte, want in [(1.2, "act"), (0.9, "act"), (0.5, "watch"), (0.2, "ok")]:
    set_boat(nm_north(3), LON0 + nm_east(xte), sog=SPEED, cog=0.0)
    r = deviation.get_deviation()
    print(f"   xte {xte} → {r['status']} (want {want})")
    check(f"xte {xte} → {want}", r["status"] == want)

# --- behind on plan pace but on the line → time-behind drives, attributed to speed/mode -------
print("behind on the line:")
deviation.reset_state()
set_boat(nm_north(3), LON0, sog=5.0, cog=0.0)       # slow → VMC deficit
set_now(T0 + (3 / SPEED) * 3600 + 200)               # 200 s later than plan
r = deviation.get_deviation()
print("  ", r["status"], "|", r["value"], "| behind", r["time_behind_s"], "| vmcdef", r["vmc_deficit_kn"])
check("behind ~200 s", abs(r["time_behind_s"] - 200) <= 3)
check("status watch (consider band)", r["status"] == "watch")
check("vmc deficit ~1 kn", abs(r["vmc_deficit_kn"] - 1.0) < 0.15)
check("why attributes to speed/mode on the line", "speed" in r["why"].lower() or "mode" in r["why"].lower())

# --- ahead of plan → no behind alarm ----------------------------------------------------------
print("ahead of plan:")
deviation.reset_state()
set_boat(nm_north(3), LON0, sog=SPEED, cog=0.0)
set_now(T0 + (3 / SPEED) * 3600 - 200)               # 200 s early
r = deviation.get_deviation()
check("time_behind negative (ahead)", r["time_behind_s"] < 0)
check("status ok when ahead + on line", r["status"] == "ok")

# --- graceful na paths ------------------------------------------------------------------------
print("na paths:")
use_bundle({})
check("no playbook → na", deviation.get_deviation()["status"] == "na")
use_bundle({"variants": [{"id": "x", "route": {"path": []}}]})
check("empty path → na", deviation.get_deviation()["status"] == "na")
use_bundle(BUNDLE)
navigator._latest = lambda: {"lat": None, "lon": None, "sog": None, "cog": None}
check("no fix → na", deviation.get_deviation()["status"] == "na")

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
