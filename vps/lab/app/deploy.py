"""Lock-in & Deploy — assemble the race homework and make it deployable onboard.

The Lab's PREP output is the boat's pre-race homework: a signed playbook (Lab-2), the course marks,
the fleet roster, and the iPad checklist subset. This module reads what's already been prepared
(the reviewed RaceDefinition + the frozen playbooks) and reports DEPLOY READINESS per race, then
builds the combined "homework package" the crew loads onto the Pi engine (:8200 `/course/load` +
`/fleet/load`) and the Orin copilot (`PLAYBOOK_PATH`).

The Lab is cloud-only with no line to the boat, so deploy is artifact-download + copy-paste load
commands — crew in the loop, frozen at the gun (RRS 41). An automated Tailscale push can layer on
later. Lock-in state (which frozen playbook is the chosen homework for a race) persists in labstate
under `deploy:<race_id>`.
"""
import json
import os
import time

from shared import race_def

from . import store, pbstore, labstate


def _lock_key(race_id) -> str:
    return "deploy:" + str(race_id)


def targets() -> dict:
    """Where the homework gets loaded — the onboard service addresses (env-overridable). Hostnames
    are the Tailscale names (see the Orin/Pi deployment docs)."""
    return {
        "pi_host": os.environ.get("DEPLOY_PI_HOST", "sr33-pi"),
        "pi_engine": os.environ.get("DEPLOY_PI_ENGINE", "http://localhost:8200"),
        "orin_host": os.environ.get("DEPLOY_ORIN_HOST", "agent-c4"),
        "orin_playbook_path": os.environ.get("DEPLOY_ORIN_PLAYBOOK_PATH",
                                             "/home/agent-c4/sr33/playbook.json"),
        "orin_service": os.environ.get("DEPLOY_ORIN_SERVICE", "sr33-orin-copilot"),
    }


def _post_json(url, body, timeout=25):
    """POST a JSON body; (ok, response-dict). Never raises — push reports per-step outcomes."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read() or b"{}")
    except Exception as exc:
        return False, {"error": str(exc)}


def push(race_id):
    """Push the locked homework straight onto the boat — no downloads, no SD card. Course/fleet/
    checklist/watch + the signed playbook → the Pi engine over Tailscale; the same playbook → the
    Orin copilot via the Pi console's /copilot proxy (rides the Pi↔Orin ethernet crossover, so no
    Orin-Tailscale dependency). Each push OVERWRITES the previous load on both boxes (the engine
    clears its trigger/matcher state; the copilot rewrites PLAYBOOK_PATH and re-reads per request).
    Returns per-step results ({target, step, ok, detail}); None if the race is unknown."""
    pkg = package(race_id)
    if not pkg:
        return None
    lock = labstate.get(_lock_key(race_id)) or {}
    bundle = pbstore.get(lock.get("playbook_id")) if lock.get("playbook_id") else None
    eng = os.environ.get("DEPLOY_PI_ENGINE_URL", "http://100.79.180.102:8200").rstrip("/")
    cop = os.environ.get("DEPLOY_COPILOT_URL", "http://100.79.180.102:8091/copilot").rstrip("/")
    steps = []

    def step(target, name, url, body, check=None):
        ok, resp = _post_json(url, body)
        if ok and check is not None and not check(resp):
            ok = False
        steps.append({"target": target, "step": name, "ok": ok, "detail": resp})

    step("pi-engine", "course", f"{eng}/course/load", pkg["course_load"])
    step("pi-engine", "fleet", f"{eng}/fleet/load", pkg["fleet_load"])
    if (pkg.get("checklist_load") or {}).get("items"):
        step("pi-engine", "checklist", f"{eng}/checklist/load", pkg["checklist_load"])
    if pkg.get("watch_load"):
        step("pi-engine", "watch", f"{eng}/watch", pkg["watch_load"])
    if bundle:
        step("pi-engine", "playbook", f"{eng}/playbook/load", bundle,
             check=lambda r: r.get("loaded"))
        step("orin-copilot", "playbook", f"{cop}/playbook/load", bundle,
             check=lambda r: r.get("loaded"))
    res = {"race_id": race_id, "playbook_id": lock.get("playbook_id"),
           "pushed_playbook": bool(bundle), "ok": bool(steps) and all(s["ok"] for s in steps),
           "steps": steps}
    record_push(res, race_name=pkg.get("race_name"), signature=lock.get("signature"))
    return res


# ---- what's on the boat ------------------------------------------------------------------------
# The boat holds whatever was pushed LAST, whichever race it was for — so the record is boat-wide,
# not per race. `last` is the latest attempt (a failed one may have half-loaded the boat);
# `last_ok` survives a later failed attempt so "the boat is unreachable" never erases what we know.
_PUSH_LAST = "deploy_push:last"
_PUSH_LAST_OK = "deploy_push:last_ok"


def _trim_detail(detail):
    """A step's response, cut to its scalar fields (no echoed bundles) for persisting."""
    out = {}
    for k, v in (detail or {}).items():
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:200]
        elif isinstance(v, list) and k == "skipped":
            out[k] = [str(x)[:60] for x in v[:10]]
    return out


def record_push(res, race_name=None, signature=None, now=None):
    """Persist a push result (when, which race + playbook, per-step ok) so the Deploy tab can say
    what went to the boat after a reload. Returns the stored record."""
    rec = {"at": int(now if now is not None else time.time()),
           "race_id": res.get("race_id"), "race_name": race_name,
           "playbook_id": res.get("playbook_id") if res.get("pushed_playbook") else None,
           "signature": (signature or "") if res.get("pushed_playbook") else "",
           "ok": bool(res.get("ok")),
           "steps": [{"target": s.get("target"), "step": s.get("step"), "ok": bool(s.get("ok")),
                      "detail": _trim_detail(s.get("detail"))} for s in res.get("steps") or []]}
    labstate.set(_PUSH_LAST, rec)
    if rec["ok"]:
        labstate.set(_PUSH_LAST_OK, rec)
    return rec


def last_push() -> dict:
    """{last, last_ok} — the latest push attempt and the latest fully successful one (either may be
    None; they are the same record when the latest attempt succeeded)."""
    return {"last": labstate.get(_PUSH_LAST), "last_ok": labstate.get(_PUSH_LAST_OK)}


def _get_json(url, timeout=4):
    """GET JSON; (ok, dict). Never raises — an unreachable boat is a normal answer here."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
        return True, data if isinstance(data, dict) else {"value": data}
    except urllib.error.HTTPError as exc:
        return False, {"error": f"HTTP {exc.code}", "status": exc.code}
    except Exception as exc:
        return False, {"error": str(exc)[:200]}


def boat_status(get=None):
    """What the boat has loaded RIGHT NOW, read over the Lab's Tailscale line — read-only GETs, never
    a write. Pi engine: GET /playbook (newer engines) or /playbook/track (older), plus /course;
    Orin copilot: GET /health through the Pi console's proxy. Each side reports `reachable`;
    unreachable sides carry the error so the UI can fall back to the last push honestly."""
    get = get or _get_json
    eng = os.environ.get("DEPLOY_PI_ENGINE_URL", "http://100.79.180.102:8200").rstrip("/")
    cop = os.environ.get("DEPLOY_COPILOT_URL", "http://100.79.180.102:8091/copilot").rstrip("/")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_pb = ex.submit(get, f"{eng}/playbook")
        f_course = ex.submit(get, f"{eng}/course")
        f_cop = ex.submit(get, f"{cop}/health")
        pb_ok, pb = f_pb.result()
        course_ok, course = f_course.result()
        cop_ok, health = f_cop.result()

    engine = {"reachable": False, "error": None, "playbook": None, "course": None}
    if not pb_ok and pb.get("status") == 404:
        # an engine from before GET /playbook — the route-track endpoint still names the race
        pb_ok, track = get(f"{eng}/playbook/track")
        if pb_ok:
            pb = ({"loaded": True, "race_id": track.get("race_id"),
                   "recommended": track.get("recommended"), "partial": True}
                  if track.get("available") or track.get("race_id") else
                  {"loaded": None, "partial": True, "note": track.get("note")})
    if pb_ok or course_ok:
        engine["reachable"] = True
    else:
        engine["error"] = pb.get("error") or course.get("error")
    if pb_ok:
        engine["playbook"] = {k: pb.get(k) for k in (
            "loaded", "race_id", "id", "start_epoch", "generated_at", "recommended", "variants",
            "plays", "signed", "signature", "partial", "note") if k in pb}
    if course_ok:
        engine["course"] = {"available": bool(course.get("available")),
                            "route": course.get("route"),
                            "marks": len(course.get("marks") or []),
                            "mark_names": [m.get("name") for m in (course.get("marks") or [])][:12]}

    copilot = {"reachable": bool(cop_ok), "error": None if cop_ok else health.get("error"),
               "playbook": None}
    if cop_ok:
        p = health.get("playbook") or {}
        copilot["playbook"] = {"loaded": bool(p.get("loaded")), "race_id": p.get("race_id"),
                               "variants": p.get("variants"), "signed": p.get("signed"),
                               "signature_ok": p.get("signature_ok")}
        copilot["mode"] = health.get("mode")
    return {"checked_at": int(time.time()), "engine": engine, "copilot": copilot,
            "reachable": engine["reachable"] or copilot["reachable"]}


def _course_for(d: dict, race_id: str, course_id=None):
    """The course to deploy: the locked playbook's course if one is locked, else the requested/first.
    Returns (marks, skipped, course_id)."""
    if course_id is None:
        lock = labstate.get(_lock_key(race_id)) or {}
        pb = pbstore.get(lock.get("playbook_id")) if lock.get("playbook_id") else None
        course_id = (pb or {}).get("course_id")
    return race_def.course_to_marks(d, course_id)


def readiness(race_id, course_id=None):
    """Per-race deploy readiness: the four homework components + their status, the frozen playbooks
    for this race, the locked-in selection, and the deploy targets. None if the race is unknown."""
    d = store.get_race(race_id)
    if not d:
        return None
    marks, skipped, cid = _course_for(d, race_id, course_id)
    blob = race_def.fleet_blob(d)
    roster = blob.get("fleet", []) or []
    reqs = d.get("requirements", []) or []
    ipad = [r for r in reqs if r.get("deliver_to_ipad")]
    pbs = [b for b in pbstore.list_bundles() if b.get("race_id") == race_id]

    lock = labstate.get(_lock_key(race_id)) or None
    if lock and not any(b["id"] == lock.get("playbook_id") for b in pbs):
        lock = None                              # the locked bundle was deleted/superseded

    return {
        "race_id": race_id,
        "race_name": d.get("name"),
        "reviewed": bool(d.get("reviewed")),
        "course": {"course_id": cid, "marks": len(marks), "skipped": skipped,
                   "ready": len(marks) >= 2 and not skipped},
        "fleet": {"roster": len(roster),
                  "scoring": (blob.get("scoring") or {}).get("method", ""),
                  "tracker_permitted": bool((blob.get("tracker") or {}).get("permitted")),
                  "ready": len(roster) > 0},
        "checklists": {"total": len(reqs), "ipad": len(ipad), "ready": len(reqs) > 0},
        "watch": {"blocks": len((d.get("watch_plan") or {}).get("blocks") or []),
                  "ready": bool((d.get("watch_plan") or {}).get("blocks"))},
        "playbooks": pbs,
        "lock_in": lock,
        "targets": targets(),
        "last_push": last_push(),
    }


def lock_in(race_id, playbook_id):
    """Record the chosen frozen playbook as this race's deploy homework. Returns the lock state, or
    None if the playbook isn't a bundle for this race."""
    b = pbstore.get(playbook_id)
    if not b or b.get("race_id") != race_id:
        return None
    sig = (b.get("signature") or {}).get("value")
    state = {"playbook_id": playbook_id, "course_id": b.get("course_id"),
             "signed": bool(sig), "signature": (sig or "")[:16], "locked_at": int(time.time())}
    labstate.set(_lock_key(race_id), state)
    return state


def package(race_id, course_id=None):
    """The combined homework package the crew loads onto the Pi engine: ready-to-POST `course_load`
    and `fleet_load` bodies + the iPad checklist subset. The signed PLAYBOOK is downloaded separately
    (byte-exact, for the Orin) and referenced here by id — embedding it would change its bytes and
    break the signature. None if the race is unknown."""
    d = store.get_race(race_id)
    if not d:
        return None
    marks, skipped, cid = _course_for(d, race_id, course_id)
    lock = labstate.get(_lock_key(race_id)) or {}
    ipad = [r for r in (d.get("requirements") or []) if r.get("deliver_to_ipad")]
    return {
        "schema": "c4.homework/v1",
        "race_id": race_id,
        "race_name": d.get("name"),
        "course_id": cid,
        "generated_at": int(time.time()),
        "course_load": {"definition": d, "course_id": cid},   # → POST /course/load (Pi engine :8200)
        "fleet_load": {"definition": d},                      # → POST /fleet/load (Pi engine :8200)
        # → POST /checklist/load (Pi engine :8200) — the in-race requirement reminders (nav
        # lights at sunset, the gate photo, the finish procedure) the console + coach surface
        "checklist_load": {"items": ipad, "race_id": race_id},
        # → POST /watch (Pi engine :8200); None when no plan authored — the boat can still
        # author one from the iPad, this just seeds it
        "watch_load": ({"plan": d["watch_plan"]} if (d.get("watch_plan") or {}).get("blocks")
                       else None),
        "skipped_marks": skipped,
        "playbook_id": lock.get("playbook_id"),
        "playbook_signature": lock.get("signature"),
    }
