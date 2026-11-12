"""Lock-in & Deploy — assemble the race homework and make it deployable onboard.

The Lab's PREP output is the boat's pre-race homework: a signed playbook (Lab-2), the course marks,
the fleet roster, and the iPad checklist subset. This module reads what's already been prepared
(the reviewed RaceDefinition + the frozen playbooks) and reports DEPLOY READINESS per race, then
builds the combined "homework package" the crew loads onto the Pi engine (:8200 `/course/load`,
`/fleet/load`, `/playbook/load`). The Pi passes the playbook on to the Orin copilot and approves it;
nothing writes the copilot's playbook file directly.

The Lab is cloud-only with no line to the boat, so deploy is artifact-download + copy-paste load
commands — crew in the loop, frozen at the gun (RRS 41). An automated Tailscale push can layer on
later. Lock-in state (which frozen playbook is the chosen homework for a race) persists in labstate
under `deploy:<race_id>`.
"""
import json
import os
import threading
import time

from shared import race_def

from . import store, pbstore, labstate, orinassets


def _lock_key(race_id) -> str:
    return "deploy:" + str(race_id)


def targets() -> dict:
    """Where the homework gets loaded, for the manual-load commands: the ssh target of the boat's Pi
    and the engine address as seen from the Pi itself. Both come from the Lab's env; no host name
    or address lives in the code (#314), so unset shows a placeholder to fill in."""
    return {
        "pi_host": os.environ.get("DEPLOY_PI_HOST", "").strip() or "<pi-host>",
        "pi_engine": os.environ.get("DEPLOY_PI_ENGINE", "http://localhost:8200"),
    }


NO_ENGINE_URL = "DEPLOY_PI_ENGINE_URL is not set in the Lab's .env. Not sent."


def engine_url():
    """The boat's Pi engine as the Lab reaches it (its Tailscale address), from DEPLOY_PI_ENGINE_URL.
    None when unset: no address default in code (#314); callers say so and send nothing."""
    return os.environ.get("DEPLOY_PI_ENGINE_URL", "").strip().rstrip("/") or None


def copilot_url():
    """The Orin copilot through the Pi console's proxy: DEPLOY_COPILOT_URL, else the engine's host
    on the console port (8091/copilot), which is how the boat is laid out. None with neither set."""
    cop = os.environ.get("DEPLOY_COPILOT_URL", "").strip().rstrip("/")
    if cop:
        return cop
    eng = engine_url()
    if not eng:
        return None
    from urllib.parse import urlsplit
    u = urlsplit(eng)
    if not u.hostname:
        return None
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme or 'http'}://{host}:8091/copilot"


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


# ---- frozen at the gun (RRS 41) ------------------------------------------------------------------
# Everything Deploy sends is shore-computed homework. The BOAT is the authority: every load endpoint
# refuses while its race log has a race session running (vps/agent/app/racegate.py). The Lab is best
# effort on top: it sends nothing once the race has started, by the plume's rule (plume.race_start_of:
# our gun, else the first minute of the start date). With no start known it sends, and the boat
# decides.
RACE_STARTED = "Race started. Not sent."
MISSING_MARKS = "Missing positions: {names}. Not sent."
_clock = time.time                    # the real clock (tests swap it)


def race_start(definition):
    """The race's start by the plume's rule, or None when nothing is known."""
    from . import plume
    return plume.race_start_of({"definition": definition or {}})


# ---- the locked playbook must be there and whole (#152) -----------------------------------------
# A lock names a frozen bundle by id. If that file is gone, unreadable or fails its signature, a
# push must not go ahead without it: the boat would keep the previous race's playbook while every
# other step shows green.
PB_MISSING = "Active playbook is missing. Activate one again."
PB_BROKEN = "Active playbook fails its check. Freeze it again."
# A lock (the ACTIVE revision, #249) records the signature it was activated with. The bytes under
# an id never change any more, but a lock from before #249 may name an old `<race>__<start>` file
# that a re-freeze overwrote after it was activated: never push bytes nobody activated.
PB_CHANGED = "Playbook changed since it was activated. Activate it again."
NOT_SENT = " Not sent."
# ... and routed on today's course (#150): the bundle carries the course hash it was routed on
# (race_def.course_hash, signed with it). An SI amendment saved after the freeze changes the hash.
PB_COURSE = "Course changed since this playbook. Freeze a new one."
PB_UNCHECKED = "Frozen before course checks. Freeze again to check it."


def locked_bundle(race_id, lock=None):
    """(bundle, problem) for the race's locked playbook, checked against today's race: (None, None)
    when none is locked, (None, text) when it is missing, damaged or routed on another course."""
    lock = (labstate.get(_lock_key(race_id)) or {}) if lock is None else lock
    pid = lock.get("playbook_id")
    if not pid:
        return None, None
    b = pbstore.get(pid)
    p = bundle_problem(race_id, b, store.get_race(race_id) or {}, lock)
    return (None, p) if p else (b, None)


def bundle_problem(race_id, b, definition, lock=None):
    """Why a locked bundle `b` can't go out beside `definition`, or None. Push passes the exact
    definition it sends, so the check and the send can't see two different reads (#150)."""
    if not isinstance(b, dict) or b.get("race_id") != race_id:
        return PB_MISSING
    from . import synthesis
    if not synthesis.verify_bundle(b):
        return PB_BROKEN
    want_sig = (lock or {}).get("signature")
    if want_sig and not str(b["signature"]["value"]).startswith(want_sig):
        return PB_CHANGED
    want = b.get("course_hash")
    if want and want != race_def.course_hash(definition or {}, b.get("course_id")):
        return PB_COURSE
    from . import routesafety
    return routesafety.refusal(b)        # #242: a route not proven safe never goes to the boat


# One push at a time (#182-10): two at once would interleave their course/assets/playbook posts
# on the boat and leave it with a mix of both. A second push is refused, not queued.
_PUSH_LOCK = threading.Lock()
PUSH_BUSY = "Another push is running. Wait for it to finish, then push again."


def push(race_id):
    """`_push` under the one-at-a-time lock; refused (nothing sent) while another push runs."""
    if not _PUSH_LOCK.acquire(blocking=False):
        return {"race_id": race_id, "ok": False, "refused": True, "busy": True,
                "detail": PUSH_BUSY, "steps": []}
    try:
        return _push(race_id)
    finally:
        _PUSH_LOCK.release()


def _push(race_id):
    """Push the locked homework straight onto the boat — no downloads, no SD card. Course/fleet/
    checklist/watch + the signed playbook → the Pi engine over Tailscale; the engine forwards the
    same playbook to the Orin copilot over the Pi↔Orin cable while it holds its race-log lock (the
    Orin takes a playbook from nowhere else). Each push OVERWRITES the previous load on both boxes (the engine
    clears its trigger/matcher state; the copilot rewrites PLAYBOOK_PATH, and uses it once the Pi
    has approved its hash).
    Returns per-step results ({target, step, ok, detail}); None if the race is unknown.
    Frozen at the gun: the start is checked before EVERY post; once it is reached the remaining
    steps are not sent (they show "Race started. Not sent.")."""
    # ONE read of the lock and its bundle, and the package built from that same selection: the
    # course sent, the course checked and the playbook sent all come from these reads (#150)
    lock = labstate.get(_lock_key(race_id)) or {}
    bundle = pbstore.get(lock["playbook_id"]) if lock.get("playbook_id") else None
    pkg = package(race_id, lock=lock, bundle=bundle)
    if not pkg:
        return None
    sent_def = (pkg.get("course_load") or {}).get("definition")
    rs = race_start(sent_def)
    if rs is not None and _clock() >= rs:
        return {"race_id": race_id, "ok": False, "refused": True, "race_start": int(rs),
                "detail": RACE_STARTED, "steps": []}
    # #151: never send a course (or a playbook routed on one) with marks dropped for no position
    from . import coursecheck
    missing = (coursecheck.missing(sent_def, pkg.get("course_id"))
               or (bundle.get("skipped_marks") if isinstance(bundle, dict) else None))
    if missing:
        return {"race_id": race_id, "ok": False, "refused": True, "steps": [],
                "detail": MISSING_MARKS.format(names=", ".join(map(str, missing)))}
    problem = bundle_problem(race_id, bundle, sent_def, lock) if lock.get("playbook_id") else None
    if problem:                          # before any post: never a push that skips the playbook
        return {"race_id": race_id, "playbook_id": lock.get("playbook_id"), "ok": False,
                "refused": True, "detail": problem + NOT_SENT, "steps": []}
    eng = engine_url()
    if not eng:
        return {"race_id": race_id, "ok": False, "refused": True, "detail": NO_ENGINE_URL,
                "steps": []}
    steps = []
    stopped = []

    def step(target, name, url, body, check=None):
        if stopped or (rs is not None and _clock() >= rs):
            stopped.append(name)
            steps.append({"target": target, "step": name, "ok": False, "not_sent": True,
                          "detail": {"error": RACE_STARTED}})
            return
        ok, resp = _post_json(url, body)
        if ok and check is not None and not check(resp):
            ok = False
        steps.append({"target": target, "step": name, "ok": ok, "detail": resp})

    # /course/load answers 200 with loaded: false when it couldn't save: that's a failed step
    step("pi-engine", "course", f"{eng}/course/load", pkg["course_load"],
         check=lambda r: r.get("loaded") is True)
    # the Orin re-route's chart + boat profile, built for THIS course and carried by the Pi with
    # each re-route request (#139, #140). Right after the course, so the two match.
    # the boat the locked playbook was built with (else DEPLOY_BOAT_ID), never another's profile
    assets, problems = orinassets.build((pkg.get("course_load") or {}).get("definition"),
                                       pkg.get("course_id"), bundle=bundle)
    step("pi-engine", "orin assets", f"{eng}/reroute/assets", assets,
         check=lambda r: r.get("loaded") is True)
    if problems and steps[-1]["ok"]:
        # sent (the boat dropped its older ones) but incomplete: never a green step
        steps[-1]["ok"] = False
        steps[-1]["detail"] = dict(steps[-1]["detail"] or {}, error=problems[0])
    elif not steps[-1]["ok"] and isinstance(steps[-1]["detail"], dict) \
            and not steps[-1]["detail"].get("error"):
        steps[-1]["detail"]["error"] = str(steps[-1]["detail"].get("detail") or "Not loaded.")[:200]
    # a boat that refuses (racing) answers loaded: false — never a green step
    taken = lambda r: r.get("loaded") is not False  # noqa: E731
    step("pi-engine", "fleet", f"{eng}/fleet/load", pkg["fleet_load"], check=taken)
    if (pkg.get("checklist_load") or {}).get("items"):
        step("pi-engine", "checklist", f"{eng}/checklist/load", pkg["checklist_load"], check=taken)
    if pkg.get("watch_load"):
        step("pi-engine", "watch", f"{eng}/watch", pkg["watch_load"], check=taken)
    if bundle:
        step("pi-engine", "playbook", f"{eng}/playbook/load", bundle,
             check=lambda r: r.get("loaded"))
        # the engine forwarded it to the Orin under its race-log lock; its answer is `copilot`
        pb = steps[-1]
        cop = (pb["detail"] or {}).get("copilot") if pb["ok"] else None
        if not isinstance(cop, dict):
            cop = {"loaded": False, "error": (pb["detail"] or {}).get("error")
                   or ("Not sent: the Pi didn't load it." if not pb["ok"]
                       else "The Pi didn't send it to the Orin. Update the Pi.")}
        elif not cop.get("loaded") and not cop.get("error"):
            cop = dict(cop, error=str(cop.get("detail") or "Not loaded.")[:200])
        steps.append({"target": "orin-copilot", "step": "playbook", "ok": bool(cop.get("loaded")),
                      "detail": cop})
    res = {"race_id": race_id, "playbook_id": lock.get("playbook_id"),
           "pushed_playbook": bool(bundle), "ok": bool(steps) and all(s["ok"] for s in steps),
           "steps": steps}
    if stopped:
        res["detail"] = RACE_STARTED
    # the signature of the bundle actually sent — a re-freeze under the same id changes it
    record_push(res, race_name=pkg.get("race_name"),
                signature=((bundle or {}).get("signature") or {}).get("value", "")[:16])
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
    labstate.update({_PUSH_LAST: rec, _PUSH_LAST_OK: rec} if rec["ok"] else {_PUSH_LAST: rec})
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
    a write. Pi engine: GET /playbook (newer engines) or /playbook/track (older), plus /course and
    /reroute/assets (the Orin re-route's chart + boat profile);
    Orin copilot: GET /health through the Pi console's proxy. Each side reports `reachable`;
    unreachable sides carry the error so the UI can fall back to the last push honestly."""
    get = get or _get_json
    eng, cop = engine_url(), copilot_url()
    if not eng or not cop:
        err = "DEPLOY_PI_ENGINE_URL is not set in the Lab's .env."
        return {"checked_at": int(time.time()), "reachable": False,
                "engine": {"reachable": False, "error": err, "playbook": None, "course": None},
                "copilot": {"reachable": False, "error": err, "playbook": None}}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_pb = ex.submit(get, f"{eng}/playbook")
        f_course = ex.submit(get, f"{eng}/course")
        f_assets = ex.submit(get, f"{eng}/reroute/assets")     # the Orin's chart + boat profile
        # the copilot's /health probes the engine (3 s) and Ollama (up to 8 s) before answering
        f_cop = ex.submit(get, f"{cop}/health", 14)
        pb_ok, pb = f_pb.result()
        course_ok, course = f_course.result()
        cop_ok, health = f_cop.result()
        assets_ok, assets = f_assets.result()

    engine = {"reachable": False, "error": None, "playbook": None, "course": None}
    if not pb_ok and pb.get("status") == 404:
        # an engine from before GET /playbook — the route-track endpoint still names the race
        pb_ok, track = get(f"{eng}/playbook/track")
        if not pb_ok:
            pb = track                   # report the fallback's real failure, not the 404
        else:
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

    if assets_ok:            # an engine from before #139 answers 404: nothing shown
        engine["orin_assets"] = {k: assets.get(k) for k in (
            "available", "race_id", "course_id", "chart", "boat_id", "boat_version", "received_at")}

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


def _course_for(d: dict, race_id: str, course_id=None, bundle=False):
    """The course to deploy: the locked playbook's course if one is locked, else the requested/first.
    `bundle` is the locked bundle already read (push), else it is read here. Returns
    (marks, skipped, course_id)."""
    if course_id is None:
        if bundle is False:
            lock = labstate.get(_lock_key(race_id)) or {}
            bundle = pbstore.get(lock.get("playbook_id")) if lock.get("playbook_id") else None
        course_id = bundle.get("course_id") if isinstance(bundle, dict) else None
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
    lock_b, lock_problem = locked_bundle(race_id, lock or {})
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
        "lock_problem": lock_problem,            # Push refuses while this is set
        # a bundle from before #150 can't say what course it was routed on: pushed, with a note
        "lock_note": PB_UNCHECKED if lock_b and not lock_b.get("course_hash") else None,
        "targets": targets(),
        "last_push": last_push(),
    }


def lock_in(race_id, playbook_id):
    """ACTIVATE a frozen revision (#249): record it as this race's deploy homework, the only one
    Push sends. A re-freeze makes a new revision and changes nothing until it is activated here.
    Returns the lock state, or None if the playbook isn't a bundle for this race."""
    b = pbstore.get(playbook_id)
    if not b or b.get("race_id") != race_id:
        return None
    sig = (b.get("signature") or {}).get("value")
    state = {"playbook_id": playbook_id, "course_id": b.get("course_id"),
             "signed": bool(sig), "signature": (sig or "")[:16], "locked_at": int(time.time())}
    labstate.set(_lock_key(race_id), state)
    return state


def package(race_id, course_id=None, lock=None, bundle=False):
    """The combined homework package the crew loads onto the Pi engine: ready-to-POST `course_load`
    and `fleet_load` bodies + the iPad checklist subset. The signed PLAYBOOK is downloaded separately
    (byte-exact, for the Orin) and referenced here by id — embedding it would change its bytes and
    break the signature. None if the race is unknown."""
    d = store.get_race(race_id)
    if not d:
        return None
    marks, skipped, cid = _course_for(d, race_id, course_id, bundle)    # push passes its own reads
    lock = (labstate.get(_lock_key(race_id)) or {}) if lock is None else lock
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
