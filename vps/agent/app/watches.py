"""Watch system — the crew rotation, live. Source-agnostic (datasource seam): the plan blob
lives in the engine kv onboard / app_state on the cloud; the model + resolver are
shared/watchplan.py (the Lab editor uses the same code, so authored and served plans agree).

Deterministic scheduling on the boat's own computer — no RRS-41 exposure, no gate, and it
works with no playbook, no course and no cloud (same standalone discipline as the race log).
"""
import time

from shared import watchplan
from . import datasource


def get_plan():
    """The stored plan (normalized) or an empty plan if never set."""
    try:
        raw = datasource.active().get_watch_plan() or {}
    except Exception:
        raw = {}
    return watchplan.normalize(raw)


def get_watch(now=None):
    """The CREW tile / matcher / coach read: live status + the plan for the detail view."""
    now = now or time.time()
    plan = get_plan()
    st = watchplan.status_at(plan, now)
    st["plan_set"] = bool(plan["blocks"])
    st["teams"] = plan["teams"]
    st["blocks"] = plan["blocks"]
    st["log"] = (plan.get("log") or [])[-10:]
    st["updated_at"] = plan.get("updated_at")
    st["now"] = round(now)
    return st


def set_watch(body):
    """One write endpoint for both surfaces:
      {plan: {...}}                          — replace the whole plan (Lab homework / block editor)
      {action: hold|swap|all_hands, minutes} — the iPad quick edits
      {clear: true}                          — drop the plan
    Returns the fresh get_watch()."""
    body = body or {}
    now = time.time()
    if body.get("clear"):
        stored = watchplan.empty_plan()
    elif "plan" in body:
        stored = watchplan.normalize(body["plan"])
        stored["updated_at"] = now
    elif body.get("action"):
        stored = watchplan.apply_edit(get_plan(), str(body["action"]),
                                      now, minutes=body.get("minutes"))
    else:
        return get_watch(now)
    datasource.active().save_watch_plan(stored)
    return get_watch(now)
