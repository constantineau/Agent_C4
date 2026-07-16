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
    return {"race_id": race_id, "playbook_id": lock.get("playbook_id"),
            "pushed_playbook": bool(bundle), "ok": bool(steps) and all(s["ok"] for s in steps),
            "steps": steps}


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
