"""Item C — the debrief bins against the wind the boat MEASURED, not a forecast's idea of it.

Boat-log fixes now carry TWS/TWA/STW straight off the instruments (ranked per second aboard).
`_fix_wind` prefers those; GRIB stays as the fallback for GPX/YB tracks, which carry position
only — and the score says which it used (`wind_source`), because a polar% against forecast wind
and one against measured wind are different claims.

Also here: the stale-config gate. `config_at()` extrapolates the last sail-log entry forever, so
Jul 15's beat home was credited to the spinnaker ("S2 at 30° TWA, 29.9% of polar" — a bin that
would teach the boat model the kite is slow). The gate is PHYSICS (no A*/S* kite below 55° TWA),
never the crossover chart: crew combos the certificate can't rate (A3+SS, S2+SS) are real data
per Cole 2026-09-09, and an implausible window goes to UNATTRIBUTED, never to the wrong sail.

Run in-container:
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \
    -v $PWD/vps/lab/test_debrief_measured_wind.py:/srv/test_debrief_measured_wind.py:ro \
    -w /srv sr33-dev-lab python test_debrief_measured_wind.py
"""
from app import track as T

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784154000.0
# a small real polar neighbourhood: (tws, twa, target_stw)
POLARS = [(10.0, 120.0, 7.59), (10.0, 135.0, 7.17), (10.0, 150.0, 6.36), (10.0, 60.0, 6.5)]


def fixes(n, tws=10.0, twa=120.0, stw=6.83, measured=True):
    out = []
    for i in range(n):
        f = {"t": T0 + i * 60, "lat": 45.0 + i * 1e-3, "lon": -83.0, "sog": stw, "cog": 0.0}
        if measured:
            f.update({"tws": tws, "twa": twa, "stw": stw})
        out.append(f)
    return out


print("_fix_wind:")
f = fixes(1)[0]
check("measured wind on the fix wins, no windfield needed",
      T._fix_wind(f, T0, None, None) == (10.0, 120.0, 6.83))
check("a negative TWA (port tack) folds to its magnitude",
      T._fix_wind({**f, "twa": -120.0}, T0, None, None)[1] == 120.0)
check("no measured wind + no windfield -> None, not a crash",
      T._fix_wind({"lat": 45, "lon": -83, "sog": 6.0, "cog": 0.0}, T0, None, None) is None)


class FakeWF:
    loaded = True

    def wind_at(self, lat, lon, ep):
        return 14.0, 300.0     # a forecast that disagrees with the instruments


check("...and a fix WITH measured wind ignores the windfield entirely",
      T._fix_wind(f, T0, FakeWF(), None)[0] == 10.0)
check("a bare fix falls back to GRIB",
      T._fix_wind({"lat": 45, "lon": -83, "sog": 6.0, "cog": 0.0}, T0, FakeWF(), None)[0] == 14.0)

print("\n_polar_pct from instruments alone:")
seg = fixes(30)
pol = T._polar_pct(seg, [x["t"] for x in seg], None, POLARS)
check(f"polar% computes with NO windfield ({pol and pol['polar_pct']}% vs target 7.59)",
      pol is not None and pol["wind_source"] == "measured" and pol["polar_pct"] == 90)

print("\n_performance_bins + the kite gate:")
LOG = [{"t": T0 - 60, "flying": ["S2"]}]      # kite up, and nobody ever logs the douse
# 40 min running deep under the kite, then the boat hardens up to 60° and beats — the Jul 15 shape
run = fixes(40, twa=150.0, stw=6.0)
beat = [{**x, "twa": 45.0, "tws": 10.0, "stw": 5.9} for x in fixes(40)]  # < the 55° gate; snaps to the 60° cert cell
for i, b in enumerate(beat):
    b["t"] = T0 + (40 + i) * 60
bins = T._performance_bins(run + beat, [x["t"] for x in run + beat], None, POLARS,
                           sail_log=LOG)
by_cfg = {}
for b in bins:
    by_cfg.setdefault(b.get("config"), []).append(b)
check(f"the run bins under S2 (configs seen: {sorted(k or '(none)' for k in by_cfg)})",
      any((b.get("twa") == 150.0) for b in by_cfg.get("S2", [])))
check("the beat does NOT bin under S2 — no kite below 55° TWA, whatever the log says",
      all(b.get("twa") > 100.0 for b in by_cfg.get("S2", [])))
check("...it bins UNATTRIBUTED (config None), never credited to another sail",
      any(b.get("twa") == 60.0 for b in by_cfg.get(None, [])))

# combos the certificate can't rate are untouched — that is real data (Cole)
LOG2 = [{"t": T0 - 60, "flying": ["A3", "SS"]}]
deep = fixes(30, twa=135.0, stw=6.73)
bins2 = T._performance_bins(deep, [x["t"] for x in deep], None, POLARS, sail_log=LOG2)
check("A3+SS at 135° bins as A3+SS — the gate is physics, not the crossover chart",
      any(b.get("config") == "A3+SS" for b in bins2))
check("a staysail alone is never mistaken for a kite (SS has no digit)",
      T._config_plausible("SS+J1", 40.0) is True)
check("a code zero at 50° is left alone — codes point higher than kites",
      T._config_plausible("C0", 50.0) is True)
check("S2 at 40° is implausible", T._config_plausible("S2", 40.0) is False)
check("no TWA at all gates nothing", T._config_plausible("S2", None) is True)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
