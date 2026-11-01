"""Frozen at the gun on the Orin: a playbook loads in two steps, both ONLY from the Pi engine over
the cable, and the write is bounded in time on the Orin's own monotonic clock.

The Pi holds its race-log session lock across POST /playbook/stage and POST /playbook/commit
(pi/engine/engine_app.py copilot_load). The Orin commits only within PLAYBOOK_COMMIT_TTL_S of
RECEIVING the stage, re-checked right before the file goes live, and the Pi waits at least the TTL
for the commit's answer. So a stalled request can't land after the Pi gave up and a race started.

Pins:
  * from the Pi's cable address, no proxy headers: stage then commit → written and loaded;
  * from anywhere else, or through a proxy (X-Forwarded-For etc.) → 403, plain, file unchanged;
  * a commit later than the TTL after its stage → "Too late. Not loaded.", file unchanged;
  * a write that stalls past the TTL (delayed remote write) never goes live, even though the
    commit arrived in time: the Pi gives up, and nothing lands afterwards;
  * wrong nonce, or a second commit of the same stage → refused;
  * PLAYBOOK_LOAD_FROM defaults to the Pi's cable address; the TTL is under the Pi's 4 s wait.

Temp PLAYBOOK_PATH; no network.
    cd pi/orin && python3 -m copilot.test_playbook_gate
"""
import json
import os
import sys
import tempfile
import threading
import time

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
TOO_LATE = {"loaded": False, "detail": "Too late. Not loaded."}


def client_from(host):
    """The app as seen from `host` (sets the ASGI peer address, like uvicorn does). No `with`, so
    the coach timer never starts."""
    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(host, 50000))
        await app_mod.app(scope, receive, send)
    return TestClient(asgi)


PI = client_from("10.10.10.1")


def on_disk():
    try:
        with open(PATH) as f:
            return json.load(f).get("race_id")
    except OSError:
        return None


def load(bundle, nonce, client=PI, headers=None, gap=0.0):
    st = client.post("/playbook/stage", json={"nonce": nonce, "bundle": bundle}, headers=headers)
    if gap:
        time.sleep(gap)
    cm = client.post("/playbook/commit", json={"nonce": nonce}, headers=headers)
    return st, cm


check("default: only the Pi's cable address", config.PLAYBOOK_LOAD_FROM == frozenset({"10.10.10.1"}))
check("default TTL is under the Pi's 4 s commit wait", 0 < config.PLAYBOOK_COMMIT_TTL_S < 4)
config.PLAYBOOK_COMMIT_TTL_S = 0.5                  # short, for the timing cases below

print("from the Pi engine, over the cable:")
st, cm = load(A, "nonce-0001")
check("staged, with the TTL", st.json() == {"staged": True, "nonce": "nonce-0001", "ttl_s": 0.5})
check("committed and loaded", cm.json().get("loaded") is True and on_disk() == "r1")
check("the old one-shot /playbook/load is gone", PI.post("/playbook/load", json=B).status_code in (404, 405))

print("from anywhere else, or through a proxy:")
for host in ("100.79.180.102", "192.168.1.20", "127.0.0.1", "10.10.10.2"):
    st, cm = load(B, "nonce-0002", client=client_from(host))
    check(f"{host}: stage and commit 403, plain", st.status_code == 403 and cm.status_code == 403
          and st.json() == REFUSED and cm.json() == REFUSED)
for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
    st, cm = load(B, "nonce-0003", headers={h: "192.168.1.20"})
    check(f"{h}: 403", st.status_code == 403 and cm.status_code == 403)
check("file unchanged", on_disk() == "r1")

print("the commit window (Orin monotonic clock, from the stage's receipt):")
st, cm = load(B, "nonce-0004", gap=0.8)
check("commit after the TTL → too late", cm.json() == TOO_LATE and on_disk() == "r1")
PI.post("/playbook/stage", json={"nonce": "nonce-0005", "bundle": B})
check("wrong nonce → refused", PI.post("/playbook/commit", json={"nonce": "nonce-9999"}).json()
      == {"loaded": False, "detail": "Nothing staged. Not loaded."} and on_disk() == "r1")
st, cm = load(B, "nonce-0006")
check("in time → loaded", cm.json().get("loaded") is True and on_disk() == "r2")
again = PI.post("/playbook/commit", json={"nonce": "nonce-0006"}).json()
check("one commit per stage", again == {"loaded": False, "detail": "Nothing staged. Not loaded."})

print("a delayed remote write: the Pi gives up, nothing lands afterwards:")
real_write = app_mod._write_tmp


def stalled_write(path, bundle):
    time.sleep(1.2)                                  # the Orin's disk stalls past the TTL
    return real_write(path, bundle)


app_mod._write_tmp = stalled_write
PI.post("/playbook/stage", json={"nonce": "nonce-0007", "bundle": A})
res = {}
t = threading.Thread(target=lambda: res.setdefault(
    "r", PI.post("/playbook/commit", json={"nonce": "nonce-0007"}).json()))
t.start()
t.join(0.5 + 0.3)                                    # the Pi waits the TTL (+ margin), then gives up
check("the Pi gave up while the Orin was still writing", t.is_alive())

check("…and nothing had landed", on_disk() == "r2")
t.join(5)
check("the stalled commit refused itself", res.get("r") == TOO_LATE)
check("nothing landed after the Pi gave up (a race may have started)", on_disk() == "r2")
check("no temp file left", not os.path.exists(PATH + ".tmp"))
app_mod._write_tmp = real_write

print("short text:")
for s in (REFUSED["detail"], TOO_LATE["detail"]):
    check(f"{s!r} ≤ 9 words", len(s.split()) <= 9)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
