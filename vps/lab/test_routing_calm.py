"""#326: a calm on the course must not kill the route.

A Lab route stopped 30 nm short of its mark with "no clear heading reached the mark". The cause
was a ~6 h calm in the forecast (under 1 kn) before a new breeze filled in. The router dropped
every point where the boat couldn't make 0.3 kn on any heading, so once the calm covered the
whole search front the leg died. A real boat waits (and drifts with any current) for the breeze.

Locked here with a made-up polar and wind (no real boat, no network):
  1) a beat with a 6 h calm in the middle reaches the mark, and its ETA includes the wait;
  2) a becalmed boat drifts with the current while it waits (and never through land);
  3) a calm that lasts until the forecast ends: the leg stops short and says why in plain words
     (not "no clear heading"), and so does the whole-course result;
  4) steady wind is unchanged (no waiting when there is wind).
Run: python3 test_routing_calm.py (lab image, cwd /srv).
"""
from app import optimizer as OPT

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  - ' + extra) if extra else ''}")


# A made-up boat (not any real one's polar). It has no way on in 2 kn of wind or less, so a
# 1 kn current (felt as 1 kn of wind over the water) can't sail it out of a calm either.
SHAPE = [(0, 0.80), (52, 0.85), (60, 0.90), (75, 0.96), (90, 1.00), (110, 0.98),
         (135, 0.90), (150, 0.80), (165, 0.70), (180, 0.62)]
P = []
for tws in (0, 2, 4, 6, 8, 10, 12, 16, 20):
    beat = 46.0 - 0.6 * max(0, tws - 4)
    vmax = 0.0 if tws <= 2 else min(7.4, 1.8 + 0.6 * tws)     # no way on in 2 kn or less
    for twa, f in SHAPE:
        P.append((float(tws), beat if twa == 0 else float(twa), round(vmax * f, 3)))

H = 3600.0


class Calm:
    """8 kn from the north, a flat calm (0.1 kn) from `calm_from` to `calm_to` hours, then 8 kn
    again. No forecast at all after `ends` hours (None, like a real wind field past its window)."""
    loaded = True

    def __init__(self, calm_from=2.0, calm_to=8.0, ends=72.0, twd=0.0):
        self.a, self.b, self.ends, self.twd = calm_from * H, calm_to * H, ends * H, twd

    def wind_at(self, lat, lon, t):
        if t > self.ends:
            return None
        return (0.1, self.twd) if self.a <= t < self.b else (8.0, self.twd)

    def detail_at(self, lat, lon, t):
        w = self.wind_at(lat, lon, t)
        return {"tws": w[0], "twd": w[1], "confidence": 1.0} if w else None

    def status(self):
        return {"models": []}


class East:
    """A steady 1 kn current setting east."""

    def current_at(self, lat, lon, t):
        return (90.0, 1.0)


S_LAT, S_LON = 44.0, -82.3
M_LAT, M_LON = 44.5, -82.3          # 30 nm dead to windward

print("1) a beat with a 6 h calm in the middle")
leg = OPT.route_leg(Calm(), P, S_LAT, S_LON, 0.0, M_LAT, M_LON)
hrs = (leg["eta"] - 0.0) / H
steady = OPT.route_leg(Calm(calm_from=99, calm_to=99), P, S_LAT, S_LON, 0.0, M_LAT, M_LON)
s_hrs = steady["eta"] / H
print(f"     reached={leg['reached']} short={leg['short_nm']} eta={hrs:.1f} h (steady wind {s_hrs:.1f} h)")
check("reaches the mark (was: stopped short, 'no clear heading')", leg["reached"] is True)
check("the ETA includes the ~6 h wait", 5.0 < hrs - s_hrs < 7.5, f"{hrs - s_hrs:.1f} h longer")
check("the leg says how long it waited (becalmed_h ~6)", 5.0 <= leg["becalmed_h"] <= 7.0,
      f"{leg['becalmed_h']} h")

print("2) a becalmed boat drifts with the current while it waits")
leg = OPT.route_leg(Calm(calm_from=0, calm_to=3), P, S_LAT, S_LON, 0.0, M_LAT, M_LON, cur=East())
p3 = [p for p in leg["path"] if p["t"] <= 3 * H + 60]
east_nm = (p3[-1]["lon"] - S_LON) * 60.0 * 0.719
print(f"     after the 3 h calm: {east_nm:.2f} nm east of the start, reached={leg['reached']}")
check("still reaches the mark", leg["reached"] is True)
check("drifted ~3 nm east with the current during the calm", 2.5 < east_nm < 3.5)



class Shore:
    """Land from 0.04 nm east of the start: the 1 kn east current would set the boat onto it."""

    def crosses(self, la1, lo1, la2, lo2):
        edge = S_LON + 0.04 / (60.0 * 0.719)
        return max(lo1, lo2) >= edge

    def blocked(self, la, lo):
        return lo >= S_LON + 0.04 / (60.0 * 0.719)


leg = OPT.route_leg(Calm(calm_from=0, calm_to=3), P, S_LAT, S_LON, 0.0, M_LAT, M_LON, cur=East(),
                    obstacles=Shore())
print(f"     calm + current onto the shore: reached={leg['reached']} stop={leg.get('stop')}")
check("a drift onto the shore isn't waited out at anchor (no route)", leg["reached"] is False
      and leg.get("stop") == "blocked")

print("3) calm until the forecast ends")
leg = OPT.route_leg(Calm(calm_from=2, calm_to=99, ends=20), P, S_LAT, S_LON, 0.0, M_LAT, M_LON)
print(f"     reached={leg['reached']} short={leg['short_nm']} stop={leg.get('stop')}")
check("stops short", leg["reached"] is False and leg["short_nm"] > 10)
check("says the forecast ended (not blocked)", leg.get("stop") == "forecast_end")
COURSE = {"courses": [{"id": "x", "start": {"lat": S_LAT, "lon": S_LON},
                       "marks": [{"name": "Top", "type": "buoy", "lat": M_LAT, "lon": M_LON,
                                  "rounding": "none"}],
                       "finish": {"points": [{"lat": S_LAT, "lon": S_LON}]}}]}
r = OPT.optimize_course(COURSE, "x", 0, Calm(calm_from=2, calm_to=99, ends=20), polar=P,
                        avoid=False, emit_exploration=False, time_budget_s=60)
inc = r.get("incomplete") or {}
print(f"     course: complete={r.get('complete')} reason={inc.get('reason')!r}")
check("course is incomplete at Top", r.get("complete") is False and inc.get("to") == "Top")
check("reason is plain: no wind before the forecast ends",
      "wind" in (inc.get("reason") or "") and "forecast" in (inc.get("reason") or "")
      and "heading" not in (inc.get("reason") or ""))
check("the warning doesn't send you looking for land",
      r.get("warnings") and "land" not in r["warnings"][0] and "forecast" in r["warnings"][0])

print("4) steady wind: no waiting")
check("steady beat reaches", steady["reached"] is True)
check("steady path never holds still", all(
    OPT._hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]) > 0.05
    for a, b in zip(steady["path"], steady["path"][1:])))

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
