"""The Lab has no boat address in code (#314).

Deploy, the live boat read and the plume push used to fall back to one boat's Pi on its Tailscale
address. Now the address is DEPLOY_PI_ENGINE_URL in the Lab's .env. Unset, each one says which
setting is missing and sends nothing: no network call is made. The copilot read defaults to the
engine's host on the Pi console's port (the boat's layout); DEPLOY_COPILOT_URL overrides it.

Run in-container (make ci does this): python3 /repo/vps/lab/test_deploy_no_address.py.
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
for k in ("DEPLOY_PI_ENGINE_URL", "DEPLOY_COPILOT_URL", "DEPLOY_PI_HOST", "PLUME_PUSH_URL"):
    os.environ.pop(k, None)

from app import deploy as D, labstate, plume as P  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def no_network(*a, **k):
    raise AssertionError("no call may be made without an address")


D._post_json = no_network
D._get_json = no_network

print("unset:")
check("no engine URL", D.engine_url() is None and D.copilot_url() is None)
st = D.boat_status(get=no_network)
check("boat read: unreachable, names the setting, no call",
      st["reachable"] is False and "DEPLOY_PI_ENGINE_URL" in st["engine"]["error"]
      and "DEPLOY_PI_ENGINE_URL" in st["copilot"]["error"])
check("manual-load commands show a placeholder host", D.targets()["pi_host"] == "<pi-host>")

# a push with nothing else wrong: the package and the lock are faked, the address is what's missing
D.package = lambda rid, course_id=None, **kw: {"race_name": "R", "course_load": {}, "fleet_load": {},
                                                "checklist_load": {"items": []}, "watch_load": None}
D.race_start = lambda d: None
from app import coursecheck  # noqa: E402
coursecheck.missing = lambda d, cid: []
res = D.push("r1")
check("push: refused, names the setting, nothing sent",
      res["ok"] is False and res.get("refused") and res["detail"] == D.NO_ENGINE_URL
      and res["steps"] == [])

check("plume: no push URL", P.push_url() is None)
rec = P.push({"key": "k", "race_start": 2_000_000_000, "built_at": 1}, post=no_network, now=1_000)
check("plume push: not sent, says which setting", rec["ok"] is False
      and "DEPLOY_PI_ENGINE_URL" in rec["error"])

print("set:")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi.example:8200/"
check("engine URL from env, trailing slash dropped", D.engine_url() == "http://pi.example:8200")
check("copilot defaults to the engine's host on the console port",
      D.copilot_url() == "http://pi.example:8091/copilot")
os.environ["DEPLOY_COPILOT_URL"] = "http://other.example/copilot/"
check("DEPLOY_COPILOT_URL wins", D.copilot_url() == "http://other.example/copilot")
check("plume goes to the engine", P.push_url() == "http://pi.example:8200/plume/load")
os.environ["PLUME_PUSH_URL"] = "http://plume.example/load"
check("PLUME_PUSH_URL wins", P.push_url() == "http://plume.example/load")
os.environ["DEPLOY_PI_HOST"] = "crew@pi.example"
check("manual-load host from env", D.targets()["pi_host"] == "crew@pi.example")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
