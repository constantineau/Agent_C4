"""Modularity acceptance test, the boat half (#315, map #316): a Pi set up for Bravo.

The Lab half (route, playbook, debrief, Orin re-route) is vps/lab/test_modularity_e2e.py. This one
runs the Pi engine (pi/engine/engine_app.py) for a second boat: BOAT_ID=bravo, the image's baked
polar file where the Pi image puts it, Bravo's playbook and reviewed profile pushed the way the
Lab's Deploy does (POST /playbook/load, POST /reroute/assets), then the engine paths the crew sees:
% of polar and targets (the onboard polar), the sail call (/sail), the cloud/iPad coach's prompt,
and the boat id every engine module files data under.

Assertions: no sr33 file is opened while serving Bravo (audit hook on open()), the numbers are
Bravo's, no "SR33" in what Bravo's crew is told, and nothing falls back to boat id "sr33".

#315's assertions here: 6 (targets, % of polar, sail advice are Bravo's; no bundle → "No boat
polar loaded"), 8 (no engine module starts without a boat id; the other services' halves are
test_boat_id_required.py, vps/ingestion/test_ingest_boat_id.py, pi/*/test_boat_id.py,
pi/orin/reroute/test_reroute_boat.py, shared/test_boat_id.py) and 9 (the coach, the summary
writer and the Orin copilot name Bravo, never SR33).

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


def test_sail_call_knows_the_boats_own_sails():
    """#305: the sail call knows the boat's own sails (an A2, a genoa), named as the boat names them."""
    deploy(bravo_bundle(sail_inventory=["G1", "G2", "A2", "S2"]))
    try:
        out = client.get("/sail", params={"tws": 11, "twa": 110, "hoisted": "A2"}).json()
    finally:
        deploy(bravo_bundle())
    assert out.get("crew_sail") == "A2", out


# ==== what Bravo's crew is told (the cloud/iPad coach) ===========================================
def test_coach_prompt_is_bravos():
    """#306: the coach names Bravo and loads no other boat's speed guide (Bravo has none)."""
    with watch() as seen:
        from app import agent
        agent = importlib.reload(agent)
    text = " ".join(b.get("text", "") for b in agent.SYSTEM_BLOCKS)
    assert seen == []
    assert "sr33" not in text.lower()
    assert "Bravo" in text, text[:300]          # #315 (9): the prompt names the boat it is for


def test_summary_prompt_is_bravos(monkeypatch):
    """#315 (9): the crew summary/debrief writer names Bravo and never SR33 (Anthropic stubbed)."""
    import anthropic
    from app import summarizer
    sent = []

    class _Msgs:
        def create(self, **kw):
            sent.append(kw)
            return type("R", (), {"content": []})()

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: type("C", (), {"messages": _Msgs()})())
    with watch() as seen:
        summarizer._llm_narrative({"window": "test"}, "debrief")
    assert seen == [] and sent
    prompt = sent[0]["system"] + json.dumps(sent[0]["messages"])
    assert "Bravo" in prompt and not re.search(r"sr33|\bC4\b", prompt, re.I), prompt


# ==== #315 (6): a Pi with NO bundle and nothing deployed =========================================
_NO_BUNDLE_PROBE = r"""
import json, os, sys
sys.path.append(sys.argv[1])
from fastapi.testclient import TestClient
from app import datasource, tools
import engine_app
c = TestClient(engine_app.app)
print(json.dumps({
    "rows": datasource.active().polars_stw(),
    "target": tools.get_polar_target(12, 90),
    "sail": c.get("/sail", params={"tws": 11, "twa": 60}).json(),
}))
"""


def test_no_bundle_tiles_say_no_polar():
    """A fresh Bravo Pi (BOAT_ID=bravo) with no playbook and no Deploy push: no polar rows, and
    the targets and sail tiles say plainly there is no polar. Never sr33's numbers."""
    env = dict(os.environ, BOAT_ID="bravo", DATA_SOURCE="onboard", ONBOARD_LIVE_WS="false")
    fresh = tempfile.mkdtemp()
    env["ENGINE_DB"] = os.path.join(fresh, "engine", "engine.db")
    env["ARCHIVE_DB"] = os.path.join(fresh, "archive", "archive.db")
    env.pop("POLARS_FILE", None)
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(REPO, "vps", "agent"), REPO, ENGINE_DIR])
    p = subprocess.run([sys.executable, "-c", _NO_BUNDLE_PROBE, ENGINE_DIR], env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    plain = (boat_polar.NO_POLAR_ABOARD, boat_polar.NO_POLAR)
    assert out["rows"] == [], out["rows"][:3]
    assert out["target"] == {"available": False, "note": boat_polar.NO_POLAR_ABOARD}, out["target"]
    assert out["sail"].get("available") is False and out["sail"].get("note") in plain, out["sail"]
    assert not re.search(r"sr33", json.dumps(out), re.I), out


def test_targets_are_bravos():
    """#315 (6): the engine's polar target (the targets / % of polar tile) reads Bravo's polar."""
    from app import tools
    with watch() as seen:
        got = [(tws, twa, tools.get_polar_target(tws, twa))
               for tws, twa in ((8, 52), (12, 90), (16, 135))]
    assert seen == []
    for tws, twa, t in got:
        assert t["available"] and abs(t["target_stw"] - SPOL.target(POLAR, tws, twa)) < 0.05, t


# ==== #313: Bravo's own kit (sensor priority, house bank, Orin, plotter) ==========================
def _push_profile(profile):
    """Bravo's profile as the Lab's Deploy push carries it (POST /reroute/assets)."""
    from app import hardware
    a = client.post("/reroute/assets", json={
        "schema": "c4.orin-assets/v1", "race_id": "channel-offshore-test", "course_id": "c",
        "boat_id": "bravo", "boat": profile, "boat_version": boat_profile.version(profile),
        "chart": None, "polar": boat_polar.resolve(profile)})
    assert a.status_code == 200 and a.json().get("loaded"), a.text
    hardware.clear_cache()


BRAVO_KIT = {"source_priority": {"heel": ["h5000", "zg100"], "tws": ["h5000", "derived"]},
             "battery": {"nominal_v": 24, "chemistry": "lithium"}, "orin": False, "plotter": False}


def test_kit_is_bravos_own():
    """Bravo's profile says what it carries: its sensor priority, a 24 V lithium bank, no Orin
    and no plotter. Each is honoured, and nothing of sr33's kit is left in force."""
    from app import gpsout, power
    from shared import boat_hardware, source_policy
    _push_profile(dict(BRAVO, hardware=BRAVO_KIT))
    try:
        assert source_policy.matchers_for("heel") == ["h5000", "zg100"]
        assert source_policy.matchers_for("navigation.speedOverGround") == []   # none of sr33's
        lines = power._boat_lines()
        assert lines["floor_v"] == 24.0 and lines["chemistry"] == "lithium" and not lines["assumed"]
        r = client.post("/playbook/load", json={"bundle": bravo_bundle()}).json()
        assert r["loaded"] and r["copilot"] == {"loaded": False, "absent": True,
                                                 "detail": boat_hardware.NO_ORIN}, r
        assert gpsout.show()["note"] == boat_hardware.NO_PLOTTER
    finally:
        _push_profile(BRAVO)


def test_kit_unset_is_said_never_sr33s():
    """Bravo's profile says nothing of its kit: no priority (freshest source, said), a 12 V
    lead-acid bank that the tile calls assumed, an Orin. Never sr33's devices."""
    from app import power, sensor_health
    from shared import boat_hardware, source_policy
    _push_profile(BRAVO)
    assert source_policy.active_priority() == {}
    ch = {"heel": {"preferred": {"source": "can0.7", "value": 3.0, "age_s": 1},
                   "readings": [{"source": "can0.7", "value": 3.0, "age_s": 1}]}}
    pb = sensor_health.assess_provenance(ch)["checks"]["policy_binds"]
    assert pb["no_policy"] and pb["reason"] == boat_hardware.NO_PRIORITY, pb
    lines = power._boat_lines()
    assert lines["floor_v"] == 11.0 and lines["assumed"] is True


def test_kit_is_the_newest_copy_not_the_one_with_a_polar():
    """Codex on #338: an older library copy WITH a polar and a newer Deploy push with no polar,
    no Orin and no plotter. The polar comes from the library (boat_for); the kit from the newest
    copy, so the engine never calls the absent Orin or pushes to a missing plotter."""
    from app import boatlib, gpsout, hardware
    from shared import boat_hardware
    old = dict(BRAVO, hardware={"orin": True, "plotter": {"max_route_waypoints": 250}})
    lib = {"schema": boatlib.SCHEMA, "built_at": 1, "boats": [
        {"boat_id": "bravo", "name": BRAVO["name"], "profile": old,
         "boat_version": boat_profile.version(old), "polar": boat_polar.resolve(old)}]}
    assert boatlib.store(lib, now=1)["stored"]
    new = dict(BRAVO, hardware={"orin": False, "plotter": False})
    a = client.post("/reroute/assets", json={
        "schema": "c4.orin-assets/v1", "race_id": "channel-offshore-test", "course_id": "c",
        "boat_id": "bravo", "boat": new, "boat_version": boat_profile.version(new),
        "chart": None, "polar": None})
    assert a.status_code == 200 and a.json().get("loaded"), a.text
    hardware.clear_cache()
    try:
        assert boatlib.boat_for("bravo")["from"] == "library"      # the polar: the library's
        assert not hardware.has_orin("bravo") and not hardware.has_orin()
        assert client.get("/reroute/assets").json()["orin"] is False   # what the Lab's check reads
        r = client.post("/playbook/load", json={"bundle": bravo_bundle()}).json()
        assert r["copilot"].get("absent") is True, r
        assert gpsout.show()["note"] == boat_hardware.NO_PLOTTER
    finally:
        boatlib.store({"schema": boatlib.SCHEMA, "built_at": 2, "boats": []})
        _push_profile(BRAVO)


def test_legacy_profile_takes_its_own_boats_seed(tmp_path):
    """Codex on #338: a Pi whose stored profile predates the hardware block keeps that boat's
    sensor priority from the seed baked into the engine image (boat-id scoped), never another
    boat's, and with no seed for the boat there is none."""
    from app import hardware
    from shared import boat_hardware, source_policy
    seed = tmp_path / "seed"
    boats = tmp_path / "boats"
    boats.mkdir()
    (boats / "bravo.json").write_text(json.dumps(dict(BRAVO, hardware=BRAVO_KIT)))
    (boats / "other.json").write_text(json.dumps({"boat_id": "other", "name": "Other",
                                                  "draft_m": 2.0, "hardware": {"orin": False}}))
    (boats / "liar.json").write_text(json.dumps({"boat_id": "../x", "hardware": {}}))
    assert boat_hardware.seed_files(str(boats), str(seed)) == ["bravo", "other"]
    assert json.loads((seed / "bravo.json").read_text()) == {"boat_id": "bravo",
                                                              "hardware": BRAVO_KIT}
    _push_profile(BRAVO)                         # stored aboard with no hardware block
    saved = list(hardware.SEED_DIRS)
    hardware.SEED_DIRS[:] = [str(seed)]
    hardware.clear_cache()
    try:
        assert source_policy.matchers_for("heel") == ["h5000", "zg100"]
        # a seed file that names another boat is never taken
        (seed / "bravo.json").write_text(json.dumps({"boat_id": "other", "hardware": BRAVO_KIT}))
        hardware.clear_cache()
        assert source_policy.active_priority() == {}
        (seed / "bravo.json").unlink()
        hardware.clear_cache()
        assert source_policy.active_priority() == {} and hardware.has_orin()
        # an explicit empty block on the older copy stands: it is not "no block" (Codex on #338)
        (seed / "bravo.json").write_text(json.dumps({"boat_id": "bravo", "hardware": BRAVO_KIT}))
        from app import boatlib
        lib = {"schema": boatlib.SCHEMA, "built_at": 1, "boats": [
            {"boat_id": "bravo", "name": BRAVO["name"], "profile": dict(BRAVO, hardware={}),
             "boat_version": boat_profile.version(dict(BRAVO, hardware={})),
             "polar": boat_polar.resolve(BRAVO)}]}
        assert boatlib.store(lib, now=1)["stored"]          # older than the push
        hardware.clear_cache()
        try:
            assert source_policy.active_priority() == {} and hardware.has_orin()
            assert hardware.profile()["hardware"] == {}
        finally:
            boatlib.store({"schema": boatlib.SCHEMA, "built_at": 2, "boats": []})
    finally:
        hardware.SEED_DIRS[:] = saved
        hardware.clear_cache()


def test_engine_image_seed_is_hardware_only():
    """The Pi engine image's seed, built as its Dockerfile builds it: the bundled boats' hardware
    blocks only (no polar, no other profile field), sr33's equal to its profile's."""
    with open(os.path.join(ENGINE_DIR, "Dockerfile")) as f:
        assert "shared.boat_hardware /tmp/boats ./knowledge/boat_hardware" in f.read()
    from shared import boat_hardware
    out = tempfile.mkdtemp()
    bdir = os.path.join(REPO, "vps", "lab", "boats")
    ids = boat_hardware.seed_files(bdir, out)
    assert "sr33" in ids
    for bid in ids:
        with open(os.path.join(out, f"{bid}.json")) as f:
            s = json.load(f)
        with open(os.path.join(bdir, f"{bid}.json")) as f:
            assert set(s) == {"boat_id", "hardware"} and s["hardware"] == json.load(f)["hardware"]


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



# ==== #335: practice on Bravo with another boat's playbook still loaded ===========================
SR33_PLAY = {"id": "c0-ceiling", "name": "Code 0 ceiling", "kind": "sail",
             "conditions": {"predicates": [{"signal": "tws_kn", "op": ">=", "value": 0}]},
             "response": {"type": "sail", "guidance": "Drop the C0 above 9 kts."}}


def sr33_bundle():
    """sr33's playbook as the Lab freezes it (the boat model from sr33's seed profile)."""
    with open(os.path.join(REPO, "vps", "lab", "boats", "sr33.json")) as f:
        sr33 = json.load(f)
    bm = {k: sr33.get(k) for k in ("boat_id", "name", "draft_m", "sail_inventory", "jib_crossovers",
                                    "code0", "main_reefs")}
    return dict(bravo_bundle(), race_id="sr33-race", boat_model=bm, plays=[SR33_PLAY])


def _orin_on(client_get, path):
    """The Orin copilot, its playbook file at `path`, asking this engine over the 'cable'."""
    sys.path.append(os.path.join(REPO, "pi", "orin"))
    from copilot import boatname, briefs, config as ccfg, engine_client, playbook as pbm
    ccfg.PLAYBOOK_PATH = path

    class Cable:
        def __init__(self, *a, **k):
            pass

        def _get(self, p, params=None):
            assert p == "/playbook/copilot"
            return client_get(p).json()

    engine_client.EngineClient = Cable
    pbm._pin.update(at=None, hash=None, boat=None, off=False)
    return boatname, briefs, pbm


def _orin_says(boatname, briefs, pbm):
    """Everything the copilot builds from the boat: its name, the playbook it uses, the sail clock
    (Code 0 ceiling, slot reef) with the breeze building through every sr33 threshold."""
    from copilot import copilot as cop
    pb = pbm.load()
    snap = {"plays": {"sail_state": {"flying": ["C0", "A3"]}},
            "trend": {"tws_trend_kn_per_hr": 4.0}, "conditions": {"tws": 7.0}}
    sails = briefs._sail_window_lines(snap, pb) or {}
    return pb, " ".join([boatname.describe(), boatname.possessive(), cop._system_prompt(pb),
                         json.dumps(sails)])


def test_practice_on_bravo_with_sr33s_playbook_no_sr33_plays_or_copilot(tmp_path, monkeypatch):
    """#335: a LOG session sailing Bravo with sr33's playbook loaded: no sr33 plays, the console
    note "Playbook is for C4. Plays off.", and the Orin copilot neither names sr33 nor uses its
    Code 0 ceiling (9 kts) or slot reef (16 kts): it speaks for Bravo. Sailing the playbook's own
    boat is unchanged. An "Other boat" doesn't get the playbook's plays either."""
    from app import boatlib, matcher
    monkeypatch.setattr(matcher, "gather", lambda route=None: {"tws_kn": 12.0})  # no archive here
    lib = {"schema": boatlib.SCHEMA, "built_at": 2, "boats": [
        {"boat_id": "bravo", "name": BRAVO["name"], "profile": BRAVO,
         "boat_version": boat_profile.version(BRAVO), "polar": boat_polar.resolve(BRAVO)}]}
    assert boatlib.store(lib)["stored"]
    sr = sr33_bundle()
    r = client.post("/playbook/load", json={"bundle": sr}).json()
    assert r["loaded"] and r["plays"] == 1, r
    engine_app.approve_copilot_playbook(engine_app.bundle_hash(sr))   # as if the Orin confirmed
    path = tmp_path / "playbook.json"
    path.write_text(json.dumps(sr))
    s = client.post("/session/start", json={"kind": "practice", "boat_id": "bravo"}).json()
    try:
        assert s["ok"] and s["active"]["boat_id"] == "bravo", s
        p = client.get("/plays").json()
        assert p.get("available") is False and p.get("plays_off") is True, p
        assert p["note"] == "Playbook is for C4. Plays off." and "plays" not in p, p
        assert "sail_state" in p, p           # the copilot brief reads what is flying from here
        cp = client.get("/playbook/copilot").json()
        assert cp["hash"] is None and cp["playbook_off"] is True, cp
        assert cp["boat"]["boat_id"] == "bravo" and cp["boat"]["profile"]["name"] == "Bravo", cp
        assert not re.search(r"sr33|C4", json.dumps(cp["boat"]), re.I), cp
        boatname, briefs, pbm = _orin_on(client.get, str(path))
        pb, said = _orin_says(boatname, briefs, pbm)
        assert not pb.loaded and pb.note == pbm.OTHER_BOAT, pb.note
        assert "Bravo" in said, said
        assert not re.search(r"sr33|\bC4\b", said, re.I), said
        assert not re.search(r"\b(9|16) kts\b", said), said            # sr33's ceiling / slot reef
    finally:
        client.post("/session/end", json={"id": s["active"]["id"]})

    # an "Other boat": no id, no profile, so not the playbook's boat either
    s = client.post("/session/start", json={"kind": "practice", "boat_name": "Mystery"}).json()
    try:
        p = client.get("/plays").json()
        assert p.get("plays_off") is True and p["note"] == "Playbook is for C4. Plays off.", p
        cp = client.get("/playbook/copilot").json()
        assert cp["hash"] is None and cp["boat"] == {"boat_id": None, "name": "Mystery",
                                                     "profile": None}, cp
        pbm._pin.update(at=None, hash=None, boat=None, off=False)
        _pb, said = _orin_says(boatname, briefs, pbm)
        assert "Mystery" in said and not re.search(r"sr33|\bC4\b|\b(9|16) kts\b", said, re.I), said
    finally:
        client.post("/session/end", json={"id": s["active"]["id"]})

    # sailing the playbook's own boat: plays run and the copilot keeps the playbook, as before
    bb = dict(bravo_bundle(), plays=[dict(SR33_PLAY, name="Bravo play")])
    deploy(bb)
    engine_app.approve_copilot_playbook(engine_app.bundle_hash(bb))
    path.write_text(json.dumps(bb))
    s = client.post("/session/start", json={"kind": "practice", "boat_id": "bravo"}).json()
    try:
        p = client.get("/plays").json()
        assert p.get("available") is True and [x["id"] for x in p["plays"]] == ["c0-ceiling"], p
        cp = client.get("/playbook/copilot").json()
        assert cp["hash"] == engine_app.bundle_hash(bb) and "playbook_off" not in cp, cp
        pbm._pin.update(at=None, hash=None, boat=None, off=False)
        pb = pbm.load()
        assert pb.loaded and pb.note is None and boatname.boat_model(pb) == bb["boat_model"]
    finally:
        client.post("/session/end", json={"id": s["active"]["id"]})
        engine_app.approve_copilot_playbook(None)
        deploy(bravo_bundle())
