"""#309: observation stations come from the race's area, not a fixed Great Lakes list.

No network: every station-list read goes through obsarea._get, stubbed here with a small fake of
aviationweather.gov's station list (filtered by the asked box, like the real one) and NDBC's
active station table.
  1) Great Lakes races (Bayview Mackinac, Chicago Mac) get exactly the curated stations they had,
     in the same order, with no fetch at all; venue skill anchors are unchanged too;
  2) a Fort Lauderdale box gets local METAR airports and NDBC buoys, nearest first;
  3) the English Channel gets METAR airports and a plain "NDBC buoys: not here" (NDBC lists a UK
     buoy there, it is still not used); never a Great Lakes station;
  4) a box with nothing near says so; a list that can't load is a note, not an error;
  5) lists are cached: a second lookup does not fetch again;
  6) the frozen bundle list (synthesis._buoy_stations) and the playbook's promise stations come
     from the same lookup, and the bundle's data notes carry the "not here" sentence.
"""
import json
import os
import re
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import obsarea, synthesis, venue  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


AWC = [   # (id, lat, lon, site, siteType)
    ("KFLL", 26.079, -80.162, "Ft Lauderdale-Hollywood Intl", ["METAR", "TAF"]),
    ("KFXE", 26.199, -80.178, "Ft Lauderdale Exec", ["METAR", "TAF"]),
    ("KPBI", 26.683, -80.096, "West Palm Beach Intl", ["METAR", "TAF"]),
    ("KAMX", 25.611, -80.413, "Miami radar", []),                  # no METAR: skipped
    ("EGHI", 50.950, -1.357, "Southampton", ["METAR", "TAF"]),
    ("EGHH", 50.780, -1.842, "Bournemouth", ["METAR", "TAF"]),
    ("KAPN", 45.07, -83.56, "Alpena", ["METAR"]),
]
NDBC_XML = """<?xml version="1.0" encoding="utf-8"?><stations>
<station id="lkwf1" lat="26.612" lon="-80.034" name="Lake Worth" type="fixed" met="y"/>
<station id="FWYF1" lat="25.591" lon="-80.097" name="Fowey Rocks" type="fixed" met="y"/>
<station id="41114" lat="27.551" lon="-80.225" name="Fort Pierce waves" type="buoy" met="n"/>
<station id="62103" lat="49.900" lon="-2.900" name="Channel Lightship" type="buoy" met="y"/>
</stations>"""
FETCHED = []


def fake_get(url, timeout=20):
    FETCHED.append(url)
    if "aviationweather" in url:
        s, w, n, e = (float(x) for x in re.search(r"bbox=([^&]+)", url).group(1).split(","))
        return json.dumps([{"id": i, "icaoId": i, "lat": la, "lon": lo, "site": site, "siteType": t}
                           for i, la, lo, site, t in AWC if s <= la <= n and w <= lo <= e])
    if "activestations" in url:
        return NDBC_XML
    raise AssertionError("unexpected fetch " + url)


obsarea._get = fake_get


def reset():
    obsarea.clear_cache()
    FETCHED.clear()


def old_freeze(bbox, pad=0.6):
    """The pre-#309 candidate list: curated stations in the padded box, in list order."""
    n, s, w, e = bbox
    return [st["id"] for st in venue.STATIONS
            if s - pad <= st["lat"] <= n + pad and w - pad <= st["lon"] <= e + pad]


BAYVIEW = (46.35, 42.5, -85.1, -81.3)       # (n, s, w, e) Port Huron → Mackinac, padded
CHIMAC = (46.35, 41.4, -88.1, -84.2)        # Chicago → Mackinac
FLL = (26.6, 25.6, -80.6, -79.6)
CHANNEL = (50.82, 49.6, -1.8, -0.25)        # off the Isle of Wight
MIDATLANTIC = (31.0, 29.0, -41.0, -39.0)    # open ocean, nothing listed

print("1) Great Lakes: the curated set, same order, no fetch")
for name, box in (("Bayview Mackinac", BAYVIEW), ("Chicago Mac", CHIMAC)):
    reset()
    got = obsarea.find(box)
    check(f"{name}: same stations, same order", [s["id"] for s in got["stations"]] == old_freeze(box),
          [s["id"] for s in got["stations"]])
    check(f"{name}: no fetch, no note", FETCHED == [] and got["notes"] == [], (FETCHED, got["notes"]))
reset()
sk = venue.stations_for(44.5, -82.6)
check("venue skill anchors on Lake Huron unchanged (curated, no fetch)",
      sk and all(s.get("source") is None for s in sk) and FETCHED == [], sk)

print("2) Fort Lauderdale: local airports and buoys")
reset()
got = obsarea.find(FLL)
ids = [s["id"] for s in got["stations"]]
check("METARs near the course, nearest first", ids[:3] == ["KFLL", "KFXE", "KPBI"], ids)
check("NDBC weather stations only (met=y), upper-case ids", set(ids[3:]) == {"LKWF1", "FWYF1"}, ids)
check("no Great Lakes station, no note", not set(ids) & {s["id"] for s in venue.STATIONS}
      and got["notes"] == [], got["notes"])
sk = venue.stations_for(26.1, -80.05)
check("venue skill anchors: KFLL + the nearest buoy",
      {s["id"] for s in sk} == {"KFLL", "FWYF1"}, sk)

print("3) English Channel: airports, and a plain note for buoys")
reset()
got = obsarea.find(CHANNEL)
ids = [s["id"] for s in got["stations"]]
check("METAR airports found, nearest first", ids == ["EGHI", "EGHH"], ids)
check("NDBC not asked off the US, says so", not any("activestations" in u for u in FETCHED)
      and got["notes"] == ["NDBC buoys: not here (US and Caribbean only)."], got["notes"])
check("venue skill: no NDBC anchor off the US", all(s["kind"] == "metar"
                                                     for s in venue.stations_for(50.3, -1.0)))

print("4) nothing near / a list that can't load")
reset()
got = obsarea.find(MIDATLANTIC)
check("open ocean: plain notes, no stations", got["stations"] == [] and got["notes"] == [
    "METAR airports: none near the course.", "NDBC buoys: not here (US and Caribbean only)."],
    got["notes"])
reset()
obsarea._get = lambda url, timeout=20: (_ for _ in ()).throw(OSError("offline"))
got = obsarea.find(FLL)
check("offline: notes, not an error", got["stations"] == [] and got["notes"] == [
    "METAR airports: could not load the station list.",
    "NDBC buoys: could not load the station list."], got["notes"])
check("offline venue skill: Lake Huron unchanged, Florida none",
      venue.stations_for(44.5, -82.6) and venue.stations_for(26.1, -80.05) == [])
obsarea._get = fake_get

print("5) cached")
reset()
obsarea.find(FLL)
n1 = len(FETCHED)
obsarea.find(FLL)
check("second lookup fetches nothing", n1 == 2 and len(FETCHED) == 2, FETCHED)

print("6) the bundle freeze and the playbook use the same lookup")
reset()
synthesis._probe_metars = lambda ids, timeout=12: set(ids)
synthesis._probe_ndbc = lambda sid, timeout=8: sid != "FWYF1"     # one dark buoy drops out
from app import optimizer  # noqa: E402
real_bbox = optimizer.course_bbox
optimizer.course_bbox = lambda d, c: FLL
frozen = synthesis._buoy_stations({}, "c", promises={"KFLL": [[0, 10.0, 90]]})
ids = [s["id"] for s in frozen]
check("Fort Lauderdale bundle: local stations, live ones only",
      ids[:3] == ["KFLL", "KFXE", "KPBI"] and "LKWF1" in ids and "FWYF1" not in ids, ids)
check("METARs frozen as shore, promise attached",
      frozen[0]["shore"] is True and frozen[0]["promise"] == [[0, 10.0, 90]]
      and not next(s for s in frozen if s["id"] == "LKWF1")["shore"])
optimizer.course_bbox = lambda d, c: BAYVIEW
check("Bayview bundle: the curated set as before",
      [s["id"] for s in synthesis._buoy_stations({}, "c")] == old_freeze(BAYVIEW))
optimizer.course_bbox = lambda d, c: CHANNEL
data = synthesis._with_station_notes({"current": "none: x", "notes": ["No current."]}, {}, "c")
check("Channel bundle data says no buoys here", data["notes"] == [
    "No current.", "NDBC buoys: not here (US and Caribbean only)."]
    and data["stations"] == "NDBC buoys: not here (US and Caribbean only).", data)
optimizer.course_bbox = lambda d, c: BAYVIEW
d0 = {"current": "x"}
check("Bayview data untouched", synthesis._with_station_notes(d0, {}, "c") is d0)
optimizer.course_bbox = real_bbox

reset()
far = obsarea.nearest(25.591 - 60.6 / 60.0, -80.097, "ndbc", 60)   # Fowey Rocks 60.6 nm away
near = obsarea.nearest(25.591 - 59.0 / 60.0, -80.097, "ndbc", 60)
check("nearest keeps to its radius (60.6 nm is out, 59 nm is in)",
      far is None and near and near["id"].upper() == "FWYF1", (far, near))

reset()
_real_get = obsarea._get
obsarea._get = lambda url, timeout=20: (FETCHED.append(url) or "") if "aviationweather" in url \
    else _real_get(url, timeout)
got = obsarea.SOURCES["metar"].find((10.0, 9.0, -40.0, -39.0))      # open Atlantic: 204, empty
again = obsarea.SOURCES["metar"].find((10.0, 9.0, -40.0, -39.0))
check("an empty answer (no airports) is no stations, and cached", got == [] and again == []
      and sum("aviationweather" in u for u in FETCHED) == 1, FETCHED)
obsarea._get = _real_get

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
