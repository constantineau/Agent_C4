"""Live buoy observations + the up-course leading indicator — unit test. Stubs the NDBC fetch +
the nav/wind reads so it runs standalone (no network).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_buoys.py
"""
import datetime as dt
import time

from app import buoys, matcher

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  — {extra}" if extra and not cond else ""))


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

print("8) no bundle, no env: the Great Lakes list only on the Great Lakes (#309)")
import os as _os
_os.environ.pop("BUOY_STATIONS", None)
stub(boat=(45.3, -84.5), nav_brg=0.0, feeds={s["id"]: "" for s in buoys._GL_FALLBACK})
r = buoys.get_buoys()
check("on Lake Huron with no bundle: the curated Great Lakes stations",
      r["available"] and {s["id"] for s in r["stations"]} <= {s["id"] for s in buoys._GL_FALLBACK})
stub(boat=(26.1, -80.0), nav_brg=0.0, feeds={})
buoys._fetch = lambda url, timeout: (_ for _ in ()).throw(AssertionError("fetched " + url))
r = buoys.get_buoys()
check("off Fort Lauderdale with no bundle: a plain note, no Great Lakes station, no fetch",
      r["available"] is False and r["note"] == buoys.NO_STATIONS_NOTE)
FL = [{"id": "LKWF1", "lat": 26.61, "lon": -80.03, "name": "Lake Worth", "kind": "ndbc"}]
stub(boat=(26.1, -80.0), nav_brg=0.0, stations=FL, feeds={"LKWF1": rt2([(20, 6.2, 90)])})
r = buoys.get_buoys()
check("off Fort Lauderdale with a bundle: the bundle's stations",
      r["available"] and [s["id"] for s in r["stations"]] == ["LKWF1"])

print("9) ERDDAP buoy networks (#309): Irish buoys from a frozen playbook, read by kind")
import urllib.parse as _up
from shared import erddap as _erddap


def _iso(mins_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - mins_ago * 60))


def ie_csv(rows, unit="knots"):
    """ERDDAP .csv: header, units row, then rows = [(station, minutes_ago, speed, dir, qc)]."""
    out = ["station_id,time,WindSpeed,WindDirection,QC_Flag", f",UTC,{unit},degrees true,"]
    out += [f"{sid},{_iso(m)},{spd},{d},{qc}" for sid, m, spd, d, qc in rows]
    return "\n".join(out)


IE_SENT = []


def ie_fetch(answer):
    def f(url, timeout):
        IE_SENT.append((url, timeout))
        if "erddap.marine.ie" not in url:
            raise AssertionError("unexpected fetch " + url)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return f


# off SW Ireland, M3 20 nm due north (up-course), M5 well east; wind here 300 @ 8 kn
IE = [{"id": "M3", "lat": 51.55, "lon": -10.5, "name": "M3", "kind": "ie_buoy"},
      {"id": "M5", "lat": 51.69, "lon": -6.70, "name": "M5", "kind": "ie_buoy"}]
IE_ROWS = [("M3", 20, 12.0, 315.0, 0), ("M3", 80, 10.0, 310.0, 1),
           ("M3", 40, 99.0, 200.0, 9),                 # missing-flagged: dropped
           ("M3", 50, "NaN", 312.0, 0), ("M3", 60, "", 312.0, 0),   # unreadable: dropped
           ("M5", 30, 9.0, 250.0, 0)]
stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
IE_SENT.clear()
buoys._fetch = ie_fetch(ie_csv(IE_ROWS))
r = buoys.get_buoys()
by = {s["id"]: s for s in r["stations"]}
q = _up.unquote(IE_SENT[0][0]) if IE_SENT else ""
check("ie_buoy is a registered ERDDAP network", _erddap.network("ie_buoy") is not None)
check("one call for the whole network, short timeout", len(IE_SENT) == 1
      and IE_SENT[0][1] == buoys.FETCH_TIMEOUT, IE_SENT)
check("the query names both stations and a recent window",
      'station_id=~"(M3|M5)"' in q and "time>=" in q and "/tabledap/IWBNetwork.csv?" in q, q)
check("latest M3 reading (knots kept), not the missing-flagged one",
      by["M3"].get("tws_kn") == 12.0 and by["M3"].get("twd_deg") == 315, by["M3"])
check("trend from the readable rows", by["M3"].get("trend")
      and by["M3"]["trend"]["tws_kn_per_h"] > 0, by["M3"].get("trend"))
check("M3 is up-course and leads the headline, over water",
      r["upcourse"] and r["upcourse"]["station"] == "M3" and not r["upcourse"]["shore"], r["upcourse"])
check("M5 read too", by["M5"].get("tws_kn") == 9.0)
check("kind kept on the row, source named", by["M3"]["kind"] == "ie_buoy"
      and "erddap_buoys" in r["based"])
buoys._fetch = ie_fetch(AssertionError("cached"))
check("cached ~10 min: no second call", buoys.get_buoys()["stations"][0].get("tws_kn") == 12.0)

stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
buoys._fetch = ie_fetch(ie_csv(IE_ROWS, unit="m/s"))
check("m/s converted to knots",
      {s["id"]: s.get("tws_kn") for s in buoys.get_buoys()["stations"]}["M3"] == round(12.0 * 1.943844, 1))
stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
buoys._fetch = ie_fetch(ie_csv(IE_ROWS, unit="furlongs"))
r = buoys.get_buoys()
check("a unit we don't know: no reading, never a guess",
      all(s["tws_kn"] is None and s["note"] == "no recent reading" for s in r["stations"]))

stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
buoys._fetch = ie_fetch(ie_csv([("M3", 20, 12.0, 315.0, 0)]))
by = {s["id"]: s for s in buoys.get_buoys()["stations"]}
check("a station not reporting: plain note, no error",
      by["M5"]["tws_kn"] is None and by["M5"]["note"] == "no recent reading" and by["M3"]["tws_kn"] == 12.0)

stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
buoys._fetch = ie_fetch(OSError("Your query produced no matching results. (404)"))
r = buoys.get_buoys()
check("offline / nothing in the window: every station noted, no error",
      r["available"] and all(s["note"] == "no recent reading" for s in r["stations"]))
stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE)
buoys._fetch = ie_fetch("<html>Service Unavailable</html>")
check("a page that isn't a table: no readings",
      all(s["tws_kn"] is None for s in buoys.get_buoys()["stations"]))

stub(boat=(51.217, -10.5), nav_brg=0.0, stations=IE[:1])
buoys._fetch = ie_fetch(ie_csv([("M3", 120, 12.0, 315.0, 0), ("M3", 180, 11.0, 310.0, 0)]))
st = buoys.get_buoys()["stations"][0]
check("an hourly network posting late: a 2 h old reading still counts (its own stale limit)",
      st["stale"] is False and st.get("delta") is not None and st["age_min"] == 120, st)
buoys._fetch = ie_fetch(ie_csv([("M3", 170, 12.0, 315.0, 0)]))
buoys.clear_cache()
st = buoys.get_buoys()["stations"][0]
check("...but not one older than that", st["stale"] is True and "delta" not in st, st)

print("10) the id as the server spells it; a kind with no reader")
LOW = [{"id": "AB1", "src_id": "ab1", "lat": 51.55, "lon": -10.5, "name": "AB1", "kind": "ie_buoy"}]
stub(boat=(51.217, -10.5), nav_brg=0.0, stations=LOW)
IE_SENT.clear()
buoys._fetch = ie_fetch(ie_csv([("ab1", 20, 7.0, 90.0, 0)]))
st = buoys.get_buoys()["stations"][0]
check("asks the server for src_id, files the reading under the frozen id",
      'station_id="ab1"' in _up.unquote(IE_SENT[0][0]) and st["id"] == "AB1" and st["tws_kn"] == 7.0,
      (IE_SENT, st))
ODD = [{"id": "X1", "lat": 51.55, "lon": -10.5, "name": "X1", "kind": "uk_buoy"}]
stub(boat=(51.217, -10.5), nav_brg=0.0, stations=ODD)
buoys._fetch = lambda url, timeout: (_ for _ in ()).throw(AssertionError("fetched " + url))
st = buoys.get_buoys()["stations"][0]
check("a kind the boat can't read: listed with a plain note, never fetched from NDBC",
      st["tws_kn"] is None and st["note"] == buoys.NO_READER_NOTE, st)
_os.environ["BUOY_STATIONS"] = "M3:51.55:-10.5:M3 buoy:ie_buoy;45003:45.3:-82.5"
stub(boat=(51.217, -10.5), nav_brg=0.0)
got = buoys.stations()
_os.environ.pop("BUOY_STATIONS", None)
check("BUOY_STATIONS takes an optional kind", got[0].get("kind") == "ie_buoy"
      and "kind" not in got[1], got)

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
