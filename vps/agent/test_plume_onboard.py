"""Onboard forecast plume — the Orin's own plume on the Pi engine (vps/agent/app/plume.py).

Pins the accept/refuse rules:
  * an onboard plume is taken WHILE RACING (race session running, past its race start) — it is
    the boat's own computer on public data;
  * it is taken only straight from the Orin's boat-local address, never from loopback, a
    Tailscale address, or through a proxy (forwarding headers), and only if it says so;
  * the shore plume's freeze is untouched: a shore plume is still refused after the gun, and a
    shore payload that CLAIMS to be onboard is still treated as shore;
  * GET /plume serves the newer of the two, the onboard one never frozen.
And the real engine endpoint wires the peer address + headers through.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_plume_onboard.py
"""
import os
import sys
import tempfile

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")
os.environ.pop("PLUME_ONBOARD_FROM", None)

from app import plume  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = 1_800_000_000
START = NOW - 3600                       # the race started an hour ago
ORIN = "10.10.10.2"
RUNS = [{"cycle": NOW - 7200, "at": NOW - 600, "pts": [[NOW, 11.2, 240], [NOW + 3600, 12.0, 245]]}]
SHORE = {"schema": "c4.plume/v1", "key": "r1__pb", "race_id": "r1", "basis": "route",
         "race_start": START, "generated_at": START - 900, "built_at": START - 1200,
         "models": [{"model": "gfs", "runs": [dict(RUNS[0], at=START - 1200)]}]}
ONBOARD = {"schema": "c4.plume/v1", "source": "onboard", "key": "onboard__r1__left", "race_id": "r1",
           "basis": "route", "label": "along the left route", "generated_at": NOW - 60,
           "built_at": NOW - 600, "models": [{"model": "hrrr", "runs": RUNS}]}

RACING = {"cur": {"id": 3, "kind": "race", "start_ts": START}}
plume.race_session = lambda: RACING["cur"]

print("shore plume before the gun, then the race starts:")
plume.race_session = lambda: None
check("shore pre-start plume taken", plume.load(SHORE, now=START - 600)["loaded"])
plume.race_session = lambda: RACING["cur"]
check("shore plume after the gun → still refused (freeze untouched)",
      plume.load(dict(SHORE, key="late", generated_at=NOW - 30), now=NOW)["loaded"] is False)
g = plume.get(NOW)
check("shore plume shows frozen, source shore", g["frozen"] and g["source"] == "shore")

print("onboard plume during the race:")
r = plume.load_onboard(ONBOARD, ORIN, headers={"content-type": "application/json"}, now=NOW - 30)
check("taken from the Orin's cable address while racing", r["loaded"] and r["models"] == 1)
g = plume.get(NOW)
check("served instead of the frozen shore plume", g["source"] == "onboard" and g["key"] == "onboard__r1__left")
check("never frozen", g["frozen"] is False and g["frozen_at"] is None)
check("age from its newest run", g["age_s"] == 600 and g["received_age_s"] == 30)
plume.load_onboard(dict(ONBOARD, race_start=START), ORIN, now=NOW)
check("a race_start it carries is dropped, so the console can't read it as frozen",
      "race_start" not in plume.get(NOW))

print("onboard refusals:")
for name, ip, hdrs, body in (
        ("from loopback (the console proxy / a shell on the Pi)", "127.0.0.1", {}, ONBOARD),
        ("from a Tailscale address (how shore reaches the Pi)", "100.79.180.102", {}, ONBOARD),
        ("from the Orin's Tailscale address", "100.70.110.72", {}, ONBOARD),
        ("from boat Wi-Fi", "192.168.1.50", {}, ONBOARD),
        ("no peer address", None, {}, ONBOARD),
        ("through a proxy claiming the Orin (X-Forwarded-For)", ORIN, {"X-Forwarded-For": ORIN}, ONBOARD),
        ("through a proxy (X-Real-IP)", ORIN, {"X-Real-IP": ORIN}, ONBOARD),
        ("through a proxy (Forwarded)", ORIN, {"Forwarded": "for=10.10.10.2"}, ONBOARD),
        ("payload doesn't say onboard", ORIN, {}, {k: v for k, v in ONBOARD.items() if k != "source"}),
        ("payload says shore", ORIN, {}, dict(ONBOARD, source="shore")),
        ("bad schema", ORIN, {}, dict(ONBOARD, schema="x")),
        ("unknown model", ORIN, {}, dict(ONBOARD, models=[{"model": "xyz", "runs": RUNS}])),
        ("no models", ORIN, {}, dict(ONBOARD, models=[]))):
    r = plume.load_onboard(dict(body, key="bad"), ip, headers=hdrs, now=NOW)
    check(f"{name} → refused", r["loaded"] is False)
check("...and the good onboard plume is still served", plume.get(NOW)["key"] == "onboard__r1__left")
big = dict(ONBOARD, models=[{"model": "gfs", "runs": [{"cycle": NOW, "at": NOW,
                                                     "pts": [[NOW + i, 1.0, 1] for i in range(60000)]}]}])
check("absurdly large → refused", plume.load_onboard(big, ORIN, now=NOW)["loaded"] is False)

print("the address list is configurable, never loopback by default:")
os.environ["PLUME_ONBOARD_FROM"] = "10.10.10.2, 10.10.10.9"
check("an extra configured address is taken", plume.load_onboard(ONBOARD, "10.10.10.9", now=NOW)["loaded"])
os.environ.pop("PLUME_ONBOARD_FROM")
check("default list is only the Orin's cable end", plume.onboard_from() == {ORIN})

print("a shore payload that claims to be onboard:")
plume.race_session = lambda: None
os.remove(plume._onboard_path())
fake = dict(SHORE, source="onboard", key="claims", race_start=NOW + 86400, generated_at=NOW - 10,
            built_at=NOW - 20)
check("pre-start, it loads through the shore door (shore rules)", plume.load(fake, now=NOW)["loaded"])
check("...and is still shown as shore", plume.get(NOW)["source"] == "shore")
plume.race_session = lambda: RACING["cur"]
check("...and once racing it is refused like any shore plume",
      plume.load(dict(fake, key="claims2"), now=NOW + 10)["loaded"] is False)
check("...and shows frozen", plume.get(NOW + 20)["frozen"] is True)

print("newer weather wins — by model cycle, not by when it was sampled:")
H = 3600


def mk(models_cycles, built):
    return {"plume": {"built_at": built, "models": [
        {"model": m, "runs": [{"cycle": c, "at": built, "pts": []}, {"cycle": c - 6 * H, "at": built, "pts": []}]}
        for m, c in models_cycles.items()]}}


C12 = NOW - 4 * H
check("shore GFS 12Z recorded 16:00 beats onboard GFS 06Z sampled 16:15",
      plume.prefer_onboard(mk({"gfs": C12 - 6 * H}, NOW), mk({"gfs": C12}, NOW - 900)) is False)
check("onboard GFS 18Z beats shore GFS 12Z (the frozen shore plume mid-race)",
      plume.prefer_onboard(mk({"gfs": C12 + 6 * H}, NOW - 9000), mk({"gfs": C12}, NOW)) is True)
check("more shared models newer onboard → onboard, even if shore's HRRR is an hour newer",
      plume.prefer_onboard(mk({"gfs": C12 + 6 * H, "nam": C12 + 6 * H, "hrrr": C12}, NOW),
                           mk({"gfs": C12, "nam": C12, "hrrr": C12 + H}, NOW)) is True)
check("split vote → the freshest shared model decides",
      plume.prefer_onboard(mk({"gfs": C12 + 6 * H, "hrrr": C12}, NOW), mk({"gfs": C12, "hrrr": C12 + 7 * H}, NOW)) is False)
check("models only one side has don't vote (ECMWF only ashore)",
      plume.prefer_onboard(mk({"gfs": C12 + 6 * H}, NOW), mk({"gfs": C12, "ecmwf": C12 + 6 * H}, NOW)) is True)
check("nothing shared → the more recently recorded",
      plume.prefer_onboard(mk({"hrrr": C12}, NOW), mk({"gfs": C12}, NOW - 60)) is True
      and plume.prefer_onboard(mk({"hrrr": C12}, NOW - 60), mk({"gfs": C12}, NOW)) is False)
check("same cycles everywhere → onboard on a full tie",
      plume.prefer_onboard(mk({"gfs": C12}, NOW), mk({"gfs": C12}, NOW)) is True)
older = {"model": "gfs", "runs": [dict(RUNS[0], cycle=START - 30 * H, at=NOW - 5)]}
plume.load_onboard(dict(ONBOARD, built_at=NOW - 5, models=[older]), ORIN, now=NOW)
check("through get(): onboard sampled later but an older GFS cycle → shore shown",
      plume.get(NOW)["source"] == "shore")
newer = {"model": "gfs", "runs": [dict(RUNS[0], cycle=START + 6 * H, at=NOW - 5)]}
plume.load_onboard(dict(ONBOARD, built_at=NOW - 5000, models=[newer]), ORIN, now=NOW)
check("through get(): onboard with the newer GFS cycle → onboard shown", plume.get(NOW)["source"] == "onboard")
os.remove(plume._path())
check("onboard alone → onboard shown", plume.get(NOW)["source"] == "onboard")
os.remove(plume._onboard_path())
g = plume.get(NOW)
check("neither → honest available false", g["available"] is False and "Orin" in g["note"])

print("the engine endpoint (real app, real peer address):")
try:
    os.environ.setdefault("DATA_SOURCE", "onboard")
    from fastapi.testclient import TestClient
    import engine_app
    have_app = True
except Exception as exc:  # noqa: BLE001
    have_app = False
    print(f"  (engine app not importable here: {type(exc).__name__}: {exc})")
    ok = False
if have_app:
    def peer(ip):
        """The engine app as seen from `ip` (sets the ASGI peer address, like uvicorn does)."""
        async def asgi(scope, receive, send):
            if scope["type"] == "http":
                scope = dict(scope, client=(ip, 50000))
            await engine_app.app(scope, receive, send)
        return TestClient(asgi)

    orin, shore, local = peer(ORIN), peer("100.79.180.102"), peer("127.0.0.1")
    r = orin.post("/plume/onboard", json=ONBOARD).json()
    check("Orin → POST /plume/onboard taken while racing", r["loaded"])
    check("shore (Tailscale) → refused", shore.post("/plume/onboard", json=ONBOARD).json()["loaded"] is False)
    # uvicorn trusts X-Forwarded-For from 127.0.0.1 — exactly how the console's nginx reaches the engine
    r = local.post("/plume/onboard", json=ONBOARD, headers={"X-Forwarded-For": ORIN}).json()
    check("via the console proxy with a forged X-Forwarded-For → refused", r["loaded"] is False)
    check("shore /plume/load still refused while racing",
          shore.post("/plume/load", json=dict(SHORE, key="x", generated_at=NOW)).json()["loaded"] is False)
    g = orin.get("/plume").json()
    check("GET /plume serves the onboard plume, not frozen", g["source"] == "onboard" and g["frozen"] is False)

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
