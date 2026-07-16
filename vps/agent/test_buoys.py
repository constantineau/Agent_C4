"""Live buoy observations + the up-course leading indicator — unit test. Stubs the NDBC fetch +
the nav/wind reads so it runs standalone (no network).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_buoys.py
"""
import datetime as dt
import time

from app import buoys, matcher

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def rt2(rows):
    """Fake an NDBC realtime2 text: rows = [(minutes_ago, wspd_ms, wdir_deg)] newest-first."""
    out = ["#YY  MM DD hh mm WDIR WSPD GST  WVHT ...", "#yr  mo dy hr mn degT m/s  m/s  m ..."]
    for mins, ms, wdir in rows:
        t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=mins)
        out.append(f"{t.year} {t.month:02d} {t.day:02d} {t.hour:02d} {t.minute:02d} "
                   f"{wdir:03.0f} {ms:4.1f} 9.0 0.5 x x x")
    return "\n".join(out)


FEEDS = {}


def stub(boat=(45.0, -82.5), cog=0.0, tws_kn=8.0, twd=300.0, nav_brg=None, feeds=None,
         stations=None):
    buoys.clear_cache()
    FEEDS.clear()
    FEEDS.update(feeds or {})
    buoys._fetch = lambda url, timeout: FEEDS[url.split("/")[-1].split(".")[0]]
    buoys.navigator._latest = lambda: {"lat": boat[0], "lon": boat[1], "cog": cog,
                                       "sog": 6.0, "heading": cog,
                                       "tws": tws_kn, "twd": twd}
    buoys.navigator.get_navigator = lambda route=None: (
        {"available": True, "next_mark": {"bearing_deg": nav_brg}} if nav_brg is not None
        else {"available": False})
    if stations is not None:
        buoys.deviation._load_playbook = lambda: {"buoys": stations}
    else:
        buoys.deviation._load_playbook = lambda: {}


# station 20 nm due NORTH of the boat; boat sailing north (up-course), wind from 300 @ 8 kn
ST = [{"id": "45003", "lat": 45.333, "lon": -82.5, "name": "N Huron"}]

print("1) up-course read + deltas vs own wind")
stub(nav_brg=0.0, stations=ST,
     feeds={"45003": rt2([(20, 6.2, 315), (50, 5.6, 312), (110, 5.1, 308)])})
r = buoys.get_buoys()
st = r["stations"][0]
up = r["upcourse"]
print(f"     station: {st['id']} rng={st['range_nm']} up={st['up_course']} "
      f"tws={st['tws_kn']} delta={st.get('delta')} trend={st.get('trend')}")
check("available with a fix + a station in range", r["available"])
check("station classified UP-COURSE (bearing ≈ course to mark)", st["up_course"])
check("obs parsed (m/s → kn)", abs(st["tws_kn"] - 12.1) < 0.2)
check("headline deltas vs own wind (+4.1 kn, +15° right)", up
      and abs(up["tws_delta_kn"] - 4.1) < 0.2 and up["twd_shift_deg"] == 15)
check("trend computed (building + shifting right)", st["trend"]
      and st["trend"]["tws_kn_per_h"] > 0 and st["trend"]["twd_deg_per_h"] > 0)
check("ref source is the course", r["ref_src"] == "course")

print("2) classification + staleness honesty")
stub(nav_brg=180.0, stations=ST,
     feeds={"45003": rt2([(20, 6.2, 315)])})
r = buoys.get_buoys()
check("same station NOT up-course when the course points away",
      not r["stations"][0]["up_course"] and r["upcourse"] is None)
stub(nav_brg=0.0, stations=ST, feeds={"45003": rt2([(150, 6.2, 315)])})
r = buoys.get_buoys()
check("old obs flagged stale, no headline read",
      (r["stations"][0].get("stale") or r["stations"][0]["tws_kn"] is None)
      and r["upcourse"] is None)
stub(nav_brg=0.0, stations=ST, feeds={"45003": ""})
r = buoys.get_buoys()
check("empty feed (out of season) → honest note, still listed",
      r["available"] and r["stations"][0]["tws_kn"] is None)
stub(nav_brg=0.0, stations=[{"id": "45003", "lat": 40.0, "lon": -70.0, "name": "far"}],
     feeds={"45003": rt2([(20, 6.2, 315)])})
check("station beyond range → na with a note", buoys.get_buoys()["available"] is False)

print("3) COG fallback + no fix")
stub(cog=0.0, nav_brg=None, stations=ST, feeds={"45003": rt2([(20, 6.2, 315)])})
r = buoys.get_buoys()
check("no course → COG is the up-course reference", r["ref_src"] == "cog"
      and r["stations"][0]["up_course"])
buoys.navigator._latest = lambda: {"lat": None, "lon": None, "cog": None, "sog": None,
                                   "heading": None, "tws": None, "twd": None}
check("no fix → na", buoys.get_buoys()["available"] is False)

print("4) matcher signals")
stub(nav_brg=0.0, stations=ST,
     feeds={"45003": rt2([(20, 6.2, 315), (50, 5.6, 312)])})
matcher.deviation._load_playbook = lambda: {"buoys": ST}
matcher.deviation.get_deviation = lambda route=None: {"available": False}
matcher.drift_mod.get_drift = lambda route=None: {"available": False}
matcher.tactics.get_tactics = lambda route=None: {"available": False}
matcher.fatigue.get_fatigue = lambda: {}


class DS:
    def get_sail_state(self):
        return {}

    def latest_value(self, path):
        return 8.0 / 1.943844          # raw SI scalar (m/s), as the real datasource returns


matcher.datasource.active = lambda: DS()
sig = matcher.gather()
print(f"     signals: upcourse={sig['upcourse_tws_delta_kn']}/{sig['upcourse_twd_shift_deg']} "
      f"tws={sig['tws_kn']}")
check("up-course deltas land as matcher signals",
      abs(sig["upcourse_tws_delta_kn"] - 4.1) < 0.2 and sig["upcourse_twd_shift_deg"] == 15)
check("tws_kn works on the REAL scalar shape (the fixed bug)", sig["tws_kn"] == 8.0)

print("5) METAR shore stations + over-water headline preference")
import json as _json

def fetch_multi(url, timeout):
    if "aviationweather" in url:
        return FEEDS["metar"]
    return FEEDS[url.split("/")[-1].split(".")[0]]

ST2 = [{"id": "45999", "lat": 45.333, "lon": -82.5, "name": "buoy", "kind": "ndbc"},
       {"id": "KXYZ", "lat": 45.25, "lon": -82.5, "name": "airport", "kind": "metar"}]
now_ep = time.time() - 15 * 60
METAR_JSON = _json.dumps([
    {"icaoId": "KXYZ", "obsTime": now_ep, "wdir": 320, "wspd": 10},
    {"icaoId": "KXYZ", "obsTime": now_ep - 3600, "wdir": 310, "wspd": 8},
    {"icaoId": "KXYZ", "obsTime": now_ep - 1800, "wdir": "VRB", "wspd": 5},   # VRB → skipped
])
stub(nav_brg=0.0, stations=ST2, feeds={"45999": "", "metar": METAR_JSON})
buoys._fetch = fetch_multi
r = buoys.get_buoys()
ap = next(s for s in r["stations"] if s["id"] == "KXYZ")
print(f"     airport: tws={ap.get('tws_kn')} twd={ap.get('twd_deg')} shore={ap.get('shore')} "
      f"trend={ap.get('trend')} headline={r['upcourse'] and r['upcourse']['station']}")
check("METAR parsed (kt, latest first; VRB skipped)", ap.get("tws_kn") == 10 and ap.get("twd_deg") == 320)
check("METAR flagged shore", ap.get("shore") is True)
check("METAR trend from the 3 h history", ap.get("trend") and ap["trend"]["tws_kn_per_h"] > 0)
check("shore station headlines only when no over-water station is up",
      r["upcourse"] and r["upcourse"]["station"] == "KXYZ" and r["upcourse"]["shore"])
stub(nav_brg=0.0, stations=ST2,
     feeds={"45999": rt2([(20, 6.2, 315)]), "metar": METAR_JSON})
buoys._fetch = fetch_multi
r = buoys.get_buoys()
check("over-water buoy takes the headline over a CLOSER shore METAR",
      r["upcourse"] and r["upcourse"]["station"] == "45999" and not r["upcourse"]["shore"])

print("6) actual vs the frozen forecast promise")
now = time.time()
ST3 = [{"id": "45999", "lat": 45.333, "lon": -82.5, "name": "buoy", "kind": "ndbc",
        "promise": [[round(now - 3600), 8.0, 290], [round(now + 3600), 10.0, 310]]}]
stub(nav_brg=0.0, stations=ST3, feeds={"45999": rt2([(20, 6.2, 315)])})
buoys._fetch = fetch_multi
r = buoys.get_buoys()
st = r["stations"][0]
f = st.get("forecast")
print(f"     obs {st['tws_kn']}kn@{st['twd_deg']} vs promise {f and f['tws_kn']}kn@{f and f['twd_deg']} "
      f"→ vs={f and f['vs']}")
check("promise interpolated at the obs moment (~8.7 kn @ ~297°)",
      f and abs(f["tws_kn"] - 8.7) < 0.2 and abs(f["twd_deg"] - 297) <= 1)
check("vs-forecast deltas (obs stronger + right of plan)",
      f and abs(f["vs"]["tws_kn"] - 3.4) < 0.3 and 16 <= f["vs"]["twd_deg"] <= 20)
check("headline carries vs_forecast", r["upcourse"] and r["upcourse"].get("vs_forecast"))

print("7) promise wrap + out-of-window honesty")
ST4 = [{"id": "45999", "lat": 45.333, "lon": -82.5, "name": "buoy", "kind": "ndbc",
        "promise": [[round(now - 3600), 8.0, 350], [round(now + 3600), 8.0, 20]]}]
stub(nav_brg=0.0, stations=ST4, feeds={"45999": rt2([(0, 4.1, 10)])})
buoys._fetch = fetch_multi
f = buoys.get_buoys()["stations"][0].get("forecast")
check("direction interpolates THROUGH north (350→20 ≈ 5°, not 185°)",
      f and (f["twd_deg"] >= 350 or f["twd_deg"] <= 20))
ST5 = [{"id": "45999", "lat": 45.333, "lon": -82.5, "name": "buoy", "kind": "ndbc",
        "promise": [[round(now - 8 * 3600), 8.0, 290], [round(now - 6 * 3600), 10.0, 310]]}]
stub(nav_brg=0.0, stations=ST5, feeds={"45999": rt2([(20, 6.2, 315)])})
buoys._fetch = fetch_multi
check("promise far outside its window → no forecast row (honest)",
      buoys.get_buoys()["stations"][0].get("forecast") is None)

print("RESULT:", "PASS" if ok else "FAIL")
