"""Route acceptance for what leaves the Lab (#242): a playbook's routes must be proven safe and
complete before it is frozen, locked in, pushed to the boat or exported as GPX.

Each route carries the optimizer's own verdict (`optimizer.route_safety`: land checked over the
whole route, depths wherever ENC charts them, every mark reached, every port/starboard mark and
island left on its side). Legs through water no chart covers are listed as notes, never refused:
"No depth data: check the paper chart", or, where a depth grid that is not a chart gave the depth
(`geo.depthsrc`, #292), which grid it was.
A playbook bundle gets one verdict over every route it carries (the main route, the options and the
plays with a route) and the chart overrides it was routed with. This module reads that verdict and
records the overrides: a per-mark exception for a chart artefact (a mark the chart puts
on land or in shallow water). An override only opens the chart at that mark (`geo.obstacles`); it
never waives a problem.

Every verdict also carries `checks` (#405): one line per safety check, each with its own state, so
no route reads as "clear" when a check didn't run. The checks only report; what is refused is still
decided by `ok`/`problems` above, unchanged.
"""
from __future__ import annotations

import time

OLD_BUNDLE = "Built before route checks. Build the playbook again."
REFUSED = "Route not proven safe: {why} {fix}"
# what to click (#325), after the problems: a frozen playbook is fixed in Gameplan, then rebuilt
FIX_OVERRIDE = "Chart wrong at {marks}? Click Override there in Gameplan, then build the playbook again."
FIX_MOVE = "Move {marks} in Course & Marks, then build the playbook again."
FIX_REBUILD = "Build the playbook again in Gameplan."


# ---- per-check states (#405) -------------------------------------------------------------------
CHECKED, NOT_CHECKED, FAILED = "checked", "not_checked", "failed"
# (id, label) in the order the Lab and the iPad list them
CHECKS = (("land", "Land"), ("depth", "Depth"), ("zones", "Race zones"),
          ("marks", "Marks and sides"), ("chart", "Chart covers route"))
_RANK = {CHECKED: 0, NOT_CHECKED: 1, FAILED: 2}
NOT_RUN = "Not checked."
LAND_SOME_LEGS = "Land not checked on every leg."
LAND_OFF_CHART = "Land not checked off the chart."
# which optimizer problem code (optimizer.route_safety) belongs to which check, and how it reads
_PROBLEM_CHECK = {"land": ("land", NOT_CHECKED), "depth": ("depth", NOT_CHECKED),
                  "box": ("chart", FAILED), "blocked": ("marks", FAILED),
                  "unreached": ("marks", FAILED), "side": ("marks", FAILED)}


def _check(cid, state, texts=()):
    label = dict(CHECKS)[cid]
    return {"id": cid, "label": label, "state": state, "texts": [t for t in texts if t]}


def route_checks(problems, notes, has_fields, land_everywhere=True):
    """[{id, label, state, texts}] for one routed course, from the optimizer's own problems and
    notes. `has_fields`: was any obstacle map built at all (no map: nothing but marks was checked).
    A leg through water no chart gives a depth for makes depth "not checked" (it is a note, never a
    refusal); a depth grid that is not a chart still counts as checked, with its note.
    `land_everywhere`: did every leg's map have land on it? One leg without is land not checked
    (the refusal only fires when none has it, unchanged)."""
    got = {cid: [CHECKED, []] for cid, _l in CHECKS}
    if not has_fields:
        for cid in ("land", "depth", "zones", "chart"):
            got[cid][0] = NOT_CHECKED
    elif not land_everywhere:
        got["land"] = [NOT_CHECKED, [LAND_SOME_LEGS]]
    for p in problems or []:
        cid, state = _PROBLEM_CHECK.get(p.get("code"), (None, None))
        if cid is None:
            continue
        if _RANK[state] > _RANK[got[cid][0]]:
            got[cid][0] = state
        got[cid][1].append(p.get("text"))
    for n in notes or []:
        code = str(n.get("code") or "")
        if code == "no_depth":
            if got["depth"][0] == CHECKED:
                got["depth"][0] = NOT_CHECKED
            legs = ", ".join(n.get("legs") or [])
            got["depth"][1].append((n.get("text") or "") + (" " + legs if legs else ""))
        elif code.startswith("grid_depth:"):
            got["depth"][1].append(n.get("text"))
    if got["chart"][0] == FAILED and got["land"][0] == CHECKED:
        # off the map's box nothing was checked, land included
        got["land"] = [NOT_CHECKED, [LAND_OFF_CHART]]
    return [_check(cid, st, texts) for cid, (st, texts) in got.items()]


def merge_checks(*lists):
    """One list over several routes (or a route plus what the boat knows): each check takes its
    worst state and every text once. A route with no `checks` (built before #405) counts as every
    check not run, and so does any check a list leaves out."""
    got = {cid: [CHECKED, []] for cid, _l in CHECKS}
    for lst in lists:
        if not isinstance(lst, list):
            lst = [_check(cid, NOT_CHECKED, [NOT_RUN]) for cid, _l in CHECKS]
        seen = set()
        for c in lst:
            if not isinstance(c, dict) or c.get("id") not in got:
                continue
            seen.add(c["id"])
            g = got[c["id"]]
            st = c.get("state") if c.get("state") in _RANK else NOT_CHECKED
            if _RANK[st] > _RANK[g[0]]:
                g[0] = st
            g[1] += [t for t in c.get("texts") or [] if t and t not in g[1]]
        for cid in got.keys() - seen:          # a check this list doesn't name didn't run
            got[cid][0] = FAILED if got[cid][0] == FAILED else NOT_CHECKED
            if NOT_RUN not in got[cid][1]:
                got[cid][1].append(NOT_RUN)
    return [_check(cid, st, texts) for cid, (st, texts) in got.items()]


def all_checked(checks):
    """True only when every check ran and passed."""
    ids = {c.get("id"): c.get("state") for c in checks or [] if isinstance(c, dict)}
    return all(ids.get(cid) == CHECKED for cid, _l in CHECKS)


def _route_entries(bundle):
    """(label, safety) for every route the bundle carries."""
    out = [("Main route", (bundle.get("consensus") or {}).get("safety", None))]
    for v in bundle.get("variants") or []:
        if isinstance(v, dict) and v.get("route"):
            out.append((v.get("name") or v.get("id") or "Option", v.get("safety")))
    for p in bundle.get("plays") or []:
        if isinstance(p, dict) and (p.get("response") or {}).get("route"):
            out.append((p.get("name") or p.get("id") or "Play", p.get("safety")))
    return out


def bundle_verdict(bundle, overrides=None):
    """{ok, problems: [{route, code, mark, text}], notes: [{code, text, legs}], overrides} over
    every route in `bundle`. A route with no verdict at all counts as not proven. `notes` never
    fail it: the legs through water no ENC chart gives a depth for (#242), once each over all
    the routes."""
    problems, notes, checks = [], {}, []
    for label, s in _route_entries(bundle):
        checks.append(s.get("checks") if isinstance(s, dict) else None)
        if not isinstance(s, dict) or (s.get("ok") is not True and not s.get("problems")):
            problems.append({"route": label, "code": "unchecked", "mark": None,
                             "text": "Not checked."})
            continue
        for p in s.get("problems") or []:
            problems.append(dict(p, route=label))
        for n in s.get("notes") or []:
            if isinstance(n, dict) and n.get("code"):
                got = notes.setdefault(n["code"], {"code": n["code"], "text": n.get("text"),
                                                   "legs": []})
                got["legs"] += [lg for lg in n.get("legs") or [] if lg not in got["legs"]]
    return {"ok": not problems, "problems": problems, "notes": list(notes.values()),
            "checks": merge_checks(*checks), "overrides": list(overrides or [])}


def refusal(bundle):
    """Why this bundle may not be frozen, deployed or exported, in the crew's words, or None."""
    if not isinstance(bundle, dict):
        return OLD_BUNDLE
    s = bundle.get("safety")
    if not isinstance(s, dict) or "ok" not in s:
        return OLD_BUNDLE
    # never trust the top-level verdict alone (a draft comes back from the browser): every route's
    # own verdict must pass too
    again = bundle_verdict(bundle)
    if s.get("ok") and not s.get("problems") and again["ok"]:
        return None
    texts, ov, move = [], [], []
    for p in (s.get("problems") or []) + again["problems"]:
        t = p.get("text") or p.get("code") or "?"
        if t not in texts:
            texts.append(t)
        m = p.get("mark")
        if p.get("code") == "blocked" and m:
            # a verdict from before #325 has no `override`: the chart was all a block could be
            got = ov if p.get("override", True) else move
            if m not in got:
                got.append(m)
    fix = " ".join(([FIX_OVERRIDE.format(marks=", ".join(ov))] if ov else []) +
                   ([FIX_MOVE.format(marks=", ".join(move))] if move else [])) or FIX_REBUILD
    return REFUSED.format(why=" ".join(texts) or "not checked.", fix=fix)


def set_override(definition, course_id, mark, by=None, why=None, now=None, remove=False):
    """Record (or with `remove`, remove) a chart override on nav mark `mark` of the course.
    Returns (definition, error). The mark must be one of the course's nav marks. One click in the
    Lab (#325): `by` defaults to "Lab" (one team login, no names) and `why` is optional."""
    from shared import race_def
    courses = (definition or {}).get("courses") or []
    course = next((c for c in courses if c.get("id") == course_id), None)
    if course is None and course_id:
        # a course named but not in the race (a stale Lab page): never another course's chart
        return definition, "No such course."
    course = course or (courses[0] if courses else None)
    if not course:
        return definition, "No course."
    names = [n for _s, n, _a, _o in race_def.course_to_marks(definition, course.get("id"))[0]]
    if mark not in names:
        return definition, "No such mark."
    ov = course.get("chart_overrides")
    ov = dict(ov) if isinstance(ov, dict) else {}
    if remove:
        ov.pop(mark, None)
    else:
        ov[mark] = {"by": (str(by or "").strip() or "Lab")[:60],
                    "at": int(now if now is not None else time.time()),
                    "why": str(why or "").strip()[:300]}
    if ov:
        course["chart_overrides"] = ov
    else:
        course.pop("chart_overrides", None)
    return definition, None
