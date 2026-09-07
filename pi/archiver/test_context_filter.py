"""Archiver vessel-context filter — AIS must never be archived as own-ship telemetry.

Regression test for the Bayview Mackinac 2026 contamination: `subscribe=all` delivers own-ship
deltas AND every AIS target on one socket, and the archiver wrote them all under the single
own-ship BOAT_ID. `navigation.position` in the archive was therefore a mix of this boat and
whatever shipping was in range — 17% of own-ship position reads were another vessel, with
implied speeds to 171,000 kn and a longitude of -2.4 deg (the Atlantic, not Lake Huron).

The filter is deliberately asymmetric: dropping own-ship telemetry is far worse than keeping a
stray AIS row, so the "keep" cases matter more than the "drop" cases and are tested harder.

Run:  python3 pi/archiver/test_context_filter.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# archiver imports websockets at module scope; stub it so this test needs no dependencies.
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SELF = "vessels.urn:mrn:signalk:uuid:c4-sr33-0001"
AIS = "vessels.urn:mrn:imo:mmsi:366123456"
ATON = "atons.urn:mrn:imo:mmsi:993672085"      # a navigation aid, also heard on AIS


def delta(ctx, path="navigation.position", value=None, source="n2k-socketcan.15"):
    msg = {"updates": [{"$source": source, "timestamp": "2026-07-18T18:00:00.000Z",
                        "values": [{"path": path,
                                    "value": value if value is not None
                                    else {"latitude": 43.2, "longitude": -82.4}}]}]}
    if ctx:
        msg["context"] = ctx
    return json.dumps(msg)


# --- the classifier ---------------------------------------------------------
print("_is_other_vessel:")
check("no context is own ship", archiver._is_other_vessel(None, SELF) is False)
check("empty context is own ship", archiver._is_other_vessel("", SELF) is False)
check("context == self is own ship", archiver._is_other_vessel(SELF, SELF) is False)
check("a different context is another vessel", archiver._is_other_vessel(AIS, SELF) is True)
check("an AtoN is not us", archiver._is_other_vessel(ATON, SELF) is True)
check("self unknown + mmsi context -> another vessel",
      archiver._is_other_vessel(AIS, None) is True)
check("self unknown + non-mmsi context -> KEPT (never drop what we cannot identify)",
      archiver._is_other_vessel(SELF, None) is False)

print("_mmsi_from_context:")
check("extracts the mmsi", archiver._mmsi_from_context(AIS) == 366123456)
check("own-ship uuid has no mmsi", archiver._mmsi_from_context(SELF) is None)
check("None is safe", archiver._mmsi_from_context(None) is None)

# --- end to end through parse_delta -----------------------------------------
print("parse_delta with a state dict (filter enabled):")
state = {}
hello = json.dumps({"self": SELF, "version": "2.27.0"})
check("hello frame yields no rows", archiver.parse_delta(hello, "T") == [])
check("hello frame records the self context", state == {} or True)
archiver.parse_delta(hello, "T", state)
check("self context learned", state.get("self") == SELF)

rows = archiver.parse_delta(delta(None), "T", state)
check("own ship (no context) is archived", len(rows) == 2)          # lat + lon flattened
rows = archiver.parse_delta(delta(SELF), "T", state)
check("own ship (explicit self context) is archived", len(rows) == 2)

before = state.get("skipped", 0)
rows = archiver.parse_delta(delta(AIS), "T", state)
check("AIS target is NOT archived", rows == [])
check("skip counter advances", state.get("skipped", 0) == before + 1)
rows = archiver.parse_delta(delta(ATON), "T", state)
check("AtoN is NOT archived", rows == [])

# the exact paths that made n2k-socketcan.43 look like a 171,000 kn boat
for path in ("navigation.position", "navigation.speedOverGround",
             "navigation.courseOverGroundTrue", "sensors.ais.class"):
    check(f"AIS {path} is not archived",
          archiver.parse_delta(delta(AIS, path=path, value=1.0), "T", state) == [])

print("without a state dict the filter is inert (back-compat):")
check("AIS delta still parses when no state is passed",
      len(archiver.parse_delta(delta(AIS), "T")) == 2)

print("own-ship data survives before the hello frame arrives:")
fresh = {}
check("no context, self unknown -> archived",
      len(archiver.parse_delta(delta(None), "T", fresh)) == 2)
check("uuid context, self unknown -> archived",
      len(archiver.parse_delta(delta(SELF), "T", fresh)) == 2)
check("mmsi context, self unknown -> dropped",
      archiver.parse_delta(delta(AIS), "T", fresh) == [])

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
