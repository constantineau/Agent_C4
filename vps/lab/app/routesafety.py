"""Route acceptance for what leaves the Lab (#242): a playbook's routes must be proven safe and
complete before it is frozen, locked in, pushed to the boat or exported as GPX.

Each route carries the optimizer's own verdict (`optimizer.route_safety`: land checked over the
whole route, depths wherever ENC charts them, every mark reached, every port/starboard mark and
island left on its side). Legs through water no ENC chart covers (Canada) are listed as notes
("No depth data: check the paper chart"), never refused.
A playbook bundle gets one verdict over every route it carries (the main route, the options and the
plays with a route) and the chart overrides it was routed with. This module reads that verdict and
records the overrides: a reviewed, per-mark exception for a chart artefact (a mark the chart puts
on land or in shallow water). An override only opens the chart at that mark (`geo.obstacles`); it
never waives a problem.
"""
from __future__ import annotations

import time

OLD_BUNDLE = "Built before route checks. Build the playbook again."
REFUSED = "Route not proven safe: {why}"


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
    problems, notes = [], {}
    for label, s in _route_entries(bundle):
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
            "overrides": list(overrides or [])}


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
    texts = []
    for p in (s.get("problems") or []) + again["problems"]:
        t = p.get("text") or p.get("code") or "?"
        if t not in texts:
            texts.append(t)
    return REFUSED.format(why=" ".join(texts) or "not checked.")


def set_override(definition, course_id, mark, by, why, now=None):
    """Record (or with an empty `why`, remove) a reviewed chart override on nav mark `mark` of the
    course. Returns (definition, error). The mark must be one of the course's nav marks."""
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
    why = (why or "").strip()
    if not why:
        ov.pop(mark, None)
    else:
        by = (by or "").strip()
        if not by:
            return definition, "Say who reviewed it."
        ov[mark] = {"by": by[:60], "at": int(now if now is not None else time.time()),
                    "why": why[:300]}
    if ov:
        course["chart_overrides"] = ov
    else:
        course.pop("chart_overrides", None)
    return definition, None
