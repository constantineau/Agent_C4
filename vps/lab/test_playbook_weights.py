"""#51: the playbook routes on the same venue-weighted wind as the Lab route.

The Lab's Optimize weights each model by its measured skill at the venue and removes its known
bias (routerun). The playbook built its field on the plain blend, so the frozen bundle could
disagree with the route the crew just looked at. Pinned: build_playbook hands the venue weights
and bias to the wind field, each model's scenario keeps its bias, and model_skill=False opts out.

No network: the venue skill and the field builder are stubbed.
"""
import os
import tempfile

os.environ["INGESTED_DIR"] = tempfile.mkdtemp()
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import modelskill, playbook, venue  # noqa: E402
from app.wind.windfield import WindField  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"race_id": "w", "name": "W", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 44.0, "lon": -82.5}, "marks": [],
    "finish": {"points": [{"lat": 44.5, "lon": -82.5}]}}]}
W = {"gfs": 0.6, "hrrr": 1.4}
B = {"gfs": (1.5, 8.0), "hrrr": (0.0, 0.0)}
venue.resolve = lambda definition, course_id, bbox=None: {"key": "huron", "label": "Lake Huron"}
modelskill.venue_weights = lambda v, models=None, race_date=None, refresh=True: {
    "enabled": True, "venue_key": v["key"], "station": "45008", "model_weights": W, "model_bias": B}

seen = {}


class Empty:
    loaded = False

    def status(self):
        return {}


def fake_build(bbox, t0, t1, **kw):
    seen.clear()
    seen.update(kw)
    return Empty()                              # stop right after the field is built


print("build_playbook → the wind field")
playbook.build_windfield = fake_build
playbook.build_playbook(DEF, "c", 0, ["gfs", "hrrr"], v2_scenarios=False)
check("the venue weights reach the field", seen.get("model_weights") == W)
check("…and the bias", seen.get("model_bias") == B)
playbook.build_playbook(DEF, "c", 0, ["gfs", "hrrr"], v2_scenarios=False, model_skill=False)
check("model_skill=False → the plain blend", not seen.get("model_weights") and not seen.get("model_bias"))

print("each model's scenario keeps its bias")
wf = WindField({("gfs", 0): [], ("hrrr", 0): []}, [{"model": "gfs"}, {"model": "hrrr"}],
               (45, 43, -83, -82), 0, 3600, model_weights=W, model_bias=B)
subs = playbook._subfields(wf)
check("gfs scenario de-biased like the blend", subs["gfs"].model_bias.get("gfs") == B["gfs"])

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
