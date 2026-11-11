"""Venue study (Lab page): any place in the world, no network.

  1) station discovery finds airports (Iowa State network list) and NDBC stations near any point,
     with names and distances, checks each for data, prefers our curated stations, and survives a
     catalogue that is down;
  2) the study window: race dates (± pad, across New Year) or a month;
  3) a Florida-like study scores every model (HRRR included: it covers Florida), says who was
     closest and who runs light, sums up the usual wind, and saves it;
  4) a later race there picks the study up by itself: venue.resolve() gives the study's key and
     stations (by distance, or by the race's venue_tag), and the optimizer's weights come from
     the study with no new downloads;
  5) outside the US, HRRR is skipped with a plain note and never fetched;
  6) NDBC's historical archive is asked for lower-case file names (upper case is a 404);
  7) the Lab endpoints: validation, the background job, list/get/delete.

All fetchers are stubbed: obs, forecasts, station catalogues.
"""
import datetime as dt
import os
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["LEARNING_DB"] = os.path.join(_tmp, "learning.db")
os.environ["INGESTED_DIR"] = _tmp

from app import modelskill, obsstations, venue, venuestudy  # noqa: E402

modelskill.SKILL_DB = os.environ["LEARNING_DB"]
modelskill.OPENMETEO_MIN_GAP_S = 0.0

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + ("" if cond or not detail else f"  ({detail})"))


def ep(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp()


# ------------------------------------------------------------------------------------- stubs
def _ewkb(s, n, w, e):
    import struct
    pts = [(w, s), (w, n), (e, n), (e, s), (w, s)]
    b = bytes([1]) + struct.pack("<I", 0x20000003) + struct.pack("<I", 4326) + struct.pack("<I", 1)
    b += struct.pack("<I", len(pts)) + b"".join(struct.pack("<dd", x, y) for x, y in pts)
    return b.hex()


NETWORKS = [
    {"id": "FL_ASOS", "name": "Florida ASOS", "tz": "America/New_York", "box": (24.5, 31.0, -87.6, -80.0)},
    {"id": "ES__ASOS", "name": "Spain ASOS", "tz": "Europe/Madrid", "box": (27.6, 43.8, -18.2, 4.3)},
    {"id": "MI_ASOS", "name": "Michigan ASOS", "tz": "America/Detroit", "box": (41.7, 47.5, -90.4, -82.4)},
]
NET_STATIONS = {
    "FL_ASOS": [
        {"id": "FXE", "name": "Fort Lauderdale Exec", "lat": 26.20, "lon": -80.17, "tz": "America/New_York",
         "begin": "1990-01-01", "end": None},
        {"id": "FLL", "name": "Fort Lauderdale Intl", "lat": 26.07, "lon": -80.15, "tz": "America/New_York",
         "begin": "1990-01-01", "end": None},
        {"id": "OLD", "name": "Closed field", "lat": 26.12, "lon": -80.10, "tz": "America/New_York",
         "begin": "1970-01-01", "end": "1999-12-31"},
        {"id": "TLH", "name": "Tallahassee", "lat": 30.39, "lon": -84.35, "tz": "America/New_York",
         "begin": "1990-01-01", "end": None},
    ],
    "ES__ASOS": [{"id": "LEPA", "name": "Palma", "lat": 39.55, "lon": 2.74, "tz": "Europe/Madrid",
                  "begin": "1990-01-01", "end": None}],
    "MI_ASOS": [{"id": "APN", "name": "Alpena", "lat": 45.07, "lon": -83.56, "tz": "America/Detroit",
                 "begin": "1990-01-01", "end": None}],
}
NDBC = [
    {"id": "41122", "name": "Hollywood Beach", "lat": 26.00, "lon": -80.10, "type": "buoy"},
    {"id": "LKWF1", "name": "Lake Worth Pier", "lat": 26.61, "lon": -80.03, "type": "C-MAN"},
    {"id": "NOWND", "name": "Water level only", "lat": 26.09, "lon": -80.11, "type": "water level"},
    {"id": "DRAG1", "name": "Dragonera", "lat": 39.56, "lon": 2.10, "type": "buoy"},
]
obsstations.iem_networks = lambda: NETWORKS
obsstations.iem_network_stations = lambda net: NET_STATIONS[net]
obsstations.ndbc_table = lambda: NDBC

NO_DATA = {"NOWND"}


def obs_for(st, sd, ed):
    """Synthetic truth: wind from 090 (E) at 12 kn, stronger (16) in the local afternoon, hourly,
    plus an extra off-hour METAR that the summary must not double count."""
    if st["id"] in NO_DATA:
        return {}
    lo = dt.datetime.fromisoformat(sd).replace(tzinfo=dt.timezone.utc).timestamp()
    hi = dt.datetime.fromisoformat(ed).replace(tzinfo=dt.timezone.utc).timestamp() + 86400
    out = {}
    t = lo
    while t < hi:
        utc_h = int((t // 3600) % 24)
        local_h = (utc_h + round(st["lon"] / 15.0)) % 24
        out[t] = (16.0 if 12 <= local_h < 18 else 12.0, 90.0)
        t += 3600
    out[lo + 1200] = (30.0, 90.0)          # a special, 20 min after the hour
    return out


FC_CALLS = []
# per model: (speed offset kn, direction offset deg)
FC_ERR = {"gfs": (-2.0, 0.0), "hrrr": (0.4, 5.0), "ecmwf": (1.0, 12.0), "icon": (0.1, 2.0), "gem": (-1.0, -8.0)}


def fake_forecast(lat, lon, sd, ed, model, timeout=45):
    FC_CALLS.append((model, lat, lon, sd))
    if model not in FC_ERR:
        return {}
    ds, dd = FC_ERR[model]
    o = obs_for({"id": "x", "lon": lon}, sd, ed)
    return {e: (s + ds, d + dd) for e, (s, d) in o.items() if e % 3600 == 0}


OBS_CALLS = []


def fake_obs(st, sd, ed):
    OBS_CALLS.append(st["id"])
    return obs_for(st, sd, ed)


modelskill.fetch_forecast = fake_forecast
modelskill.obs_series = fake_obs
modelskill.fetch_reforecast = lambda *a, **k: {}      # the pre-2021 GRIB archive: no network here
TODAY = dt.date(2026, 11, 11)

# ---------------------------------------------------------------------------------------------
print("1) station discovery anywhere")
check("EWKB network box parses", obsstations._ewkb_box(_ewkb(24.5, 31.0, -87.6, -80.0)) == (24.5, 31.0, -87.6, -80.0))
check("a bad box is None, not an error", obsstations._ewkb_box("zz") is None)
win = (2025, "2025-10-14", "2025-11-29")
d = obsstations.discover(26.10, -80.05, 40, window=win)
ids = [s["id"] for s in d["stations"]]
check("finds the Fort Lauderdale airports and buoys in range", {"FXE", "FLL", "41122", "LKWF1"} <= set(ids))
check("leaves out a station 300 nm away", "TLH" not in ids)
check("skips a station whose archive closed before the window", "OLD" not in ids)
check("nearest first, with distances",
      [s["dist_nm"] for s in d["stations"]] == sorted(s["dist_nm"] for s in d["stations"])
      and all(s["dist_nm"] > 0 for s in d["stations"]))
check("names come through", any(s["name"] == "Fort Lauderdale Intl" for s in d["stations"]))
check("each is checked for data in the window",
      all("obs" in s for s in d["stations"]) and next(s for s in d["stations"] if s["id"] == "NOWND")["obs"] == 0)
tick = {s["id"] for s in d["stations"] if s.get("ticked")}
check("ticks the nearest airport and the nearest station with data", tick == {"FLL", "41122"})
check("the airport's time zone rides along", next(s for s in d["stations"] if s["id"] == "FLL")["tz"] == "America/New_York")

g = obsstations.discover(45.07, -83.5, 30, window=win)
apn = [s for s in g["stations"] if s["kind"] == "metar" and s["id"] in ("KAPN", "APN")]
check("our curated KAPN and Iowa State's APN are one station, ours wins",
      len(apn) == 1 and apn[0]["id"] == "KAPN" and apn[0].get("curated") and apn[0].get("ticked"))


class _Down:
    name, kind = "Broken list", "metar"

    def find(self, *a):
        raise OSError("down")


dd_ = obsstations.discover(26.10, -80.05, 40, window=win, sources=[_Down(), obsstations.NdbcSource()])
check("a catalogue that is down is a note, not a failure",
      any("Broken list" in n for n in dd_["notes"]) and any(s["id"] == "41122" for s in dd_["stations"]))
import threading  # noqa: E402
_hold = threading.Event()


def _slow_probe(st_, w_):
    if st_["id"] == "FXE":
        _hold.wait(5)              # a stalled archive
    return 10


t0 = time.time()
sl = obsstations.discover(26.10, -80.05, 40, window=win, probe_fn=_slow_probe, deadline_s=0.5)
_hold.set()
check("a stalled station doesn't hold Find stations: answered by the deadline, marked not checked",
      time.time() - t0 < 3 and next(s_ for s_ in sl["stations"] if s_["id"] == "FXE")["obs"] is None
      and next(s_ for s_ in sl["stations"] if s_["id"] == "FLL")["obs"] == 10
      and any("not checked" in n for n in sl["notes"]))
class _Stalled:
    name, kind = "Slow list", "metar"

    def find(self, *a):
        _hold2.wait(5)
        return []


_hold2 = threading.Event()
t0 = time.time()
sl2 = obsstations.discover(26.10, -80.05, 40, window=win, deadline_s=0.5,
                           sources=[_Stalled(), obsstations.NdbcSource()], probe_fn=lambda s_, w_: 5)
_hold2.set()
check("a stalled station list doesn't hold it either: the others come back, with a note",
      time.time() - t0 < 3 and any(s_["id"] == "41122" for s_ in sl2["stations"])
      and any("Slow list: no answer in time" == n for n in sl2["notes"]))
empty = obsstations.discover(-50.0, -140.0, 20, window=win)
check("nothing in range: a plain note", not empty["stations"] and "bigger radius" in empty["notes"][0])

# ---------------------------------------------------------------------------------------------
print("2) the study window")
sp = venuestudy.window_spec({"start": "2027-11-04", "end": "2027-11-08"})
check("race dates keep the season, padded", sp["start_md"] == [11, 4] and sp["end_md"] == [11, 8]
      and sp["pad_days"] == modelskill.SEASON_PAD_DAYS)
m = venuestudy.window_spec({"month": 2})
check("a month is the whole month, no pad", m["start_md"] == [2, 1] and m["end_md"] == [2, 28] and m["pad_days"] == 0)
for bad in ({}, {"start": "2027-11-08", "end": "2027-11-01"}, {"month": 13}):
    try:
        venuestudy.window_spec(bad)
        check(f"refuses {bad}", False)
    except ValueError:
        check(f"refuses {bad} plainly", True)
ws = modelskill._span_windows((12, 26), (1, 2), 2021, 2026, 0, TODAY)
check("a race over New Year ends in the next year", ws[0] == (2021, "2021-12-26", "2022-01-02"))
check("the window still running this year is cut at yesterday", ws[-1][2] <= "2026-11-10")
check("the station check looks at the last whole past window, not this year's first days",
      venuestudy.probe_window(sp, today=TODAY) == (2025, "2025-10-14", "2025-11-29")
      and venuestudy.probe_window(m, today=TODAY)[0] == 2026)
wins, _ = venuestudy.windows_for(sp, today=TODAY)
check("Open-Meteo years only without the older-years option, this year's part included",
      [w[0] for w in wins] == [2021, 2022, 2023, 2024, 2025, 2026] and wins[-1][2] == "2026-11-10")

# ---------------------------------------------------------------------------------------------
print("3) a Florida-like study")
FL_ST = [dict(s, kind=s.get("kind")) for s in d["stations"] if s.get("ticked")]
FC_CALLS.clear()
logs = []
st = venuestudy.run("Fort Lauderdale", 26.10, -80.05, 40, sp, FL_ST, log=logs.append, today=TODAY)
r = st["result"]
check("saved under the venue cell key", st["key"] == venue._cell_key(26.10, -80.05) == "v26.0N080.0W")
check("all five models scored, HRRR included (it covers Florida)",
      {t["model"] for t in r["table"]} == {"gfs", "hrrr", "ecmwf", "icon", "gem"})
check("verdict: ICON closest, GFS 2 kn light", r["verdict"] == "ICON was closest; GFS runs 2.0 kn light.", r["verdict"])
gfs = next(t for t in r["table"] if t["model"] == "gfs")
ec = next(t for t in r["table"] if t["model"] == "ecmwf")
check("speed bias and direction offset are measured", gfs["speed_bias_kn"] == -2.0 and abs(ec["dir_bias_deg"] - 12) < 0.5)
check("years and sample size per model", gfs["years"] == [2021, 2022, 2023, 2024, 2025, 2026] and gfs["n"] > 1000)
icon = next(t for t in r["table"] if t["model"] == "icon")
check("the closest model gets the biggest weight", icon["weighted"] and icon["weight"] == max(t["weight"] for t in r["table"]))
check("progress text while it runs", any("obs=" in x for x in logs) and logs[-1] == "Done.")
c = next(x for x in r["climate"] if x["station"] == "41122")
check("usual wind: mostly E", c["rose"][2]["dir"] == "E" and c["rose"][2]["pct"] == 100.0)
check("one reading per hour (the 20-min special isn't counted)",
      c["hours"] == sum(t["n"] for t in c["tod"]) and all(r_["bins"][4] == 0 for r_ in c["rose"]))
aft = next(t for t in c["tod"] if t["label"] == "Afternoon")
check("by time of day: afternoon is windier", aft["mean_kn"] >= 15 and "afternoon" in c["line"], aft)
check("time of day is the station's own clock when known",
      next(x for x in r["climate"] if x["station"] == "FLL")["time_label"] == "local time")
check("a buoy borrows the nearest airport's clock", c["time_label"] == "local time")
check("the line is short and plain", c["line"].startswith("Mostly E.") and len(c["line"]) < 80)
rows = modelskill.load_scores(st["key"])
check("model scores land where the optimizer reads them, marked as a study",
      set(rows) == {"gfs", "hrrr", "ecmwf", "icon", "gem"} and all(x["study"] for x in rows.values()))
check("study rows stand until the study is run again", modelskill._fresh(rows))
check("listed", [s["key"] for s in venuestudy.list_all()] == [st["key"]])

# ---------------------------------------------------------------------------------------------
print("4) a later race there picks it up by itself")
bbox = (26.25, 25.98, -80.12, -79.95)          # a buoy course off Fort Lauderdale (n, s, w, e)
v = venue.resolve_from_bbox(bbox, name="ORC NA")
check("venue key matches the study", v["key"] == st["key"])
check("the study's stations anchor it (the curated list has none here)",
      {s_["id"] for s_ in v["stations"]} == {"FLL", "41122"} and v["station"] and v["study"] == "Fort Lauderdale")
off = venue.resolve_from_bbox((26.55, 26.45, -80.05, -79.90))     # 25 nm north, the next cell up
check("a course in the next cell but still near the study uses it",
      off["key"] == st["key"] and venue._cell_key(26.50, -79.975) != st["key"])
tagged = venue.resolve({"name": "far", "venue_tag": st["key"], "courses": []}, "x", bbox=(10.1, 10.0, 10.0, 10.1))
check("or the race names the study with venue_tag", tagged["key"] == st["key"] and tagged["stations"])
FC_CALLS.clear()
OBS_CALLS.clear()
w = modelskill.venue_weights(v, race_date=dt.date(2027, 11, 5))
check("optimizer weights come from the study", w["enabled"] and w["model_weights"]["icon"] > w["model_weights"]["gfs"])
check("no new downloads at route time", not FC_CALLS and not OBS_CALLS)
none = venue.resolve_from_bbox((-33.8, -33.9, 151.2, 151.3))
check("a place with no study and no curated station: no stations, no error",
      none["stations"] == [] and none["station"] is None and none["study"] is None)

# ---------------------------------------------------------------------------------------------
print("5) outside the US: HRRR is skipped plainly")
pal = obsstations.discover(39.50, 2.50, 40, window=win)
PAL_ST = [s_ for s_ in pal["stations"] if s_.get("ticked")]
check("finds Palma's airport and buoy", {s_["id"] for s_ in PAL_ST} == {"LEPA", "DRAG1"})
FC_CALLS.clear()
ps = venuestudy.run("Palma", 39.50, 2.50, 40, venuestudy.window_spec({"month": 8}), PAL_ST, today=TODAY)
pr = ps["result"]
check("HRRR never fetched there", not any(c_[0] == "hrrr" for c_ in FC_CALLS))
check("a plain note says why", "HRRR: not here (US only)" in pr["notes"])
check("the other models still scored", {t["model"] for t in pr["table"]} == {"gfs", "ecmwf", "icon", "gem"})
check("Palma has its own key", ps["key"] != st["key"] and ps["key"] == "v39.5N002.5E")
check("covers(): Florida yes, Palma no", modelskill.covers("hrrr", 26.1, -80.05) and not modelskill.covers("hrrr", 39.5, 2.5))
gl = {"key": "vgl", "stations": [{"id": "KAPN", "kind": "metar", "lat": 45.07, "lon": -83.56}]}
FC_CALLS.clear()
modelskill.refresh_venue_skill(gl, race_date=dt.date(2026, 7, 12), force=True)
check("the race-time path still scores HRRR on the Great Lakes", any(c_[0] == "hrrr" for c_ in FC_CALLS))

# ---------------------------------------------------------------------------------------------
print("6) NDBC archive file names are lower case")
_seen = []
_orig_get = modelskill._get
modelskill._get = lambda url, timeout=45, retries=5: (_seen.append(url), (_ for _ in ()).throw(OSError()))[1]
modelskill.fetch_ndbc_historical("LKWF1", 2025)
modelskill._get = _orig_get
check("asks for lkwf1h2025.txt.gz", _seen and _seen[0].endswith("/lkwf1h2025.txt.gz"))

# ---------------------------------------------------------------------------------------------
print("7) Lab endpoints")
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, main  # noqa: E402

client = TestClient(main.app)
H = {"Authorization": "Bearer " + auth.issue_token()}
check("needs the team login", client.get("/api/venue-study").status_code == 401)
check("lists saved studies", {s_["key"] for s_ in client.get("/api/venue-study", headers=H).json()["studies"]}
      == {st["key"], ps["key"]})
check("opens one", client.get(f"/api/venue-study/{st['key']}", headers=H).json()["name"] == "Fort Lauderdale")
check("unknown study: 404", client.get("/api/venue-study/nope", headers=H).status_code == 404)
bad = client.post("/api/venue-study/stations", headers=H, json={"lat": "x", "month": 11})
check("no place: a plain 400", bad.status_code == 400 and "tap the map" in bad.json()["detail"])
fs = client.post("/api/venue-study/stations", headers=H,
                 json={"lat": 26.10, "lon": -80.05, "radius_nm": 40, "start": "2027-11-04", "end": "2027-11-08"}).json()
check("stations endpoint: found, key, the year it checked",
      fs["key"] == st["key"] and isinstance(fs["checked_year"], int)
      and any(s_["id"] == "41122" for s_ in fs["stations"]))
nost = client.post("/api/venue-study/run", headers=H, json={"lat": 26.1, "lon": -80.05, "month": 11, "stations": []})
check("run with no station ticked: a plain 400", nost.status_code == 400 and "Tick" in nost.json()["detail"])
go = client.post("/api/venue-study/run", headers=H, json={
    "name": "Fort Lauderdale", "lat": 26.10, "lon": -80.05, "radius_nm": 40, "month": 11,
    "stations": [{"id": s_["id"], "kind": s_["kind"], "lat": s_["lat"], "lon": s_["lon"], "name": s_["name"]}
                 for s_ in FL_ST]}).json()
check("run starts a background job", go.get("ok") and go.get("job_id"))
for _ in range(200):
    js = client.get(f"/api/venue-study/status?job_id={go['job_id']}", headers=H).json()
    if js["state"] != "running":
        break
    time.sleep(0.05)
check("the job finishes with the saved study", js["state"] == "done" and js["result"]["key"] == st["key"]
      and js["result"]["spec"]["pad_days"] == 0, js.get("error"))
far = client.post("/api/venue-study/run", headers=H, json={
    "lat": 39.5, "lon": 2.5, "radius_nm": 40, "month": 8,
    "stations": [{"id": s_["id"], "kind": s_["kind"], "lat": s_["lat"], "lon": s_["lon"]} for s_ in FL_ST]})
check("stations from another place are refused (the pin moved after Find stations)",
      far.status_code == 400 and "Find stations again" in far.json()["detail"])
before = venuestudy.load(st["key"])
before_w = modelskill.load_scores(st["key"])
_fc = modelskill.fetch_forecast
modelskill.fetch_forecast = lambda *a, **k: {}
try:
    venuestudy.run("Fort Lauderdale", 26.10, -80.05, 40, sp, FL_ST, today=TODAY)
    check("a re-run with nothing coming back fails plainly", False)
except ValueError as exc:
    check("a re-run with nothing coming back fails plainly", "Try again later" in str(exc))
modelskill.fetch_forecast = _fc
check("...and keeps the last good study and its weights",
      venuestudy.load(st["key"]) == before and len(modelskill.load_scores(st["key"])) == 5)
_fc = modelskill.fetch_forecast
modelskill.fetch_forecast = lambda *a, **k: {}
modelskill.fetch_reforecast = lambda lat, lon, sd, ed, m, y: fake_forecast(lat, lon, sd, ed, "gfs") if m == "gefs" else {}
try:
    venuestudy.run("Fort Lauderdale", 26.10, -80.05, 40, sp, FL_ST, include_deep=True, today=TODAY)
    check("older years with only the reference GEFS line back: fails, keeps the study", False)
except ValueError:
    check("older years with only the reference GEFS line back: fails, keeps the study",
          venuestudy.load(st["key"]) == before and len(modelskill.load_scores(st["key"])) == 5)
modelskill.fetch_forecast = _fc
modelskill.fetch_reforecast = lambda *a, **k: {}
_fc = modelskill.fetch_forecast
modelskill.fetch_forecast = lambda lat, lon, sd, ed, m, timeout=45: _fc(lat, lon, sd, ed, m) if m == "hrrr" else {}
try:
    venuestudy.run("Fort Lauderdale", 26.10, -80.05, 40, sp, FL_ST, today=TODAY)
    check("only one model back: doesn't replace a weighted study", False)
except ValueError as exc:
    check("only one model back: doesn't replace a weighted study",
          "kept" in str(exc) and venuestudy.load(st["key"]) == before and len(modelskill.load_scores(st["key"])) == 5)
modelskill.fetch_forecast = _fc
_orig_save = venuestudy.save
venuestudy.save = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
try:
    venuestudy.run("Fort Lauderdale", 26.10, -80.05, 40, sp, FL_ST, today=TODAY)
except OSError:
    pass
venuestudy.save = _orig_save
check("weights and study are saved together: a failed study write leaves the old weights",
      venuestudy.load(st["key"]) == before
      and modelskill.load_scores(st["key"])["icon"]["updated_at"] == before_w["icon"]["updated_at"])
DEEP = []
_run = venuestudy.run
venuestudy.run = lambda *a, **k: (DEEP.append(k.get("include_deep")), _run(*a, **k))[1]
_bd = modelskill.backfill_deep
modelskill.backfill_deep = lambda *a, **k: DEEP.append("plain")
from app import store  # noqa: E402
_gr = store.get_race
store.get_race = lambda rid: {"race_id": rid, "name": "ORC NA", "courses": []}
venue.resolve = lambda d, c, bbox=None: venue.resolve_from_bbox(bbox or (26.2, 26.0, -80.1, -79.95))
dj = client.post("/api/model-skill/backfill", headers=H, json={"race_id": "orcna"}).json()
for _ in range(200):
    djs = client.get("/api/model-skill/backfill/status", headers=H).json()
    if djs["state"] != "running":
        break
    time.sleep(0.05)
check("'Add older years' on a studied venue deepens the study itself", DEEP == [True] and djs["state"] == "done",
      (DEEP, djs.get("error")))
venuestudy.run, modelskill.backfill_deep, store.get_race = _run, _bd, _gr
_gate = threading.Event()
_obs0 = modelskill.obs_series
modelskill.obs_series = lambda st_, sd_, ed_: (_gate.wait(5), _obs0(st_, sd_, ed_))[1]
_th = threading.Thread(target=lambda: venuestudy.run("Palma", 39.50, 2.50, 40, venuestudy.window_spec({"month": 8}),
                                                     PAL_ST, include_deep=True, today=TODAY))
_th.start()
time.sleep(0.2)
busy = client.delete(f"/api/venue-study/{ps['key']}", headers=H)
try:
    venuestudy.run("Palma", 39.50, 2.50, 40, venuestudy.window_spec({"month": 8}), PAL_ST, today=TODAY)
    second = False
except ValueError as exc:
    second = "already running" in str(exc)
_gate.set()
_th.join()
modelskill.obs_series = _obs0
check("no delete while a run (here the older-years one) is going at that place",
      busy.status_code == 409 and venuestudy.load(ps["key"]))
check("and no second run at the same place meanwhile", second)
check("free again once it's done", not venuestudy.busy(ps["key"]))
check("delete", client.delete(f"/api/venue-study/{ps['key']}", headers=H).json()["deleted"]
      and not modelskill.load_scores(ps["key"]))
check("the Lab shell has the page", 'href="#venue"' in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")).read())

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
