"""Frozen at the gun on the Orin: the copilot's POST /playbook/load asks the Pi engine whether the
boat is racing (GET /racing; the Orin has no race log) and refuses unless the Pi says no.

Pins:
  * not racing → the bundle is written and loaded;
  * racing → refused, the file aboard unchanged;
  * the Pi can't tell (unreachable, an old engine without /racing, a bad answer) → refused
    (fail closed), file unchanged;
  * the Pi's real GET /racing answer shape is what the copilot reads (racing: bool).

Stubbed engine, temp PLAYBOOK_PATH; no network.
    cd pi/orin && python3 -m copilot.test_playbook_gate
"""
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from . import app as app_mod
from . import config

client = TestClient(app_mod.app)        # no `with` → the coach timer never starts
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


PATH = os.path.join(tempfile.mkdtemp(), "playbook.json")
config.PLAYBOOK_PATH = PATH
A = {"race_id": "r1", "variants": [{"id": "a"}], "plays": []}
B = {"race_id": "r2", "variants": [{"id": "b"}], "plays": []}
ANSWER = {"v": {"racing": False}}


class StubEngine:
    base_url = "http://stub:8200"

    def __init__(self, *a, **k):
        pass

    def racing(self):
        v = ANSWER["v"]
        if isinstance(v, Exception):
            raise v
        return v

    def __getattr__(self, name):           # any other read: the engine's "not available" shape
        return lambda *a, **k: {"available": False}


app_mod.EngineClient = StubEngine


def on_disk():
    try:
        with open(PATH) as f:
            return json.load(f).get("race_id")
    except OSError:
        return None


print("not racing:")
r = client.post("/playbook/load", json=A).json()
check("loaded", r.get("loaded") is True and r.get("race_id") == "r1" and on_disk() == "r1")

print("racing:")
ANSWER["v"] = {"racing": True}
r = client.post("/playbook/load", json=B).json()
check("refused, plain", r == {"loaded": False, "detail": "Racing now. Not loaded."})
check("file unchanged", on_disk() == "r1")

print("can't tell → refused (fail closed):")
for label, v in (("engine unreachable", {"available": False, "error": "timed out"}),
                 ("old engine, no /racing (404)", {"available": False, "error": "HTTP 404"}),
                 ("bad answer", {"racing": "no"}),
                 ("client raised", OSError("boom"))):
    ANSWER["v"] = v
    r = client.post("/playbook/load", json=B).json()
    check(f"{label}: refused", r == {"loaded": False, "detail": "Can't check the race. Not loaded."})
check("file unchanged", on_disk() == "r1")

print("not racing again:")
ANSWER["v"] = {"racing": False}
r = client.post("/playbook/load", json=B).json()
check("loaded", r.get("loaded") is True and on_disk() == "r2")

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
