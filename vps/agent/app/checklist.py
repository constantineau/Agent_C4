"""Race checklist — the SI/NOR requirement reminders, live (the `deliver_to_ipad` subset).

The Lab's Lab-0 ingestion extracts the COMPREHENSIVE requirement checklist from the NOR/SI/SER
(`shared/race_def.py` Requirement); the items flagged `deliver_to_ipad` ride the homework package
(`checklists_ipad`) and are loaded here. This module evaluates each item's trigger against the
boat's own live picture and surfaces it AT ITS MOMENT — nav lights at sunset, the gate photo on
the Cove Island approach, the finish procedure (cross E→W, GPS photo, display numbers) on the
finish approach — as a persistent action item until the crew acks it.

Deterministic scheduling on the boat's own computer + pre-loaded homework — no RRS-41 exposure,
no gate; empty-but-valid when nothing is loaded (same standalone discipline as the watch system).

Trigger taxonomy (shared/race_def.py TRIGGER_TYPES → what the engine can evaluate):
  time      "sunset->sunrise"      — active from sunset−lead to the next sunrise (NOAA solar calc
                                     on live position); re-arms every evening (multi-night race).
  location  "Cove Island gate"     — matched by name against the loaded course marks; arms inside
                                     CHECKLIST_LOC_NM and LATCHES until acked (passing the mark
                                     without the photo must not silence the reminder).
  event     "finishing"            — arms when the next mark is the course's last (the finish) and
                                     it is inside CHECKLIST_FINISH_NM; latches until acked.
  anything else                    — "manual": always listed, ackable, never auto-fires (e.g. the
                                     sponsor-flag item's "if supplied / per SI").

Statuses: pending (trigger not yet met) · active (met or latched, awaiting ack) · done (acked
for the current window) · manual. Acks persist in the engine kv (survive a service restart);
a time item's ack is per-window (tonight's ack doesn't silence tomorrow night).
"""
import math
import os
import time

from . import datasource, navigator

LEAD_MIN = float(os.environ.get("CHECKLIST_LEAD_MIN", "30"))        # arm this before sunset
LOC_NM = float(os.environ.get("CHECKLIST_LOC_NM", "8"))             # location-trigger arm radius
FINISH_NM = float(os.environ.get("CHECKLIST_FINISH_NM", "10"))      # finish-trigger arm distance

# words too generic to identify a mark by name ("gate" alone must not match every gate)
_STOPWORDS = {"the", "a", "at", "of", "to", "in", "on", "gate", "mark", "line", "virtual",
              "island", "islands", "point", "light"}


# --- solar (NOAA general solar position approximation — pure math, ±few minutes) ---------------
def _sun_times(lat, lon, epoch):
    """(sunrise, sunset) epochs (UTC) for the UTC calendar day containing `epoch`, or
    (None, None) in polar day/night."""
    tm = time.gmtime(epoch)
    gamma = 2 * math.pi / 365 * (tm.tm_yday - 1 + (tm.tm_hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    lat_r = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lat_r) * math.cos(decl))
              - math.tan(lat_r) * math.tan(decl))
    if not -1.0 <= cos_ha <= 1.0:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    midnight = epoch - (tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec)
    sunrise = midnight + (720 - 4 * (lon + ha) - eqtime) * 60
    sunset = midnight + (720 - 4 * (lon - ha) - eqtime) * 60
    return sunrise, sunset


def _night_window(lat, lon, now):
    """The relevant sunset→next-sunrise window around `now`: the pair we are inside (or whose
    lead we are inside), else the NEXT upcoming one. Scans the UTC days around now so a venue
    whose local sunset lands past UTC midnight (Lake Huron in July) still pairs correctly.
    Returns (sunset, next_sunrise) or (None, None) where the sun never sets/rises."""
    pairs = []
    for d in (-1, 0, 1):
        _, ss = _sun_times(lat, lon, now + d * 86400)
        if ss is None:
            continue
        # the sunrise ENDING this night = the first one after ss. A July Lake-Huron sunset
        # lands past UTC midnight, so ss's own UTC day usually holds it; else the next day's.
        sr_next, _ = _sun_times(lat, lon, ss)
        if sr_next is None or sr_next <= ss:
            sr_next, _ = _sun_times(lat, lon, ss + 86400)
        if sr_next is None:
            continue
        if not any(abs(ss - p[0]) < 3600 for p in pairs):
            pairs.append((ss, sr_next))
    for ss, sr in sorted(pairs):
        if now <= sr:                       # inside this night, or it's still ahead
            return ss, sr
    return (pairs[-1] if pairs else (None, None))


# --- normalization / persistence ----------------------------------------------------------------
def _normalize(items):
    out, seen = [], set()
    for r in items or []:
        if not isinstance(r, dict) or not r.get("id") or not r.get("text"):
            continue
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({"id": str(r["id"]), "category": r.get("category", ""),
                    "phase": r.get("phase", ""), "text": str(r["text"]),
                    "trigger_type": r.get("trigger_type", "none"),
                    "trigger_detail": r.get("trigger_detail", ""),
                    "critical": bool(r.get("critical")), "source": r.get("source", "")})
    return out


def _blob():
    try:
        return datasource.active().get_checklist() or {}
    except Exception:
        return {}


def load(body):
    """Load the iPad checklist (homework `checklists_ipad`). Body: {items: [...]} /
    {checklists_ipad: [...]} / {definition: <RaceDefinition>} (filters deliver_to_ipad).
    Replaces any prior list and clears acks/latches (a fresh race)."""
    body = body or {}
    items = body.get("items") or body.get("checklists_ipad")
    if items is None and body.get("definition") is not None:
        reqs = (body["definition"] or {}).get("requirements") or []
        items = [r for r in reqs if r.get("deliver_to_ipad")]
    norm = _normalize(items)
    if not norm:
        return {"loaded": False, "detail": "no checklist items (need id + text)"}
    datasource.active().save_checklist({"items": norm, "loaded_at": time.time(),
                                        "race_id": body.get("race_id")
                                        or (body.get("definition") or {}).get("race_id"),
                                        "acks": {}, "armed": {}})
    return {"loaded": True, "items": len(norm)}


# --- trigger evaluation ---------------------------------------------------------------------------
def _tokens(s):
    return {w for w in "".join(ch if ch.isalnum() else " " for ch in (s or "").lower()).split()
            if len(w) > 2 and w not in _STOPWORDS}


def _match_mark(detail, marks):
    """The course mark the location detail names — max significant-token overlap, None if none."""
    want = _tokens(detail)
    if not want:
        return None
    best, best_n = None, 0
    for m in marks or []:
        n = len(want & _tokens(m.get("name")))
        if n > best_n:
            best, best_n = m, n
    return best


def _fmt_min(mins):
    return f"{round(mins / 60, 1)} h" if mins >= 90 else f"{round(mins)} min"


def _eval_item(item, ctx, now):
    """(in_window, window_key, measure) for one item against the live picture. window_key
    identifies the arming occasion (per-night for time triggers) so acks scope correctly;
    None window_key ⇒ 'manual' (nothing the engine can evaluate)."""
    tt, detail = item.get("trigger_type"), (item.get("trigger_detail") or "").lower()
    lat, lon = ctx.get("lat"), ctx.get("lon")

    if tt == "time" and "sunset" in detail:
        if lat is None:
            return False, "night", "no position fix — can't time sunset"
        ss, sr = _night_window(lat, lon, now)
        if ss is None:
            return False, "night", "no sunset at this latitude"
        wkey = f"night:{int(ss)}"
        if ss - LEAD_MIN * 60 <= now <= sr:
            left = ("sunset in " + _fmt_min((ss - now) / 60)) if now < ss else \
                   ("until sunrise (" + _fmt_min((sr - now) / 60) + ")")
            return True, wkey, left
        return False, wkey, "sunset in " + _fmt_min((ss - now) / 60)

    if tt == "location":
        mark = _match_mark(item.get("trigger_detail"), ctx.get("marks"))
        if mark is None:
            return False, None, None                       # no such mark aboard → manual
        if lat is None:
            return False, f"loc:{mark['name']}", f"{mark['name']} — no position fix"
        d = navigator._hav_nm(lat, lon, mark["lat"], mark["lon"])
        wkey = f"loc:{mark['name']}"
        if d <= LOC_NM:
            return True, wkey, f"{mark['name']} — {round(d, 1)} nm"
        return False, wkey, f"{mark['name']} in {round(d, 1)} nm (arms at {round(LOC_NM)})"

    if tt == "event" and "finish" in detail:
        nm, total = ctx.get("next_mark") or {}, ctx.get("marks_total") or 0
        d = nm.get("distance_nm")
        if not nm or d is None or not total:
            return False, "finish", "no course/position aboard"
        on_final = nm.get("index") == total - 1
        if on_final and d <= FINISH_NM:
            return True, "finish", f"finish in {round(d, 1)} nm"
        return False, "finish", (f"finish in {round(d, 1)} nm" if on_final
                                 else f"{total - 1 - (nm.get('index') or 0)} legs to the finish")

    return False, None, None                               # unevaluable → manual


def _context(route):
    """One shared live read for the whole list: position + course marks + the navigator's
    next-mark/finish picture. Every field degrades to None — the checklist still lists."""
    ctx = {"lat": None, "lon": None, "marks": [], "next_mark": None, "marks_total": 0}
    try:
        s = navigator._latest()
        ctx["lat"], ctx["lon"] = s.get("lat"), s.get("lon")
    except Exception:
        pass
    try:
        ctx["marks"] = navigator._marks(route or navigator.active_route()) or []
        ctx["marks_total"] = len(ctx["marks"])
    except Exception:
        pass
    try:
        nav = navigator.get_navigator(route)
        if nav.get("available"):
            ctx["next_mark"] = nav.get("next_mark")
            ctx["marks_total"] = nav.get("marks_total") or ctx["marks_total"]
    except Exception:
        pass
    return ctx


def get_checklist(route=None, now=None):
    """The live checklist state (the iPad card + coach read): every item with status
    pending/active/done/manual + a live measure ("Cove Island Virtual Gate in 23.4 nm").
    Active = trigger met OR previously latched, not yet acked for the current window."""
    now = now or time.time()
    blob = _blob()
    items = blob.get("items") or []
    if not items:
        return {"available": True, "plan_set": False, "items": [], "active": [],
                "counts": {}, "now": round(now)}
    acks, armed = dict(blob.get("acks") or {}), dict(blob.get("armed") or {})
    ctx = _context(route)
    out, dirty = [], False
    for item in items:
        in_window, wkey, measure = _eval_item(item, ctx, now)
        it = dict(item)
        it["measure"] = measure
        arm = armed.get(item["id"])
        if in_window and (not arm or arm.get("window") != wkey):
            arm = {"at": now, "window": wkey}              # newly armed (or a new night)
            armed[item["id"]] = arm
            dirty = True
        # a time item's latch expires with its night (re-arms next sunset);
        # location/event latches persist until acked
        aw = str((arm or {}).get("window", ""))
        latched = bool(arm) and (not aw.startswith("night:") or aw == wkey)
        ack = acks.get(item["id"])
        acked = bool(ack) and (wkey is None or ack.get("window") == wkey)
        if wkey is None:
            it["status"] = "done" if acked else "manual"
        elif acked:
            it["status"] = "done"
        elif in_window or latched:
            it["status"] = "active"
            it["armed_at"] = arm.get("at") if arm else now
        else:
            it["status"] = "pending"
        out.append(it)
    if dirty:                                              # persist new latches (restart-safe)
        blob["armed"] = armed
        try:
            datasource.active().save_checklist(blob)
        except Exception:
            pass
    active = [i for i in out if i["status"] == "active"]
    counts = {}
    for i in out:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return {"available": True, "plan_set": True, "race_id": blob.get("race_id"),
            "items": out, "active": active, "counts": counts,
            "loaded_at": blob.get("loaded_at"), "now": round(now)}


def ack(body, now=None):
    """Crew ack: {id, undo?} — done for the current window (time items re-arm next night).
    Returns the fresh get_checklist()."""
    body = body or {}
    now = now or time.time()
    iid = str(body.get("id") or "")
    blob = _blob()
    items = {i["id"]: i for i in (blob.get("items") or [])}
    if iid not in items:
        return {"available": False, "note": f"no checklist item '{iid}'"}
    acks = dict(blob.get("acks") or {})
    if body.get("undo"):
        acks.pop(iid, None)
    else:
        _, wkey, _ = _eval_item(items[iid], _context(body.get("route")), now)
        acks[iid] = {"at": now, "window": wkey}
    blob["acks"] = acks
    datasource.active().save_checklist(blob)
    return get_checklist(body.get("route"), now=now)
