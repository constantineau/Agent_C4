"""N2K address drift (#185-4): checked on the boat, shown on the DATA tile, and source priority
follows the device, not the address.

Pins:
  * `n2k_sources.drift()` sees a re-claimed address even when the serial matches (the GPSMAP 943
    and its virtual input handler share one);
  * `verified()` follows a device to its new address and drops the stale one; `moved()` explains;
  * the engine's check reads Signal K's sources (flat `sources-cache.json` shape or the nested
    REST shape), installs the verified map, and `devices_for` then returns it, so the live
    priority pick binds to the device at its new address;
  * /health/sensors carries provenance.checks.n2k_addresses: "not checked" until a good read (a
    note, status untouched), a followed move is a note, a device gone while its address is held
    by another is a warning that raises the chip;
  * a failed read keeps the last good map; a list with no product info is "not checked".

Run in-container (make ci): PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_n2k_drift.py
"""
import copy
import os
import sys
import tempfile

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")
os.environ["N2K_CHECK"] = "0"

from shared import n2k_sources as ns, source_policy  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


STATIC = ns.SR33_DEVICES


def flat(devices):
    """A sources-cache.json style payload from an address→device map."""
    return {s: {"updates": [{"source": {"manufacturerCode": d["manufacturer"], "modelId": d["model"],
                                        "modelSerialCode": d["serial"]}}]}
            for s, d in devices.items()}


def nested(devices):
    """The REST /signalk/v1/api/sources shape: provider -> address -> {n2k: {...}}."""
    out = {}
    for s, d in devices.items():
        prov, addr = s.rsplit(".", 1)
        out.setdefault(prov, {"type": "NMEA2000", "label": prov})[addr] = {
            "n2k": {"src": addr, "manufacturerName": d["manufacturer"], "modelId": d["model"],
                    "modelSerialCode": d["serial"]}}
    return out


# the GPS24xd re-claimed address 20; the GST10 (was .4) now sits on its old address .3
MOVED = copy.deepcopy(STATIC)
MOVED["n2k-socketcan.20"] = MOVED.pop("n2k-socketcan.3")
MOVED["n2k-socketcan.3"] = MOVED.pop("n2k-socketcan.4")

print("shared/n2k_sources:")
swap = copy.deepcopy(STATIC)
swap["n2k-socketcan.5"], swap["n2k-socketcan.11"] = swap["n2k-socketcan.11"], swap["n2k-socketcan.5"]
d = ns.drift(flat(swap), STATIC)
check("drift sees the 943 and its input handler swap (same serial)",
      [x[0] for x in d] == ["n2k-socketcan.11", "n2k-socketcan.5"])
check("no drift on the boat's own map", ns.drift(flat(STATIC), STATIC) == [])
v = ns.verified(flat(MOVED), STATIC)
check("verified: the GPS24xd is followed to .20", v["n2k-socketcan.20"]["model"] == "GPS24xd-NMEA2000")
check("verified: .3 is now the GST10", v["n2k-socketcan.3"]["model"] == "GST10")
check("verified: the GST10's old .4 (silent) is dropped", "n2k-socketcan.4" not in v)
off = copy.deepcopy(STATIC)
off.pop("n2k-socketcan.15")
check("verified: a powered-off device keeps its static entry",
      ns.verified(flat(off), STATIC)["n2k-socketcan.15"]["model"] == "Orca Core")
mv = {m["source"]: m for m in ns.moved(flat(MOVED), STATIC)}
check("moved: .3 expected the GPS24xd, found the GST10, the GPS24xd is at .20",
      mv.get("n2k-socketcan.3") == {"source": "n2k-socketcan.3", "expected": "GPS24xd-NMEA2000",
                                    "found": "GST10", "now_at": "n2k-socketcan.20"})
check("moved: the GST10 left .4 for .3 (old address silent)",
      (mv.get("n2k-socketcan.4") or {}).get("now_at") == "n2k-socketcan.3")

print("engine check + priority:")
import engine_app  # noqa: E402

rows = [("n2k-socketcan.20", 100.0, 1.0), ("n2k-socketcan.3", 100.0, 2.0)]
before = source_policy.preferred("sog", rows, ns.devices_for("sr33"))
check("static map: SOG binds to whatever holds .3 (the bug)", before[0] == "n2k-socketcan.3")

st = {"checked_at": None, "error": None, "moved": [], "static": None}
row = engine_app.n2k_check(st)
check("never read: 'not checked', a note, status unknown", row["status"] == "unknown" and row["note"])
h = engine_app.with_n2k_check({"available": True, "status": "ok", "flags": [], "notes": [],
                               "provenance": {"status": "ok", "checks": {}, "flags": [], "notes": []}}, st)
check("never read: the chip status is untouched", h["status"] == "ok" and not h["flags"]
      and h["provenance"]["checks"]["n2k_addresses"]["status"] == "unknown")

good = engine_app.check_n2k_once(st, fetch=lambda: nested(MOVED))
check("a good read of the nested REST shape", good and st["checked_at"])
after = source_policy.preferred("sog", rows, ns.devices_for("sr33"))
check("verified map: SOG follows the GPS24xd to .20", after[0] == "n2k-socketcan.20")
row = engine_app.n2k_check(st)
check("a followed move is a note, status ok", row["status"] == "ok" and "moved to" in (row["note"] or ""))

gone = copy.deepcopy(STATIC)
gone["n2k-socketcan.3"] = gone.pop("n2k-socketcan.4")      # GST10 on .3, the GPS24xd not on the bus
st2 = {"checked_at": None, "error": None, "moved": [], "static": None}
engine_app.check_n2k_once(st2, fetch=lambda: flat(gone))
h = engine_app.with_n2k_check({"available": True, "status": "unknown", "flags": [], "notes": [],
                               "provenance": {"status": "unknown", "checks": {}, "flags": [], "notes": []}}, st2)
c = h["provenance"]["checks"]["n2k_addresses"]
check("device gone + address re-claimed: warn", c["status"] == "warn")
check("the warning raises the chip even over unknown", h["status"] == "warn")
check("the flag names the missing device and who holds its address",
      any("GPS24xd-NMEA2000 not found" in f and "GST10" in f for f in h["flags"]))

partial = {s: d for s, d in gone.items() if s not in ("n2k-socketcan.3",)}
engine_app.check_n2k_once(st2, fetch=lambda: flat(partial))
check("a later list that omits the device keeps what an earlier read proved",
      ns.devices_for("sr33")["n2k-socketcan.3"]["model"] == "GST10"
      and engine_app.n2k_check(st2)["status"] == "warn")

st5 = {"checked_at": None, "error": None, "moved": [], "static": None}
only20 = {"n2k-socketcan.20": STATIC["n2k-socketcan.3"]}          # GPS24xd seen at .20
engine_app.check_n2k_once(st5, fetch=lambda: flat(only20))
engine_app.check_n2k_once(st5, fetch=lambda: flat({"n2k-socketcan.20": STATIC["n2k-socketcan.4"]}))
check("a disproved address never falls back to its static device",
      "n2k-socketcan.3" not in ns.devices_for("sr33"))

kept = ns.devices_for("sr33")
check("a failed read keeps the last good map",
      not engine_app.check_n2k_once(st2, fetch=lambda: (_ for _ in ()).throw(OSError("down")))
      and ns.devices_for("sr33") == kept and st2["error"])
st3 = {"checked_at": None, "error": None, "moved": [], "static": None}
check("a list with no product info is 'not checked'",
      not engine_app.check_n2k_once(st3, fetch=lambda: {"derived-data": {"type": "plugin"}})
      and engine_app.n2k_check(st3)["status"] == "unknown")

numeric = {"n2k-socketcan": {"20": {"n2k": {"src": "20", "manufacturerCode": 229,
                                             "modelId": "GPS24xd-NMEA2000",
                                             "modelSerialCode": "3479803194"}}}}
st4 = {"checked_at": None, "error": None, "moved": [], "static": None}
engine_app.check_n2k_once(st4, fetch=lambda: numeric)
try:
    pick = source_policy.preferred("sog", rows, ns.devices_for("sr33"))
    check("a numeric manufacturer code can't crash priority reads", pick[0] == "n2k-socketcan.20")
except Exception as exc:
    check(f"a numeric manufacturer code can't crash priority reads ({exc})", False)
both = {"n2k-socketcan": {"20": {"n2k": {"manufacturerCode": 229, "manufacturerName": "Garmin",
                                          "modelId": "GST10", "modelSerialCode": "1"}}}}
check("the manufacturer NAME is kept over the numeric code",
      ns.build_from_signalk(engine_app.flatten_sources(both))["n2k-socketcan.20"]["manufacturer"] == "Garmin")

check("the sources URL comes from SIGNALK_WS",
      engine_app._signalk_sources_url() == "http://localhost:3010/signalk/v1/api/sources")

ns.set_verified("sr33", None)
check("clearing the verified map restores the static one", ns.devices_for("sr33") is STATIC)

from fastapi.testclient import TestClient  # noqa: E402
engine_app.onboard_conditions.get_current_conditions = lambda: {"channels": {}}
engine_app.sensor_health.assess = lambda **kw: {
    "available": True, "status": "ok", "flags": [], "notes": [], "heading": {},
    "provenance": {"status": "ok", "checks": {}, "flags": [], "notes": []}}
r = TestClient(engine_app.app).get("/health/sensors").json()
check("/health/sensors carries the check (endpoint wired)",
      "n2k_addresses" in ((r.get("provenance") or {}).get("checks") or {}))

print("ALL OK" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
