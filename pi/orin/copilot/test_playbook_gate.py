"""Frozen at the gun on the Orin: the copilot's POST /playbook/load takes a playbook ONLY from the
Pi engine, straight over the cable. The engine forwards it while it holds the race log's session
lock (pi/engine/engine_app.py playbook_load), so the race check and this write can't be split by
a race start.

Pins:
  * from the Pi's cable address, no proxy headers → written and loaded;
  * from anywhere else (the Lab, the iPad, Tailscale) → 403, plain, file unchanged;
  * from the Pi's address but through a proxy (the console's /copilot/ nginx route sets
    X-Forwarded-For) → 403, file unchanged;
  * PLAYBOOK_LOAD_FROM defaults to the Pi's cable address only.

Temp PLAYBOOK_PATH; no network.
    cd pi/orin && python3 -m copilot.test_playbook_gate
"""
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from . import app as app_mod
from . import config

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


PATH = os.path.join(tempfile.mkdtemp(), "playbook.json")
config.PLAYBOOK_PATH = PATH
A = {"race_id": "r1", "variants": [{"id": "a"}], "plays": []}
B = {"race_id": "r2", "variants": [{"id": "b"}], "plays": []}
REFUSED = {"loaded": False, "detail": "Load the playbook from the Pi. Not loaded."}


def client_from(host):
    """The app as seen from `host` (sets the ASGI peer address, like uvicorn does). No `with`, so
    the coach timer never starts."""
    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(host, 50000))
        await app_mod.app(scope, receive, send)
    return TestClient(asgi)


def on_disk():
    try:
        with open(PATH) as f:
            return json.load(f).get("race_id")
    except OSError:
        return None


check("default: only the Pi's cable address", config.PLAYBOOK_LOAD_FROM == frozenset({"10.10.10.1"}))

print("from the Pi engine, over the cable:")
r = client_from("10.10.10.1").post("/playbook/load", json=A)
check("loaded", r.status_code == 200 and r.json().get("loaded") is True and on_disk() == "r1")

print("from anywhere else:")
for host in ("100.79.180.102", "192.168.1.20", "127.0.0.1", "10.10.10.2"):
    r = client_from(host).post("/playbook/load", json=B)
    check(f"{host}: 403, plain", r.status_code == 403 and r.json() == REFUSED)
check("file unchanged", on_disk() == "r1")

print("from the Pi's address but through the console proxy:")
for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
    r = client_from("10.10.10.1").post("/playbook/load", json=B, headers={h: "192.168.1.20"})
    check(f"{h}: 403", r.status_code == 403 and r.json() == REFUSED)
check("file unchanged", on_disk() == "r1")

print("the Pi again:")
r = client_from("10.10.10.1").post("/playbook/load", json=B)
check("loaded", r.json().get("loaded") is True and on_disk() == "r2")

print("short text:")
check("≤ 9 words", len(REFUSED["detail"].split()) <= 9)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
