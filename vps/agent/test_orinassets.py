"""The Orin re-route's chart + boat profile on the Pi (vps/agent/app/orinassets.py, #139, #140).

Pins:
  * a Deploy payload (land-v2 chart with a fingerprint, reviewed boat profile + version) is stored
    in one file and goes into every re-route request; the chart only for the course aboard;
  * refused, and nothing changes, when: a race session is running (also when the race log can't
    be read), the chart isn't land-v2, has no fingerprint, is for another course, is empty without
    the open-water flag, or its mask doesn't match its grid; the profile has errors or doesn't
    match its version;
  * a push without a chart (or profile) drops the old one: an older asset never passes as this
    push's;
  * a race session can't start between the check and the write (the plume's lock);
  * a failed write leaves the old file whole, no temp files;
  * the real engine endpoints.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_orinassets.py
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import zlib

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from shared import boat_profile  # noqa: E402

from app import orinassets as OA, orinroute, plume, racelog, deviation  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def mask(nx, ny, land=True):
    m = bytearray(nx * ny)
    if land:
        m[5] = 1
    return base64.b64encode(zlib.compress(bytes(m), 9)).decode()


CHART = {"schema": "c4.obstacles/v1", "bbox": [46.0, 42.5, -83.5, -81.5], "res_deg": 0.5,
         "nx": 4, "ny": 7, "layers": {"coastline": 1}, "source": "gshhg", "safety_depth": 2.63,
         "active": True, "mask_zb64": mask(4, 7), "open_water": False, "race_id": "r1",
         "course_id": "c", "kind": "land-v2", "fingerprint": "0123456789abcdef"}
BOAT = {"schema_version": "0.1", "boat_id": "sr33", "name": "SR33", "draft_m": 2.1336,
        "helm_factor": 0.931, "polar_adjustments": [{"tws": 10, "twa": 90, "mult": 1.03}] * 24}
GOOD = {"schema": "c4.orin-assets/v1", "race_id": "r1", "course_id": "c", "built_at": 1,
        "chart": CHART, "boat": BOAT, "boat_version": boat_profile.version(BOAT)}
PATH = OA._path()
_ORIG_RS = plume.race_session          # the real check, against a stub race log, below
plume.race_session = lambda: None

print("nothing pushed yet:")
check("GET says none, short", OA.get() == {"available": False, "note": "No chart or boat profile aboard."})
check("no assets → none in the request (the Orin uses its own files, as before)",
      OA.for_run({"race_id": "r1"}, "c") is None)

print("a Deploy push:")
r = OA.load(GOOD, now=1000)
check("loaded, with what it holds", r["loaded"] and r["chart"] and r["boat_id"] == "sr33"
      and r["boat_version"] == GOOD["boat_version"] and r["polar_cells"] == 24
      and r["helm_factor"] == 0.931)
g = OA.get()
check("GET: race, course, chart, boat id + version, when", g["available"] and g["course_id"] == "c"
      and g["chart"] and g["boat_version"] == GOOD["boat_version"] and g["received_at"] == 1000)
fr = OA.for_run({"race_id": "r1"}, "c")
check("the request gets the chart and the profile", fr["chart"] == CHART and fr["boat"] == BOAT
      and fr["boat_version"] == GOOD["boat_version"] and fr["received_at"] == 1000)
check("another course aboard → no chart, the profile still goes",
      OA.for_run({"race_id": "r1"}, "other")["chart"] is None
      and OA.for_run({"race_id": "r2"}, "c")["chart"] is None
      and OA.for_run(None, None)["chart"] is None
      and OA.for_run({"race_id": "r2"}, "c")["boat"] == BOAT)

print("refused, and what was held stays:")
before = open(PATH).read()
BAD = [
    ("not the schema", dict(GOOD, schema="x")),
    ("no course id", dict(GOOD, course_id="")),
    ("chart of the old kind", dict(GOOD, chart=dict(CHART, kind="land"))),
    ("chart without a fingerprint", dict(GOOD, chart={k: v for k, v in CHART.items() if k != "fingerprint"})),
    ("chart for another course", dict(GOOD, chart=dict(CHART, course_id="z"))),
    ("chart for another race", dict(GOOD, chart=dict(CHART, race_id="r2"))),
    ("empty chart (shoreline didn't load)", dict(GOOD, chart=dict(CHART, mask_zb64=mask(4, 7, False)))),
    ("mask that doesn't match its grid", dict(GOOD, chart=dict(CHART, nx=5))),
    ("mask not base64", dict(GOOD, chart=dict(CHART, mask_zb64="not base64 !!"))),
    ("mask not zlib", dict(GOOD, chart=dict(CHART, mask_zb64="aGVsbG8gd29ybGQ="))),
    ("a huge grid", dict(GOOD, chart=dict(CHART, nx=100000, ny=100000))),
    ("tiny cells with nx = ny = 1 (16 TB on the Orin)",
     dict(GOOD, chart=dict(CHART, bbox=[46, 42, -84, -80], res_deg=0.000001, nx=1, ny=1,
                           mask_zb64=base64.b64encode(zlib.compress(b"\x01")).decode()))),
    ("a box past the cell cap, stated to match", dict(GOOD, chart=dict(CHART, res_deg=0.0001,
                                                                        nx=20000, ny=35000))),
    ("reversed box", dict(GOOD, chart=dict(CHART, bbox=[42.5, 46.0, -83.5, -81.5]))),
    ("NaN in the box", dict(GOOD, chart=dict(CHART, bbox=[float("nan"), 42.5, -83.5, -81.5]))),
    ("zero cell size", dict(GOOD, chart=dict(CHART, res_deg=0))),
    ("no box", dict(GOOD, chart={k: v for k, v in CHART.items() if k != "bbox"})),
    ("profile for another boat than named", dict(GOOD, boat_id="bravo")),
    ("chart not a dict", dict(GOOD, chart="x")),
    ("profile with errors", dict(GOOD, boat={"boat_id": "sr33"},
                                 boat_version=boat_profile.version({"boat_id": "sr33"}))),
    ("profile that doesn't match its version", dict(GOOD, boat_version="0" * 12)),
    ("profile not a dict", dict(GOOD, boat=["x"])),
]
for name, body in BAD:
    r = OA.load(body, now=2000)
    check(f"{name} → refused", r["loaded"] is False and r["detail"])
check("…and the held file never changed", open(PATH).read() == before)
check("an empty chart flagged open water is taken",
      OA.load(dict(GOOD, chart=dict(CHART, mask_zb64=mask(4, 7, False), open_water=True)),
              now=2100)["loaded"])
OA.load(GOOD, now=2200)

print("racing:")
plume.race_session = lambda: {"kind": "race", "start_ts": 1}
before = open(PATH).read()
r = OA.load(dict(GOOD, course_id="c2", chart=dict(CHART, course_id="c2")), now=3000)
check("race session running → refused, short", r == {"loaded": False, "detail": "Racing now. Not loaded."})
check("…the held file is unchanged", open(PATH).read() == before)
check("…and the held assets still go with a re-route", OA.for_run({"race_id": "r1"}, "c")["chart"] == CHART)
plume.race_session = _ORIG_RS
import types  # noqa: E402
_src = racelog._src
racelog._src = lambda: (_ for _ in ()).throw(OSError("db locked"))
r = OA.load(GOOD, now=3100)
racelog._src = _src
check("race log unreadable → treated as racing (fail closed)", r["loaded"] is False
      and r["detail"] == OA.RACING)
racelog._src = lambda: types.SimpleNamespace(session_current=lambda: {"kind": "practice"})
check("a practice session doesn't block it", OA.load(GOOD, now=3200)["loaded"])
racelog._src = _src
plume.race_session = lambda: None

print("a race session can't start between the check and the write:")
seen = {}
os.remove(PATH)


def session_starts_meanwhile():
    def tap():
        with racelog.SESSION_LOCK:            # what racelog.start() takes
            seen["file_when_session_started"] = os.path.exists(PATH)
    th = threading.Thread(target=tap)
    th.start()
    time.sleep(0.15)
    seen["thread"] = th
    return None


plume.race_session = session_starts_meanwhile
OA.load(GOOD, now=4000)
seen["thread"].join()
plume.race_session = lambda: None
check("the session start waited for the write", seen.get("file_when_session_started") is True)

print("a push without a chart or profile drops the old one:")
r = OA.load(dict(GOOD, chart=None), now=5000)
check("loaded, says no chart", r["loaded"] and r["chart"] is False)
check("no chart goes to the Orin, the profile does", OA.for_run({"race_id": "r1"}, "c")["chart"] is None
      and OA.for_run({"race_id": "r1"}, "c")["boat"] == BOAT)
OA.load(dict(GOOD, boat=None, boat_version=None), now=5100)
fr = OA.for_run({"race_id": "r1"}, "c")
check("no profile → none sent (the Orin warns)", fr["boat"] is None and fr["boat_version"] is None
      and fr["chart"] == CHART)

print("a failed write:")
OA.load(GOOD, now=6000)
before = open(PATH).read()
_replace = os.replace


def broken_replace(a, b):
    raise OSError("disk full")


os.replace = broken_replace
try:
    OA.load(dict(GOOD, chart=None), now=6100)
    check("raises", False)
except OSError:
    check("raises (the endpoint answers 500, the Lab marks the step failed)", True)
os.replace = _replace
check("…the old file is whole", open(PATH).read() == before)
check("…no temp files left", not [n for n in os.listdir(os.path.dirname(PATH)) if n.endswith(".tmp")])
with open(PATH, "w") as f:
    f.write('{"received_at": 1, "assets": ')            # torn by hand
check("a torn file reads as nothing pushed, not a crash", OA.for_run({"race_id": "r1"}, "c") is None
      and OA.get()["available"] is False)

print("the re-route request carries them:")
MARKS = [{"seq": 1, "name": "Start", "lat": 43.0, "lon": -82.4},
         {"seq": 2, "name": "Finish", "lat": 45.84, "lon": -84.62}]
NAV.get_navigator = lambda route=None: {"available": True, "route": "r1",
                                        "next_mark": {"name": "Finish", "index": 1}}
NAV._marks = lambda route: MARKS
NAV._latest = lambda: {"lat": 44.4, "lon": -81.9}
deviation._load_playbook = lambda: None
os.remove(PATH)
req, why = orinroute.build_request("iPad 1")
check("nothing pushed → no assets key", why is None and "assets" not in req)
OA.load(GOOD, now=7000)
DEF = {"race_id": "r1", "courses": [{"id": "c", "start": {"lat": 43.0, "lon": -82.4}, "marks": [],
                                     "finish": {"points": [{"lat": 45.84, "lon": -84.62}]}}]}
orinroute.save_definition(DEF, "c", "r1")
req, why = orinroute.build_request("iPad 1")
check("with the course aboard: chart + profile in the request", req["assets"]["chart"] == CHART
      and req["assets"]["boat"] == BOAT and req["definition"]["race_id"] == "r1")
check("the request stays under the Orin's 2 MB cap", len(json.dumps(req)) < 2 * 1024 * 1024)

print("the real engine endpoints:")
from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402

client = TestClient(engine_app.app)
r = client.post("/reroute/assets", json=dict(GOOD, course_id="c9", chart=dict(CHART, course_id="c9")))
check("POST /reroute/assets loads", r.status_code == 200 and r.json()["loaded"]
      and r.json()["course_id"] == "c9")
r = client.get("/reroute/assets")
check("GET /reroute/assets: what's held, no geometry", r.json()["course_id"] == "c9"
      and "mask_zb64" not in json.dumps(r.json()))
plume.race_session = lambda: {"kind": "race"}
r = client.post("/reroute/assets", json=GOOD)
check("racing → refused through the endpoint", r.json() == {"loaded": False, "detail": OA.RACING})
plume.race_session = lambda: None

print("short text:")
for s in (OA.RACING, "No chart or boat profile aboard."):
    check(f"{s!r} ≤ 12 words", len(s.split()) <= 12)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
