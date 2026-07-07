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

print("RESULT:", "PASS" if ok else "FAIL")
