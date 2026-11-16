"""New boat from its ORC certificate (POST /api/boats/from-orc).

  * a cert link, a bare RefNo or a sail number all find the record; a link is never fetched;
  * a RefNo with no country: the race/boat defaults, then USA, and the reply says what was searched;
  * no `save` → a preview and nothing stored; `save` → a profile with draft, cert polar and the
    cert facts (RefNo, issue date, GPH);
  * never over a different boat of the same name; the same RefNo again refreshes that profile;
  * a cert-only boat gets its polar from `polar_for` and is in /api/boats/library.

Offline: `fleetimport._orc_dump` is stubbed; the network is never touched.
"""
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
HERE = os.path.dirname(os.path.abspath(__file__))

from fastapi.testclient import TestClient  # noqa: E402

from shared import boat_polar as BP, boat_profile  # noqa: E402
from app import auth, boats, certboat, fleetimport, main  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


boats.EDITED_DIR = os.path.join(_tmp, "boats")
boats.BUNDLED_DIR = os.path.join(_tmp, "bundled")
os.makedirs(boats.EDITED_DIR)
os.makedirs(boats.BUNDLED_DIR)
shutil.copy(os.path.join(HERE, "testdata", "boats", "bravo.json"), boats.BUNDLED_DIR)


def _rec(ref, sail, name, draft=3.2, cls="TP 52", gph=438.9):
    return {"RefNo": ref, "CertNo": "US1", "SailNo": sail, "YachtName": name, "Class": cls,
            "IssueDate": "2026-11-12T20:13:01.664Z", "Draft": draft, "LOA": 15.84, "MB": 4.43,
            "GPH": gph, "Age_Year": 2006.0, "NatAuth": sail.split()[0],
            "Allowances": {"WindSpeeds": [6, 10, 16], "WindAngles": [52, 90, 150],
                           "R52": [600, 450, 400], "R90": [500, 400, 350],
                           "R150": [700, 500, 380], "Beat": [900, 700, 650],
                           "BeatAngle": [44, 41, 39], "Run": [800, 550, 420],
                           "GybeAngle": [145, 150, 170]}}


HOOL = _rec("045600052L8", "USA 52521", "HOOLIGAN")
TWIN = _rec("04560009ZZZ", "USA 777", "HOOLIGAN", draft=2.0, cls="J/111")   # same name, other boat
GBR = _rec("034500011AB", "GBR 1", "RULE BRITANNIA", draft=2.5, cls="Fast 40")
DUMPS = {"USA": [HOOL, TWIN], "GBR": [GBR]}
calls = []


def _dump(cc):
    calls.append(cc)
    if cc not in DUMPS:
        raise RuntimeError("no such dump")
    rows = DUMPS[cc]
    idx = {"by_sail": {}, "by_name": {}, "n": len(rows)}
    for r in rows:
        idx["by_sail"].setdefault(fleetimport._norm_sail(r["SailNo"]), r)
        idx["by_name"].setdefault(fleetimport._norm(r["YachtName"]), r)
    return idx


print("0) the ORC dump indexes every cert by RefNo, a second cert under one sail number too")
import json  # noqa: E402
_club = dict(HOOL, RefNo="045600052L9", C_Type="CLUB")
fleetimport._get = lambda url: json.dumps({"rms": [HOOL, _club]}).encode()
fleetimport._orc_cache.clear()
_idx = fleetimport._orc_dump("USA")
check("both certs by RefNo", set(_idx["by_ref"]) == {"045600052L8", "045600052L9"})
_found = certboat.lookup("045600052L9", country="USA")
check("the second cert is found by its RefNo", _found["ok"] and _found["rec"]["C_Type"] == "CLUB")
fleetimport._orc_cache.clear()

fleetimport._orc_dump = _dump
fleetimport._get = lambda url: (_ for _ in ()).throw(AssertionError("network: " + url))

client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}


def post(**body):
    return client.post("/api/boats/from-orc", json=body, headers=TEAM)


print("1) parsing what the owner pastes")
P = certboat.parse_cert
check("cert link → its RefNo, nothing to fetch",
      P("https://sas.orc.org/sasapi/cc/045600052L8") == {"ref": "045600052L8", "sail": None})
check("lower-case link works", P("sas.orc.org/sasapi/cc/045600052l8")["ref"] == "045600052L8")
check("bare RefNo", P(" 045600052L8 ")["ref"] == "045600052L8")
check("sail number", P("USA 52521") == {"ref": None, "sail": "USA 52521"})
for bad in ("", "https://sas.orc.org/sasapi/cc/"):
    try:
        P(bad)
        check(f"refused: {bad!r}", False)
    except ValueError:
        check(f"refused: {bad!r}", True)

print("2) lookup: link / RefNo / sail; countries searched")
r = post(cert="https://sas.orc.org/sasapi/cc/045600052L8")
j = r.json()
check("link: found Hooligan", r.status_code == 200 and j["preview"]["ref_no"] == "045600052L8")
check("RefNo with no country: USA tried, and said", j["country"] == "USA" and j["searched"] == ["USA"])
pv = j["preview"]
check("preview facts", pv["name"] == "Hooligan" and pv["sail_number"] == "USA 52521"
      and pv["boat_class"] == "TP 52" and pv["draft_m"] == 3.2 and pv["gph"] == 438.9
      and pv["issue_date"] == "2026-11-12" and pv["polar_rows"] > 0)
check("preview stores nothing", "saved" not in j and not boats.get_boat("hooligan"))
check("sail number found via its prefix", post(cert="USA 52521").json()["preview"]["ref_no"]
      == "045600052L8")
calls.clear()
j = post(cert="GBR 1").json()
check("sail prefix picks the country", j["country"] == "GBR" and calls == ["GBR"])
j = post(cert="034500011AB", country="gbr").json()
check("RefNo + typed country", j.get("country") == "GBR" and j["preview"]["name"] == "Rule Britannia")
r = post(cert="034500011AB")
check("RefNo in another country, none typed: not found, searched said",
      r.status_code == 404 and r.json()["detail"] == certboat.NOT_FOUND
      and r.json()["searched"] == ["USA"])
check("active boat's sail prefix is tried before USA",
      certboat.countries_to_search({"ref": "x", "sail": None}, active={"sail_number": "GBR 9"})
      == ["GBR", "USA"])
check("race's rating countries tried",
      certboat.countries_to_search({"ref": "x", "sail": None},
                                   definition={"rules_profile": {"scoring": {
                                       "rating_countries": ["CAN"]}}}) == ["CAN", "USA"])
r = post(cert="USA 99999")
check("unknown sail: the plain message", r.status_code == 404
      and r.json()["detail"] == certboat.NOT_FOUND)

print("3) create")
r = post(cert="045600052L8", save=True)
j = r.json()
check("saved as a new boat", r.status_code == 200 and j["saved"] and j["boat_id"] == "hooligan"
      and not j["updated"])
b = boats.get_boat("hooligan")
check("profile: name, sail, class, draft from the cert", b and b["name"] == "Hooligan"
      and b["sail_number"] == "USA 52521" and b["boat_class"] == "TP 52" and b["draft_m"] == 3.2)
check("profile says where its polar came from", b["provenance"]["cert"]["ref_no"] == "045600052L8"
      and b["provenance"]["cert"]["issue_date"] == "2026-11-12" and b["orc"]["gph"] == 438.9
      and "045600052L8" in b["note"])
check("profile valid, no shape errors", boat_profile.validate(b)[0] == []
      and boat_profile.shape_errors(b) == [])
check("no own-boat files: polar is the cert", "polar_files" not in b and b["cert_polar"])
pol = boats.polar_for(b)
check("polar_for gives its cert polar", pol and pol["source"] == "cert"
      and pol["boat_id"] == "hooligan" and len(pol["envelope"]) == len(b["cert_polar"])
      and not BP.check(pol, "hooligan"))
lib = client.get("/api/boats/library", headers=TEAM).json()
lb = {x["boat_id"]: x for x in lib["boats"]}
check("in the boat library the Pi pulls, with its polar",
      "hooligan" in lb and lb["hooligan"]["polar"]["source"] == "cert")
check("listed in the picker", "hooligan" in {x["boat_id"] for x in
                                             client.get("/api/boats", headers=TEAM).json()["boats"]})

print("4) never over a different boat; same RefNo updates")
r = post(cert="USA 777", save=True)
j = r.json()
check("same name, other boat → its own id", j["boat_id"] == "hooligan-usa-777"
      and not j["updated"] and boats.get_boat("hooligan")["sail_number"] == "USA 52521")
b = boats.get_boat("hooligan")
b["draft_m"] = 3.0                         # owner's edits stand
b["helm_factor"] = 0.9
b["cert_polar"] = [[10.0, 90.0, 1.0]]
boats.save_boat(b)
HOOL["Allowances"]["R90"] = [480, 380, 330]
r = post(cert="https://sas.orc.org/sasapi/cc/045600052L8", save=True)
j = r.json()
b = boats.get_boat("hooligan")
check("same RefNo → that profile updated, and said", j["boat_id"] == "hooligan" and j["updated"]
      and "updated" in j["note"])
check("its cert polar refreshed", len(b["cert_polar"]) > 1
      and [10.0, 90.0, round(3600 / 380, 3)] in b["cert_polar"])
check("owner's edits kept", b["draft_m"] == 3.0 and b["helm_factor"] == 0.9)
check("no third Hooligan", sorted(x["boat_id"] for x in boats.list_boats()
                                  if x["boat_id"].startswith("hooligan"))
      == ["hooligan", "hooligan-usa-777"])

print("5) a cert-only boat routes: the active boat's polar block")
boats.labstate.set(boats.ACTIVE_KEY, "hooligan")
check("active polar = the cert", boats.active_polar()["boat_id"] == "hooligan")
r = client.get("/api/crossovers", headers=TEAM)
check("crossovers answer for a cert-only boat", r.status_code == 200
      and r.json()["boat_id"] == "hooligan")

print("6) a long name that's taken still gets a valid id")
_long = {"YachtName": "A" * 70, "SailNo": "USA 1234567890"}
_real_get = certboat.boats.get_boat
certboat.boats.get_boat = lambda bid: {"boat_id": bid} if bid == "a" * 50 else _real_get(bid)
_id = certboat._new_id(_long)
certboat.boats.get_boat = _real_get
check(f"id within 60 characters ({len(_id)})", len(_id) <= 60 and _id != "a" * 50)

shutil.rmtree(_tmp, ignore_errors=True)
print("ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
