"""Monitor map: our boat is picked out of the tracker fleet by name (#87).

The map drew all 106 tracker boats as identical blue dots, ours included, so the only way to find
C4 was to tap dots. The backend now flags the fix whose name matches the race's `own.boat` and
passes the name through; the map draws that fix as ours and labels the nearest rivals.

Run in-container (make ci does this): see test_monitor_freshness.py.
"""
import time

from app import monitor as M

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def payload(names):
    now_ms = time.time() * 1000
    return {"teams": [{"name": n, "positions": [{"latitude": 44.0, "longitude": -82.5,
                                                  "gpsAtMillis": now_ms - 600_000}]} for n in names]}


def fleet_for(definition, names):
    real = M._http_json
    M._http_json = lambda url, **kw: payload(names)
    try:
        return M.fleet(definition)
    finally:
        M._http_json = real


TRACKER = {"provider": "yb", "race": "bayviewmack2026", "delay_min": 15}

print("our boat in the tracker feed")
f = fleet_for({"tracker": TRACKER, "own": {"boat": "C4"}}, ["Joyride", "C4", "Nelda Ray"])
check("own_name comes from the race's own.boat", f["own_name"] == "C4")
check("exactly the C4 fix is flagged own", [x["name"] for x in f["fixes"] if x.get("own")] == ["C4"])

print("name match ignores case and stray whitespace")
f = fleet_for({"tracker": TRACKER, "own": {"boat": " c4 "}}, ["C4 ", "C44"])
check("'C4 ' matches ' c4 ', 'C44' does not", [x["name"] for x in f["fixes"] if x.get("own")] == ["C4 "])

print("no own boat configured")
f = fleet_for({"tracker": TRACKER, "own": {"boat": ""}}, ["C4", "Joyride"])
check("own_name is None", f["own_name"] is None)
check("nothing is flagged", not any(x.get("own") for x in f["fixes"]))
f = fleet_for({"tracker": TRACKER}, ["C4"])
check("a race with no `own` block is fine too", f["own_name"] is None and not f["fixes"][0].get("own"))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
