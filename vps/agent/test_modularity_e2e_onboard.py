"""Modularity acceptance test, the boat half (#315, map #316): a Pi set up for Bravo.

The Lab half (route, playbook, debrief, Orin re-route) is vps/lab/test_modularity_e2e.py. This one
runs the Pi engine (pi/engine/engine_app.py) for a second boat: BOAT_ID=bravo, the image's baked
polar file where the Pi image puts it, Bravo's playbook and reviewed profile pushed the way the
Lab's Deploy does (POST /playbook/load, POST /reroute/assets), then the engine paths the crew sees:
% of polar and targets (the onboard polar), the sail call (/sail), the cloud/iPad coach's prompt,
and the boat id every engine module files data under.

Assertions: no sr33 file is opened while serving Bravo (audit hook on open()), the numbers are
Bravo's, no "SR33" in what Bravo's crew is told, and nothing falls back to boat id "sr33".

Parts that fail on today's dev are strict expected failures naming the ticket that fixes each;
the fix turns its xfail into an unexpected pass, which fails this file until the marker goes. The
fixer may adapt the deploy step to the real interface, keeping the assertions.

Run:  PYTHONPATH=vps/agent:. python3 -m pytest -q vps/agent/test_modularity_e2e_onboard.py
"""
import contextlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
ENGINE_DIR = os.path.join(REPO, "pi", "engine")
sys.path.append(ENGINE_DIR)

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")
os.environ["ARCHIVE_DB"] = os.path.join(_tmp, "archive", "archive.db")
os.environ["ONBOARD_LIVE_WS"] = "false"
os.environ["DATA_SOURCE"] = "onboard"
os.environ["COPILOT_URL"] = "http://127.0.0.1:9"        # no Orin: refused at once
os.environ["BOAT_ID"] = "bravo"                         # this Pi is Bravo's
# the Pi engine image carries no polar since #304: it comes with the Deploy push (below)
os.environ.pop("POLARS_FILE", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402

_watching = []


def _audit(event, args):
    if event == "open" and _watching and args:
        path = args[0]
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        if isinstance(path, str) and re.search(r"sr33", os.path.basename(path), re.I):
            _watching[-1].append(path)


sys.addaudithook(_audit)


@contextlib.contextmanager
def watch():
    """Record every sr33 file opened inside the block."""
    seen = []
    _watching.append(seen)
    try:
        yield seen
    finally:
        _watching.pop()


@pytest.fixture(autouse=True)
def _known_fail_line(request):
    """Name each expected failure in run.sh's summary ([KNOWN-FAIL] lines)."""
    yield
    m = request.node.get_closest_marker("xfail")
    if m:
        cap = request.config.pluginmanager.getplugin("capturemanager")
        with (cap.global_and_fixture_disabled() if cap else contextlib.nullcontext()):
            print(f"\n[KNOWN-FAIL] {request.node.name}: {m.kwargs.get('reason')}")


def xfail(ticket, why):
    # only a failed assertion is the known gap: any other error (a crash) fails CI
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{why} ({ticket})")


with open(os.path.join(REPO, "vps", "lab", "testdata", "boats", "bravo.json")) as _f:
    BRAVO = json.load(_f)
POLAR = [tuple(map(float, p)) for p in BRAVO["cert_polar"]]

from fastapi.testclient import TestClient  # noqa: E402

from shared import boat_polar, boat_profile, polar as SPOL  # noqa: E402
from app import datasource  # noqa: E402
import engine_app  # noqa: E402

client = TestClient(engine_app.app)
# the Orin copilot's copy of the playbook is not what this test is about (and waits out its TTL)
engine_app.copilot_load = lambda bundle: {"sent": False, "note": "not in this test"}


def bravo_bundle(sail_inventory=None):
    """A frozen playbook as the Lab ships it for Bravo (just what the engine reads here)."""
    bm = {"boat_id": "bravo", "name": "Bravo", "draft_m": BRAVO["draft_m"],
          "sail_inventory": sail_inventory or BRAVO.get("sail_inventory") or [],
          "jib_crossovers": [], "code0": {}, "main_reefs": {}, "polar": BRAVO["cert_polar"]}
    return {"schema": "c4.playbook/v1", "race_id": "channel-offshore-test",
            "race_name": "Channel Offshore Test", "course_id": "c", "start_epoch": 1_811_224_800,
            "variants": [{"id": "middle", "name": "Middle", "route": {"path": [
                {"lat": 50.30, "lon": -1.30, "t": 1_811_224_800},
                {"lat": 50.10, "lon": -1.00, "t": 1_811_235_600}]}}],
            "boat_model": bm, "routing": {}, "plays": []}


def deploy(bundle):
    r = client.post("/playbook/load", json={"bundle": bundle})
    assert r.status_code == 200 and r.json().get("loaded"), r.text
    a = client.post("/reroute/assets", json={
        "schema": "c4.orin-assets/v1", "race_id": bundle["race_id"],
        "course_id": bundle["course_id"], "boat_id": "bravo", "boat": BRAVO,
        "boat_version": boat_profile.version(BRAVO), "chart": None,
        # the boat's own polar block, as the Lab's Deploy push carries it (#304)
        "polar": boat_polar.resolve(BRAVO)})
    assert a.status_code == 200 and a.json().get("loaded"), a.text


deploy(bravo_bundle())


# ==== % of polar and targets: the engine's onboard polar ==========================================
@pytest.fixture(scope="module")
def onboard_polar():
    """A fresh engine data source after Bravo's deploy (as on a Pi restart)."""
    datasource._SOURCE = None
    with watch() as seen:
        rows = datasource.active().polars_stw()
    return rows, seen


def test_onboard_polar_opens_no_sr33_file(onboard_polar):
    assert onboard_polar[1] == []


def test_onboard_polar_is_bravos(onboard_polar):
    rows = onboard_polar[0]
    assert rows, "no polar aboard"
    for tws, twa in ((6, 52), (12, 90), (20, 135)):
        assert abs(SPOL.lookup(rows, tws, twa) - SPOL.lookup(POLAR, tws, twa)) < 0.05


# ==== the sail call (/sail) =====================================================================
@pytest.fixture(scope="module")
def sail_call():
    from app import sails
    with watch() as seen:
        importlib.reload(sails)          # the module reads its speed guide when it loads
        r = client.get("/sail", params={"tws": 11, "twa": 60})
    return r.json(), seen


def test_sail_call_answers(sail_call):
    out = sail_call[0]
    assert out.get("available") or out.get("note")


def test_sail_call_opens_no_sr33_file(sail_call):
    assert sail_call[1] == []


def test_sail_call_targets_are_bravos_or_none(sail_call):
    """Either Bravo's own target speed, or a plain 'no sail model' — never another boat's."""
    out = sail_call[0]
    if not out.get("available"):
        assert re.search(r"sail|polar|boat", out.get("note") or "", re.I), out
        return
    btv = (out.get("targets") or {}).get("btv")
    lo, hi = sorted((SPOL.lookup(POLAR, 10, 60), SPOL.lookup(POLAR, 12, 60)))   # either TWS row
    assert isinstance(btv, (int, float)) and lo - 0.05 <= btv <= hi + 0.05, out.get("targets")


@xfail("#305", "sail names are fixed to sr33's (J1/A3/S1…): an A2 is not recognised")
def test_sail_call_knows_the_boats_own_sails():
    deploy(bravo_bundle(sail_inventory=["G1", "G2", "A2", "S2"]))
    try:
        out = client.get("/sail", params={"tws": 11, "twa": 110, "hoisted": "A2"}).json()
    finally:
        deploy(bravo_bundle())
    assert out.get("crew_sail") == "A2", out


# ==== what Bravo's crew is told (the cloud/iPad coach) ===========================================
@xfail("#306", "the coach's prompt says 'SR33 AI Navigator' and loads sr33's speed guide")
def test_coach_prompt_is_bravos():
    with watch() as seen:
        from app import agent
        agent = importlib.reload(agent)
    text = " ".join(b.get("text", "") for b in agent.SYSTEM_BLOCKS)
    assert seen == []
    assert "sr33" not in text.lower()


# ==== boat id: never "sr33" by default ===========================================================
ENGINE_MODULES = ["app.datasource", "app.datasource_onboard", "app.onboard_conditions", "app.ais",
                  "app.fatigue", "app.navigator", "app.sensor_health", "engine_app"]
_PROBE = r"""
import importlib, json, sys
out = {}
for m in sys.argv[1:]:
    try:
        mod = importlib.import_module(m)
        out[m] = getattr(mod, "BOAT_ID", None)
    except (Exception, SystemExit) as e:
        if "BOAT_ID" not in str(e):      # only a refusal that names the missing setting counts
            raise
        out[m] = "refused: " + type(e).__name__
print(json.dumps(out))
"""


def _module_boat_ids(boat_id):
    """Each engine module's BOAT_ID, imported fresh with BOAT_ID set to `boat_id` (None = unset)."""
    env = {k: v for k, v in os.environ.items() if k != "BOAT_ID"}
    if boat_id is not None:
        env["BOAT_ID"] = boat_id
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(REPO, "vps", "agent"), REPO, ENGINE_DIR])
    p = subprocess.run([sys.executable, "-c", _PROBE, *ENGINE_MODULES], env=env,
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:          # a crash, not a boat-id answer: never the known gap
        raise RuntimeError(p.stderr[-800:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_every_engine_module_takes_the_configured_boat():
    """BOAT_ID=bravo: every module that keeps a boat id keeps Bravo's (none hard-codes sr33)."""
    ids = _module_boat_ids("bravo")
    assert all(v in ("bravo", None) for v in ids.values()), ids


def test_no_engine_module_defaults_to_sr33():
    """BOAT_ID unset: each module refuses to start or has no boat id — never files as sr33."""
    ids = _module_boat_ids(None)
    assert all(v is None or str(v).startswith("refused") for v in ids.values()), ids


# ==== #320: practice on any boat, no Lab prep (the boat library + the LOG picker) =================
def test_practice_log_on_any_boat_no_lab_prep():
    """A Practice LOG picks the boat: Bravo from the library aboard has its polar; an "Other boat"
    records fine and says "No polar for this boat."; GET /boats and /session name the boat."""
    from app import boatlib
    lib = {"schema": boatlib.SCHEMA, "built_at": 1, "boats": [
        {"boat_id": "bravo", "name": BRAVO["name"], "profile": BRAVO,
         "boat_version": boat_profile.version(BRAVO), "polar": boat_polar.resolve(BRAVO)}]}
    assert boatlib.store(lib)["stored"]
    r = client.post("/session/start", json={"kind": "practice", "boat_id": "bravo"}).json()
    try:
        assert r["ok"] and r["active"]["boat_id"] == "bravo", r
        b = client.get("/boats").json()
        assert b["current"]["boat_id"] == "bravo" and b["current"]["has_polar"], b
        assert [x["boat_id"] for x in b["boats"]] == ["bravo"]
        s = client.get("/session").json()
        assert s["active"]["boat_name"] == BRAVO["name"] and s["boats"]["current"]["boat_id"] == "bravo"
        with watch() as seen:
            rows = datasource.active().polars_stw()
        assert seen == [] and abs(SPOL.lookup(rows, 12, 90) - SPOL.lookup(POLAR, 12, 90)) < 0.05
    finally:
        client.post("/session/end", json={"id": r["active"]["id"]})
    r = client.post("/session/start", json={"kind": "practice", "boat_name": "Mystery"}).json()
    try:
        assert r["ok"] and r["active"]["boat_id"] is None and r["active"]["boat_name"] == "Mystery"
        out = client.get("/sail", params={"tws": 11, "twa": 60}).json()
        assert out.get("available") is False and out.get("note") == boat_polar.NO_POLAR, out
        assert client.get("/boats").json()["current"]["note"] == boat_polar.NO_POLAR
    finally:
        client.post("/session/end", json={"id": r["active"]["id"]})

