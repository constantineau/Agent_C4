"""#304 — aboard, the polar, speed guide and sail crossovers are the DEPLOYED boat's.

The Pi engine image carries no boat data. The Lab's Deploy push (POST /reroute/assets) carries the
boat profile and its polar block; the engine's polar table, targets and sail dial read that, and
with nothing deployed they say "No boat polar loaded." — never sr33's numbers. Pinned:
  * nothing deployed → empty polar, the sail dial refuses plainly;
  * sr33 deployed (its block) → the polar and the dial match sr33's own files exactly;
  * Bravo deployed (cert profile, its block) → Bravo's polar, no dial, and NO sr33 file opened;
  * a deployed profile with an inline cert but no block → that cert, still not sr33's;
  * the store refuses a polar for another boat, a tampered one, or one without a profile;
  * the re-route request carries the stored polar to the Orin.
"""
import builtins
import io
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATA_SOURCE"] = "onboard"
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine.db")
os.environ.pop("POLARS_FILE", None)
os.environ.pop("BOAT_ID", None)
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

from shared import boat_polar as BP, boat_profile  # noqa: E402
from app import datasource_onboard as DSO, orinassets, sails  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def profile(bid):
    with open(os.path.join(REPO, "vps", "lab", "boats", f"{bid}.json")) as f:
        return json.load(f)


SR33, BRAVO = profile("sr33"), profile("bravo")
DIRS = (os.path.join(REPO, "vps", "db", "seed"), os.path.join(REPO, "vps", "agent", "knowledge"))
P33 = BP.resolve(SR33, dirs=DIRS)
PB = BP.resolve(BRAVO, dirs=DIRS)
SR33_FILES = set(SR33["polar_files"].values())


def deploy(boat, polar, **extra):
    body = {"schema": orinassets.SCHEMA, "race_id": "r", "course_id": "c", "chart": None,
            "boat_id": boat["boat_id"] if boat else None, "boat": boat,
            "boat_version": boat_profile.version(boat) if boat else None, "polar": polar}
    body.update(extra)
    okv, why = orinassets.validate(body)
    if okv:
        orinassets._write(json.dumps({"received_at": 1, "assets": body}))
    return okv, why


print("1) nothing deployed → no polar, plainly")
check("no polar block", orinassets.boat_polar() is None)
check("engine polar table empty", DSO._polar_rows() == [])
adv = sails.get_sail_advice(12, 45, "J1")
check("sail dial: 'No boat polar loaded.'", adv["available"] is False
      and adv["note"] == BP.NO_POLAR_ABOARD)
check("the image has no polar file the engine could fall back on",
      DSO.POLARS_FILE is None)

print("2) sr33 deployed → sr33's own polar and guide, exactly")
check("stored", deploy(SR33, P33) == (True, ""))
rows = DSO._polar_rows()
with open(os.path.join(DIRS[0], "polars_sr33.sql")) as f:
    want = BP.parse_polar_sql(f.read())
check("polar = sr33's ORC file, row for row",
      [(r["tws"], r["twa"], r["target_stw"], r["target_vmg"]) for r in rows] == want)
a = sails.get_sail_advice(12, 45, "J1")
check("sail dial available, with targets", a["available"] and a["targets"])
os.environ["BOAT_ID"] = "sr33"            # the shore agent's path: sr33's files by its boat id
os.environ["DATA_SOURCE"] = "cloud"
shore = [sails.get_sail_advice(t, w, h) for t in (6, 12, 18) for w in (40, 90, 140) for h in ("J1", "A3")]
os.environ["DATA_SOURCE"] = "onboard"
os.environ.pop("BOAT_ID")
aboard = [sails.get_sail_advice(t, w, h) for t in (6, 12, 18) for w in (40, 90, 140) for h in ("J1", "A3")]
check("the deployed guide gives the same advice as sr33's own files", shore == aboard)

print("3) Bravo deployed → Bravo's polar, no sr33 file opened")
opened = []
_open, _io = builtins.open, io.open


def spy(file, *a, **k):
    opened.append(os.path.basename(str(file)))
    return _open(file, *a, **k)


builtins.open = io.open = spy
try:
    check("stored", deploy(BRAVO, PB) == (True, ""))
    rows_b = DSO._polar_rows()
    adv_b = sails.get_sail_advice(12, 45, "J1")
    req = orinassets.for_run({"race_id": "r"}, "c")
finally:
    builtins.open, io.open = _open, _io
check("polar = Bravo's cert", [(r["tws"], r["twa"], r["target_stw"]) for r in rows_b]
      == [tuple(map(float, r)) for r in BRAVO["cert_polar"]])
check("no speed guide for Bravo → the dial says so, not sr33's call",
      adv_b["available"] is False and adv_b["note"] == BP.NO_POLAR_ABOARD)
check("the re-route request carries Bravo's polar", req["polar"]["boat_id"] == "bravo"
      and req["polar"]["version"] == PB["version"])
leaked = sorted(set(opened) & SR33_FILES)
check(f"no sr33 polar file opened (opened: {leaked or 'none'})", not leaked)

print("4) a deployed cert profile without a block → its own cert")
check("stored", deploy(BRAVO, None) == (True, ""))
check("Bravo's cert from the profile", orinassets.boat_polar()["source"] == "cert"
      and orinassets.boat_polar()["boat_id"] == "bravo")
check("sr33 profile without its block → no polar (no files aboard)",
      deploy(SR33, None) == (True, "") and orinassets.boat_polar() is None)

print("5) the store refuses a bad polar")
check("another boat's polar", deploy(BRAVO, P33)[0] is False)
check("a tampered polar", deploy(BRAVO, dict(PB, label="x"))[0] is False)
check("a polar without a profile", deploy(None, PB)[0] is False)
check("summary names the polar", orinassets.summary({"boat": BRAVO, "polar": PB})["polar"]
      == BP.summary(PB))

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
