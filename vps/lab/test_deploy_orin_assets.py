"""Deploy carries the Orin re-route's chart + boat profile (vps/lab/app/orinassets.py, #139, #140).

Pins:
  * the chart is land only, land-v2, no pockets, with the fingerprint the Orin computes for the
    deployed course and the CARRIED boat's draft (the boat the Deploy is for, not the Lab's
    active one);
  * the profile is the reviewed one from the Lab's volume, with its version;
  * a chart that didn't build (no land, an error) is sent as null and the step is never green;
  * the Pi's own check takes what the Lab builds;
  * the push: the course step fails on `loaded: false`; the assets step goes right after the
    course and fails (with the Pi's reason) when the Pi refuses, e.g. while racing.

No network: the posts are faked. Run in-container (make ci): python3 /repo/vps/lab/test_deploy_orin_assets.py
"""
import importlib.util
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"
os.environ["DEPLOY_COPILOT_URL"] = "http://pi:8091/copilot"

from shared import boat_profile  # noqa: E402

from app import boats, deploy as D, labstate, orinassets as OA  # noqa: E402
from app.geo import obstacles as geo  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
boats.EDITED_DIR = os.path.join(_tmp, "boats")

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"race_id": "r1", "name": "Race", "zones": [],
       "courses": [{"id": "c", "start": {"lat": 43.0, "lon": -82.4},
                    "marks": [{"name": "Gate", "type": "buoy", "lat": 44.5, "lon": -82.2,
                               "rounding": "port"}],
                    "finish": {"points": [{"lat": 45.0, "lon": -82.3}]}}]}

# the reviewed SR33 on the Lab's volume (helm + approved polar cells), and another boat active
repo = boats.get_boat("sr33")
reviewed = dict(repo, helm_factor=0.931, polar_adjustments=[{"tws": 10, "twa": 90, "mult": 1.03}] * 24)
boats.save_boat(reviewed)
deep = dict(repo, boat_id="deep", name="Deep", draft_m=3.5)
boats.save_boat(deep)
boats.set_active("deep")

BUILD_KW = {}
LAND = {"on": True, "raise": False}


def fake_build(definition, course_id, bbox, **k):
    if LAND["raise"]:
        raise RuntimeError("gdal")
    BUILD_KW.clear()
    BUILD_KW.update(k)
    f = geo.ObstacleField(bbox, res_deg=0.05)
    if LAND["on"]:
        n, s, w, e = bbox
        f._fill_polygon([[(w, s), (w + 0.2, s), (w + 0.2, s + 0.2), (w, s + 0.2)]], 1, "coastline")
    f.source = k.get("source")
    return f


geo.build_for_course = fake_build

print("the payload:")
p, problems = OA.build(DEF, "c", now=100)
ch = p["chart"]
check("complete: no problems", problems == [])
check("land only, no pockets", BUILD_KW.get("zones_on") is False and BUILD_KW.get("islands_on") is False
      and BUILD_KW.get("carve_waypoints") is False)
check("land-v2, for this race and course", ch["kind"] == "land-v2" and ch["race_id"] == "r1"
      and ch["course_id"] == "c" and ch["schema"] == geo.FIELD_SCHEMA and ch["open_water"] is False)
depth = boats.safety_depth_m(reviewed)
check("fingerprint = the Orin's, with the carried boat's draft (not the active boat's)",
      ch["fingerprint"] == geo.course_fingerprint(DEF, "c", depth)
      and BUILD_KW["safety_depth"] == depth and depth != boats.safety_depth_m(deep))
check("the reviewed profile, with its version", p["boat"]["helm_factor"] == 0.931
      and len(p["boat"]["polar_adjustments"]) == 24 and p["boat_version"] == boat_profile.version(reviewed)
      and p["boat_version"] != boat_profile.version(repo))
check("covers the course", geo.field_from_dict(ch).bbox[0] >= 45.0 and geo.field_from_dict(ch).bbox[1] <= 43.0)

LAND["on"] = False
p2, problems2 = OA.build(DEF, "c")
check("no land (the shoreline didn't load) → chart null, said", p2["chart"] is None
      and problems2 == [OA.NO_LAND] and p2["boat"]["boat_id"] == "sr33")
LAND["on"], LAND["raise"] = True, True
p3, problems3 = OA.build(DEF, "c")
check("build error → chart null, said", p3["chart"] is None and problems3 == [OA.BUILD_FAILED])
LAND["raise"] = False
os.environ["DEPLOY_BOAT_ID"] = "nope"
p4, problems4 = OA.build(DEF, "c")
os.environ.pop("DEPLOY_BOAT_ID")
check("no profile for the boat → nothing half-built", p4["boat"] is None and p4["chart"] is None
      and problems4 == [OA.NO_BOAT.format(boat="nope")])

print("the boat the playbook was built with:")
pb_deep, pr_deep = OA.build(DEF, "c", bundle={"boat_model": {"boat_id": "deep"}})
check("the bundle's boat, its profile and its draft", pr_deep == [] and pb_deep["boat"]["boat_id"] == "deep"
      and pb_deep["boat_id"] == "deep"
      and pb_deep["chart"]["fingerprint"] == geo.course_fingerprint(DEF, "c", boats.safety_depth_m(deep)))
pb_x, pr_x = OA.build(DEF, "c", bundle={"boat_model": {"boat_id": "ghost"}})
check("the bundle's boat has no profile → nothing sent for another boat, said",
      pb_x["boat"] is None and pb_x["chart"] is None and pb_x["boat_id"] == "ghost"
      and pr_x == ["No boat profile for ghost. Chart and profile not sent."])
check("a bundle that doesn't say → DEPLOY_BOAT_ID", OA.boat_id({"boat_model": {}}) == "sr33"
      and OA.boat_id(None) == "sr33" and OA.boat_id({"boat_model": {"boat_id": " "}}) == "sr33")

print("a carried field's grid is checked before anything is allocated:")
EVIL = dict(geo.field_to_dict(geo.ObstacleField((46.0, 42.0, -84.0, -80.0), res_deg=1.0)),
            res_deg=0.000001, nx=1, ny=1)
EVIL["mask_zb64"] = __import__("base64").b64encode(__import__("zlib").compress(b"\x01")).decode()
for name, bad in (("tiny cells with nx = ny = 1 (16 TB)", EVIL),
                  ("nx/ny that don't match the box", dict(ch, nx=ch["nx"] + 1)),
                  ("a box past the cap", dict(ch, res_deg=0.0001, nx=60000, ny=60000)),
                  ("reversed box", dict(ch, bbox=[ch["bbox"][1], ch["bbox"][0]] + ch["bbox"][2:])),
                  ("NaN box", dict(ch, bbox=[float("nan")] + ch["bbox"][1:])),
                  ("zero cell size", dict(ch, res_deg=0)),
                  ("no nx", {k: v for k, v in ch.items() if k != "nx"})):
    try:
        geo.field_from_dict(bad)
        check(f"field_from_dict: {name} → ValueError", False)
    except ValueError:
        check(f"field_from_dict: {name} → ValueError", True)
for name, bad in (("nx = 1e309", dict(ch, nx=1e309)),
                  ("a 400-digit number in the box", dict(ch, bbox=[10 ** 400] + ch["bbox"][1:])),
                  ("a layer count of 1e309", dict(ch, layers={"coastline": 1e309})),
                  ("a cell size that divides to infinity", dict(ch, res_deg=1e-320)),
                  ("mask bytes other than 0 and 1", dict(ch, mask_zb64=__import__("base64").b64encode(
                      __import__("zlib").compress(bytes([2]) * (ch["nx"] * ch["ny"]))).decode()))):
    try:
        geo.field_from_dict(bad)
        check(f"field_from_dict: {name} → ValueError", False)
    except ValueError:
        check(f"field_from_dict: {name} → ValueError", True)
    except Exception as exc:  # noqa: BLE001
        check(f"field_from_dict: {name} → ValueError, not {type(exc).__name__}", False)
inact = geo.field_from_dict(dict(ch, active=False, safety_depth="deep", source=None))
land_lat, land_lon = ch["bbox"][1] + 0.1, ch["bbox"][2] + 0.1      # inside fake_build's land block
check("active is derived from the mask, not taken from the payload", inact.active is True
      and inact.blocked(land_lat, land_lon))
check("a safety depth too large for a float → None, no crash",
      geo.field_from_dict(dict(ch, safety_depth=10 ** 400)).safety_depth is None)
check("the repo's and the reviewed SR33 profiles have the right shape",
      boat_profile.shape_errors(repo) == [] and boat_profile.shape_errors(reviewed) == [])
check("provenance fields are cleaned, not trusted", inact.safety_depth is None
      and isinstance(inact.source, str))
empty_d = geo.field_to_dict(geo.ObstacleField(tuple(ch["bbox"]), res_deg=ch["res_deg"]))
check("an empty mask is inactive whatever it says", geo.field_from_dict(dict(empty_d, active=True)).active is False)
check("a good one still round-trips", geo.field_from_dict(ch).nx == ch["nx"])

print("the Pi's own check takes it:")
pkg_dir = os.path.join("/repo", "vps", "agent", "app")
spec = importlib.util.spec_from_file_location("piapp", os.path.join(pkg_dir, "__init__.py"),
                                              submodule_search_locations=[pkg_dir])
piapp = importlib.util.module_from_spec(spec)
sys.modules["piapp"] = piapp
spec.loader.exec_module(piapp)
pi_oa = importlib.import_module("piapp.orinassets")
check("valid on the Pi", pi_oa.validate(p) == (True, ""))
check("…and a no-chart push is valid too (it clears the old one)", pi_oa.validate(p2) == (True, ""))
check("the Pi refuses the 16 TB grid too", pi_oa.validate(dict(p, chart=dict(EVIL, race_id="r1",
      course_id="c", kind="land-v2", fingerprint=ch["fingerprint"])))[0] is False)
check("same cell cap on both sides", pi_oa.MAX_CELLS == geo.MAX_FIELD_CELLS)
check("same chart format on both sides", pi_oa.CHART_KIND == OA.CHART_KIND == "land-v2")

print("the push:")
posted = []
PI = {"course": {"loaded": True, "marks": 3}, "assets": {"loaded": True, "chart": True}}


def fake_post(url, body, timeout=25):
    posted.append((url.rsplit(":8200", 1)[-1], body))
    if url.endswith("/course/load"):
        return True, PI["course"]
    if url.endswith("/reroute/assets"):
        return True, PI["assets"]
    return True, {"loaded": True}


D._post_json = fake_post
D.package = lambda rid, course_id=None: {"race_name": "Race", "course_id": "c",
                                         "course_load": {"definition": DEF, "course_id": "c"},
                                         "fleet_load": {}, "checklist_load": {"items": []},
                                         "watch_load": None}
res = D.push("r1")
urls = [u for u, _b in posted]
check("assets right after the course", urls[:3] == ["/course/load", "/reroute/assets", "/fleet/load"])
sent = posted[1][1]
check("the chart + profile went", sent["chart"]["fingerprint"] == ch["fingerprint"]
      and sent["boat_version"] == p["boat_version"])
check("all green", res["ok"] and [s["step"] for s in res["steps"]] == ["course", "orin assets", "fleet"])

posted.clear()
PI["assets"] = {"loaded": False, "detail": "Racing now. Not loaded."}
res = D.push("r1")
st = res["steps"][1]
check("racing on the boat → the step fails, with the Pi's reason", not res["ok"] and not st["ok"]
      and st["detail"]["error"] == "Racing now. Not loaded.")
rec = D.last_push()["last"]
check("…recorded for the Deploy tab", rec["steps"][1]["ok"] is False
      and rec["steps"][1]["detail"]["error"] == "Racing now. Not loaded.")

PI["assets"] = {"loaded": True, "chart": False}
LAND["on"] = False
res = D.push("r1")
LAND["on"] = True
st = res["steps"][1]
check("no chart built → sent (old one dropped), but the step is not green",
      not st["ok"] and st["detail"]["error"] == OA.NO_LAND and posted[-2][1]["chart"] is None)

PI["assets"] = {"loaded": True, "chart": False}
D.pbstore.get = lambda pid: {"race_id": "r1", "variants": [{"id": "a"}],
                             "boat_model": {"boat_id": "ghost"}} if pid == "pb1" else None
labstate.set("deploy:r1", {"playbook_id": "pb1"})
posted.clear()
res = D.push("r1")
st = res["steps"][1]
sent = [b for u, b in posted if u == "/reroute/assets"][0]
check("playbook built for a boat with no profile → red, short, no other boat's profile sent",
      not st["ok"] and st["detail"]["error"] == "No boat profile for ghost. Chart and profile not sent."
      and sent["boat"] is None and sent["chart"] is None)
labstate.set("deploy:r1", {})

PI["assets"] = {"loaded": True}
PI["course"] = {"loaded": False, "detail": "Couldn't save the course. Try again."}
res = D.push("r1")
check("course not saved on the Pi → the course step fails (it used to pass)", not res["steps"][0]["ok"]
      and not res["ok"])

for s in (OA.NO_LAND, OA.NO_MARKS, OA.NO_BOAT.format(boat="sr33"), OA.BUILD_FAILED):
    check(f"short: {s!r}", len(s.split()) <= 12)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
