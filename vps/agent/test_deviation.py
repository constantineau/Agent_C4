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

# --- the two bands are exported separately: time behind alone has no side (#43) ---------------
print("bands exported separately (#43):")
deviation.reset_state()
set_boat(nm_north(3), LON0, sog=5.0, cog=0.0)
set_now(T0 + (3 / SPEED) * 3600 + 400)               # 400 s behind, on the line
r = deviation.get_deviation()
check("status act from time behind", r["status"] == "act")
check("…but the XTE band is ok", r["xte_status"] == "ok" and r["behind_status"] == "act")

# --- xte trend compares to a sample ≥ TREND_MIN_AGE_S old, not the last call (#43) -------------
print("xte trend over a real interval (#43):")
deviation.reset_state()
t = T0 + (3 / SPEED) * 3600
set_boat(nm_north(3), LON0 + nm_east(0.2), sog=SPEED, cog=0.0)
set_now(t)
deviation.get_deviation()
set_boat(nm_north(3), LON0 + nm_east(0.5), sog=SPEED, cog=0.0)
set_now(t + 61)
a = deviation.get_deviation()["xte_trend"]
b = deviation.get_deviation()["xte_trend"]           # a second caller in the same poll
check("drifting out over a minute → diverging", a == "diverging")
check("…and a back-to-back second call says the same (was 'steady')", b == "diverging")
set_now(t + 61 + 30)                                 # 30 s later, still 0.5 nm out
check("compared to the ≥60 s old sample, not the latest one", deviation.get_deviation()["xte_trend"] == "diverging")
deviation.reset_state()
set_now(t)
deviation.get_deviation()
set_boat(nm_north(3), LON0 + nm_east(0.2), sog=SPEED, cog=0.0)
set_now(t + 10)
check("no sample old enough yet → steady", deviation.get_deviation()["xte_trend"] == "steady")
set_now(t + 65)
check("closing in → converging", deviation.get_deviation()["xte_trend"] == "converging")

# --- graceful na paths ------------------------------------------------------------------------
# --- #183-10: out-and-back — the boat stays on its own leg ------------------------------------
print("out-and-back (#183-10):")
# 5 nm north in 1 nm points, then back south on a line 0.2 nm east. Every point 10 min apart.
OB = ([{"lat": nm_north(i), "lon": LON0, "t": T0 + i * 600} for i in range(6)]
      + [{"lat": nm_north(5 - i), "lon": LON0 + nm_east(0.2), "t": T0 + (6 + i) * 600}
         for i in range(6)])
use_bundle({"race_id": "ob", "recommended": "ob",
            "variants": [{"id": "ob", "name": "Out and back", "route": {"path": OB}}]})
deviation.reset_state()
# outbound, 2 nm up, 0.12 nm east: nearer the RETURN line (0.08) than its own (0.12)
set_boat(nm_north(2), LON0 + nm_east(0.12), cog=0.0)
set_now(T0 + 2 * 600)
r = deviation.get_deviation()
check(f"first read sits on the outbound leg (along {r['along_nm']} nm)", r["along_nm"] < 3.0)
check("time behind ~0, not a leg's worth", abs(r["time_behind_s"] or 0) < 120)
set_boat(nm_north(2.5), LON0 + nm_east(0.13), cog=0.0)
set_now(T0 + 2.5 * 600)
r = deviation.get_deviation()
check(f"next poll stays outbound (along {r['along_nm']} nm)", r["along_nm"] < 3.5)
# just before the turn, still northbound, nearer the return line: not the return leg yet
for k, n in enumerate((3.5, 4.5)):
    set_boat(nm_north(n), LON0 + nm_east(0.13), cog=0.0)
    set_now(T0 + (3 + k) * 600)
    r = deviation.get_deviation()
check(f"pre-turn, northbound, still outbound (along {r['along_nm']} nm)", r["along_nm"] < 5.0)
# round the top and come back down southbound: the return leg is picked up
for k, (n, e, c) in enumerate([(5.0, 0.1, 90.0), (4.5, 0.15, 180.0), (3.0, 0.15, 180.0)]):
    set_boat(nm_north(n), LON0 + nm_east(e), cog=c)
    set_now(T0 + (5 + k) * 600)
    r = deviation.get_deviation()
check(f"after the turn it reads the return leg (along {r['along_nm']} nm)", r["along_nm"] > 6.5)
# a fresh read (no history) on the return leg, heading south, lands on the return leg
deviation.reset_state()
set_boat(nm_north(3.0), LON0 + nm_east(0.08), cog=180.0)
set_now(T0 + 8 * 600)
r = deviation.get_deviation()
check(f"no history, southbound: return leg (along {r['along_nm']} nm)", r["along_nm"] > 6.5)
# no COG, polled every minute: still outbound just before the turn
deviation.reset_state()
for k in range(0, 11):
    n = 3.5 + k * 0.1
    navigator._latest = (lambda n=n: {"lat": nm_north(n), "lon": LON0 + nm_east(0.13), "sog": None,
                                      "cog": None, "tws": None, "twd": None, "heading": None})
    set_now(T0 + n * 600)
    r = deviation.get_deviation()
check(f"no COG, 1-min polls, pre-turn: outbound (along {r['along_nm']} nm)", r["along_nm"] < 5.0)
# a densely sampled straight route: no false deviation from a poll gap
DENSE = [{"lat": nm_north(0.1 * i), "lon": LON0, "t": T0 + i * 60} for i in range(60)]
use_bundle({"race_id": "d", "recommended": "d",
            "variants": [{"id": "d", "name": "Dense", "route": {"path": DENSE}}]})
deviation.reset_state()
set_boat(nm_north(0.05), LON0, sog=6.0, cog=0.0)
set_now(T0 + 30)
deviation.get_deviation()
set_boat(nm_north(0.55), LON0, sog=6.0, cog=0.0)
set_now(T0 + 330)
r = deviation.get_deviation()
check(f"dense straight route after a 5-min gap: on the line (xte {r['xte_nm']}, behind {r['time_behind_s']})",
      r["xte_nm"] < 0.05 and abs(r["time_behind_s"] or 0) < 30)
use_bundle({"race_id": "ob", "recommended": "ob",
            "variants": [{"id": "ob", "name": "Out and back", "route": {"path": OB}}]})
# a late gun (since = 70 min after the plan start): the leg comes from the plan's clock
deviation.reset_state()
set_boat(nm_north(2.0), LON0, sog=0.0, cog=0.0)
set_now(T0 + 70 * 60 + 2 * 600)
r = deviation.get_deviation(since=T0 + 70 * 60)
check(f"late gun, no history: outbound, on pace (along {r['along_nm']}, behind {r['time_behind_s']})",
      r["along_nm"] < 3.0 and abs(r["time_behind_s"] or 0) < 120)
use_bundle(BUNDLE)
deviation.reset_state()

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
