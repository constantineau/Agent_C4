"""#320 — practice on any boat with no Lab prep: the boat library aboard + the session's boat.

The Pi keeps every boat profile and its polar (synced from the Lab, kept on the engine volume). The
engine's polar, targets, sail dial and the Orin re-route request follow the CURRENT boat: the open
LOG session's, else the last one used, else this kit's own. Pinned, with sr33, bravo and an
"Other boat":
  * library store: good entries kept, a bad one dropped by name, a race refuses it;
  * nothing pushed, library synced → the kit's boat (sr33) has its polar, no Deploy needed;
  * a Practice LOG on bravo → bravo's polar at once (matcher too), no sr33 file opened, the
    session row names bravo; ending the log keeps bravo (last used);
  * an "Other boat" → no polar: the sail dial says "No polar for this boat.", the Orin request
    carries nulls and names the boat (so the Orin refuses, never routes on its own files);
  * a boat the library doesn't know → recorded by its name, never refused;
  * push vs library: the newer copy of the same boat wins; an explicit "no polar" push gives way
    to the library; a push for another boat is never used for this one;
  * the sync: off without LAB_URL, skipped while racing (no shore call mid-race).
"""
import builtins
import io
import json
import os
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["DATA_SOURCE"] = "onboard"
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine.db")
os.environ["ARCHIVE_DB"] = os.path.join(_tmp, "archive.db")
os.environ["ONBOARD_LIVE_WS"] = "false"
os.environ.pop("POLARS_FILE", None)
os.environ.pop("LAB_URL", None)
os.environ["BOAT_ID"] = "sr33"           # this kit is sr33's
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

from shared import boat_polar as BP, boat_profile  # noqa: E402
from app import boatlib, datasource, matcher, orinassets, racelog, sails  # noqa: E402
from app import datasource_onboard as DSO  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def profile(bid):
    with open(os.path.join(REPO, "vps", "lab", "boats", f"{bid}.json")) as f:
        return json.load(f)


DIRS = (os.path.join(REPO, "vps", "db", "seed"), os.path.join(REPO, "vps", "agent", "knowledge"))
SR33, BRAVO = profile("sr33"), profile("bravo")
P33, PB = BP.resolve(SR33, dirs=DIRS), BP.resolve(BRAVO, dirs=DIRS)
SR33_FILES = set(SR33["polar_files"].values())


def entry(p, pol):
    return {"boat_id": p["boat_id"], "name": p["name"], "profile": p,
            "boat_version": boat_profile.version(p), "polar": pol}


def library(*entries):
    return {"schema": boatlib.SCHEMA, "built_at": 1, "boats": list(entries)}


def push(boat, polar, at, **extra):
    body = {"schema": orinassets.SCHEMA, "race_id": "r", "course_id": "c", "chart": None,
            "boat_id": boat["boat_id"], "boat": boat, "boat_version": boat_profile.version(boat),
            "polar": polar}
    body.update(extra)
    assert orinassets.validate(body)[0]
    orinassets._write(json.dumps({"received_at": at, "assets": body}))


def unpush():
    try:
        os.remove(orinassets._path())
    except OSError:
        pass


def rows_of(block):
    return [(float(r[0]), float(r[1]), float(r[2])) for r in block["envelope"]]


ds = datasource.active()
racelog.deviation._load_playbook = lambda: None

print("1) the library store keeps good entries, drops a bad one by name")
bad = entry(BRAVO, dict(PB, label="tampered"))
out = boatlib.store(library(entry(SR33, P33), bad))
check("stored", out["stored"] and out["boats"] == ["sr33"])
check("the tampered polar is dropped and named",
      out["dropped"] and out["dropped"][0]["boat_id"] == "bravo")
check("another boat's polar under a profile is dropped",
      boatlib.entry_errors(entry(BRAVO, P33)) == ["polar is for another boat"])
check("a profile whose version doesn't match is dropped",
      boatlib.entry_errors(dict(entry(BRAVO, PB), boat_version="x")))
check("not a library → refused", boatlib.store({"boats": []})["stored"] is False)
out = boatlib.store(library(entry(SR33, P33), entry(BRAVO, PB)))
check("sr33 + bravo stored", out["stored"] and out["boats"] == ["bravo", "sr33"])
check("it is on the engine volume (survives a restart)",
      os.path.dirname(boatlib._path()) == os.path.dirname(os.environ["ENGINE_DB"]))

print("2) nothing pushed: the kit's boat (sr33) sails on the library's polar, no Deploy needed")
unpush()
cur = boatlib.current_boat()
check("current boat = the kit's (sr33)", cur["boat_id"] == "sr33" and cur["from"] == "kit")
check("polar = sr33's library block", (orinassets.boat_polar() or {}).get("version")
      == P33["version"])
check("% of polar rows = sr33's polar", [(r["tws"], r["twa"], r["target_stw"])
                                        for r in DSO._polar_rows()] == rows_of(P33))
check("the sail dial answers on sr33's guide", sails.get_sail_advice(12, 45, "J1")["available"])

print("3) a Practice LOG on bravo: bravo's polar at once, no sr33 file opened")
t33 = matcher._polar_table()
opened = []
_open, _io = builtins.open, io.open


def spy(file, *a, **k):
    opened.append(os.path.basename(str(file)))
    return _open(file, *a, **k)


builtins.open = io.open = spy
try:
    r = racelog.start(kind="practice", boat_id="bravo")
    cur_b = boatlib.current_boat()
    rows_b = DSO._polar_rows()
    tb = matcher._polar_table()
    adv_b = sails.get_sail_advice(12, 45, "J1")
    req_b = orinassets.for_run({"race_id": "r"}, "c")
finally:
    builtins.open, io.open = _open, _io
check("session started, names bravo", r["ok"] and r["active"]["boat_id"] == "bravo"
      and r["active"]["boat_name"] == "Bravo")
check("current boat = the session's", cur_b["boat_id"] == "bravo" and cur_b["from"] == "session")
check("polar = bravo's cert", [(x["tws"], x["twa"], x["target_stw"]) for x in rows_b]
      == rows_of(PB))
check("the matcher switched at once", tb != t33 and len(tb) == len(BRAVO["cert_polar"]))
check("no sail guide for bravo → the dial says so, not sr33's call", adv_b["available"] is False)
check("the re-route request carries bravo's profile + polar, naming it",
      req_b["boat"]["boat_id"] == "bravo" and req_b["polar"]["version"] == PB["version"]
      and req_b["session_boat"] == {"boat_id": "bravo", "name": "Bravo", "from": "library",
                                    "at": req_b["session_boat"]["at"]})
leaked = sorted(set(opened) & SR33_FILES)
check(f"no sr33 polar file opened (opened: {leaked or 'none'})", not leaked)
check("the recorded rows stay the kit's: the session row alone names the boat",
      ds.sessions_list(1)[0]["boat_id"] == "bravo" and boatlib.kit_id() == "sr33")
racelog.end()
check("log ended: still bravo (the last boat used)",
      boatlib.current_boat()["boat_id"] == "bravo"
      and boatlib.current_boat()["from"] == "last")
r = racelog.start(kind="practice")
check("a LOG with no pick starts on the last boat used", r["active"]["boat_id"] == "bravo")
racelog.end()

print("4) an Other boat: the session records fine, polar numbers say 'No polar for this boat.'")
r = racelog.start(kind="practice", boat_name="  Mystery Ship  ")
check("recorded with the typed name, no boat id",
      r["ok"] and r["active"]["boat_id"] is None and r["active"]["boat_name"] == "Mystery Ship")
check("no polar", orinassets.boat_polar() is None and DSO._polar_rows() == [])
adv = sails.get_sail_advice(12, 45, "J1")
check("sail dial: 'No polar for this boat.'", adv["note"] == BP.NO_POLAR)
req = orinassets.for_run({"race_id": "r"}, "c")
check("the Orin gets nulls + the boat's name (it refuses; never its own files)",
      req["boat"] is None and req["polar"] is None and "session_boat" in req
      and req["session_boat"]["name"] == "Mystery Ship")
check("GET /boats says so", boatlib.view()["current"]["note"] == BP.NO_POLAR)
racelog.end()
r = racelog.start(kind="practice", boat_id="no-such-boat")
check("a boat the library doesn't know: recorded by its name, never refused",
      r["ok"] and r["active"]["boat_id"] is None and r["active"]["boat_name"] == "no-such-boat")
racelog.end()
r = racelog.start(kind="practice", boat_id="sr33")
racelog.end()

print("5) push vs library for the same boat: the newer copy wins; never another boat's")
now = time.time()
lib_at = boatlib.entries()["sr33"]["got_at"]
push(SR33, dict(P33), now + 100)
check("push newer than the library → the push's", boatlib.current_boat()["polar_from"] == "deploy")
push(SR33, dict(P33), lib_at - 100)
check("library newer than the push → the library's",
      boatlib.current_boat()["polar_from"] == "library")
push(SR33, None, now + 200)
check("an explicit 'no polar' push → the library's polar still",
      (orinassets.boat_polar() or {}).get("version") == P33["version"])
r = racelog.start(kind="practice", boat_id="bravo")
push(SR33, dict(P33), now + 300)
check("an sr33 push while sailing bravo: bravo's polar, never sr33's",
      orinassets.boat_polar()["boat_id"] == "bravo")
racelog.end()
check("the newer push names the next boat when no session runs (sr33)",
      boatlib.current_boat()["boat_id"] == "sr33" and boatlib.current_boat()["from"] == "deploy")
check("GET /boats names the deployed playbook's boat", boatlib.view()["playbook_boat"] == "sr33")
unpush()

print("6) the sync: off without LAB_URL, skipped while racing (no shore call mid-race)")
check("no LAB_URL: the sync is off and says so",
      boatlib.start_sync() is False and boatlib.view()["library"]["note"] == boatlib.NOT_SET)
calls = []


def fetcher():
    calls.append(1)
    return library(entry(SR33, P33)), None


out = boatlib.sync_once(fetcher=fetcher)
check("a pull stores what the Lab served", out["stored"] and calls == [1]
      and sorted(boatlib.entries()) == ["sr33"])
got = boatlib.entries()["sr33"]["got_at"]
boatlib.sync_once(now=time.time() + 50, fetcher=fetcher)
check("an unchanged polar keeps when it first arrived", boatlib.entries()["sr33"]["got_at"] == got)
racelog.start(kind="race", boat_id="sr33")
calls.clear()
out = boatlib.sync_once(fetcher=fetcher)
check("racing: no fetch at all", out["stored"] is False and calls == [] and out["note"] == boatlib.RACING)
check("racing: a library can't land either",
      boatlib.store(library(entry(BRAVO, PB)))["stored"] is False)
racelog.end()
racelog.race_over(since=racelog.race_state()["since"])
check("Race over: it syncs again", boatlib.sync_once(fetcher=fetcher)["stored"])

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
