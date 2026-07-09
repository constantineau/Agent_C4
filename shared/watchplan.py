"""Watch plan — the crew rotation schedule, shared by the Lab editor, the onboard engine and
the dashboard CREW tile.

SINGLE SOURCE OF TRUTH for "who is on watch at time t". The Lab authors a plan (a pattern
generator + hand edits), the homework package seeds it aboard, and the iPad edits it live
(hold a watch, swap teams, call all hands) — real races never run the plan as written. Pure
stdlib so it imports anywhere the repo is present (Lab container, engine image, tests).

The STORED format is always the explicit block list — generators are conveniences that emit
blocks. Blocks are absolute-epoch, sorted, non-overlapping:

    {"schema": "c4.watchplan/v1",
     "teams":  {"A": {"name": "Port", "members": ["..."]},
                "B": {"name": "Starboard", "members": []}},
     "blocks": [{"start": epoch, "end": epoch, "on": "A"|"B"|"ALL", "note": "..."}, ...],
     "updated_at": epoch,
     "log": [{"ts": epoch, "action": "...", "detail": "..."}, ...]}   # capped edit history

"ALL" = all hands (start, finish, big maneuvers). A gap between blocks = no watch system in
effect (deliveries, post-finish). Times are epoch seconds; the UIs render boat-local clock.
"""

SCHEMA = "c4.watchplan/v1"
TEAM_IDS = ("A", "B")
ON_VALUES = ("A", "B", "ALL")
LOG_CAP = 100

# named generator presets: the repeating cycle of block lengths, in hours. The classic
# "swedish" 4-4-4-6-6 cycle is 24 h in 5 blocks — the odd count flips which team gets
# which night slot on alternate days.
PATTERNS = {
    "4on4off": [4.0],
    "3on3off": [3.0],
    "6on6off": [6.0],
    "swedish": [4.0, 4.0, 4.0, 6.0, 6.0],
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def empty_plan():
    return {"schema": SCHEMA,
            "teams": {"A": {"name": "A", "members": []}, "B": {"name": "B", "members": []}},
            "blocks": [], "updated_at": None, "log": []}


def normalize(plan):
    """Coerce an untrusted plan blob into the canonical shape: valid teams, blocks sorted by
    start with bad rows dropped and overlaps clipped (the later block's start wins). Never
    raises — worst case returns an empty plan."""
    if not isinstance(plan, dict):
        return empty_plan()
    out = empty_plan()
    teams = plan.get("teams") or {}
    for tid in TEAM_IDS:
        t = teams.get(tid) or {}
        name = str(t.get("name") or tid).strip() or tid
        members = [str(m).strip() for m in (t.get("members") or []) if str(m).strip()]
        out["teams"][tid] = {"name": name, "members": members}
    blocks = []
    for b in (plan.get("blocks") or []):
        if not isinstance(b, dict):
            continue
        start, end, on = _f(b.get("start")), _f(b.get("end")), str(b.get("on") or "").upper()
        if start is None or end is None or end <= start or on not in ON_VALUES:
            continue
        note = str(b.get("note")).strip() if b.get("note") else None
        blocks.append({"start": start, "end": end, "on": on, "note": note or None})
    blocks.sort(key=lambda b: b["start"])
    clipped = []
    for b in blocks:
        if clipped and b["start"] < clipped[-1]["end"]:
            clipped[-1]["end"] = b["start"]          # later block wins the overlap
            if clipped[-1]["end"] <= clipped[-1]["start"]:
                clipped.pop()
        clipped.append(b)
    out["blocks"] = clipped
    out["updated_at"] = _f(plan.get("updated_at"))
    log = [e for e in (plan.get("log") or []) if isinstance(e, dict)]
    out["log"] = log[-LOG_CAP:]
    return out


def generate(anchor, total_hours, pattern, first_on="A"):
    """Emit alternating-team blocks: `pattern` is a named preset (see PATTERNS) or a list of
    block lengths in hours, cycled from `anchor` (epoch) until `total_hours` is covered (the
    last block is clipped). All-hands segments are hand-edits on top."""
    hours = PATTERNS.get(pattern) if isinstance(pattern, str) else list(pattern or [])
    hours = [h for h in (_f(x) for x in (hours or [])) if h and h > 0]
    anchor, total_hours = _f(anchor), _f(total_hours)
    if not hours or anchor is None or not total_hours or total_hours <= 0:
        return []
    blocks, t, i = [], anchor, 0
    stop = anchor + total_hours * 3600.0
    on = "A" if str(first_on).upper() != "B" else "B"
    while t < stop:
        end = min(t + hours[i % len(hours)] * 3600.0, stop)
        blocks.append({"start": t, "end": end, "on": on, "note": None})
        t, i, on = end, i + 1, ("B" if on == "A" else "A")
    return blocks


def _team_label(plan, on):
    if on == "ALL":
        return "ALL HANDS"
    t = (plan.get("teams") or {}).get(on) or {}
    return t.get("name") or on


def status_at(plan, now, upcoming=3):
    """Resolve the plan at time `now` → what every consumer (tile, matcher, coach) reads:
    {active, on, on_label, block, next_change, next_on, next_on_label, mins_to_change,
     all_hands, upcoming:[blocks]}. Outside any block: active=False, next_change = the next
    block's start (None after the plan ends)."""
    now = _f(now)
    blocks = plan.get("blocks") or []
    cur, nxt = None, None
    for b in blocks:
        if b["start"] <= now < b["end"]:
            cur = b
        elif b["start"] > now and nxt is None:
            nxt = b
    if cur is not None:
        # the change is the current block's end even if a gap follows
        change = cur["end"]
        after = nxt if (nxt and abs(nxt["start"] - cur["end"]) < 1.0) else None
        next_on = after["on"] if after else None
    else:
        change = nxt["start"] if nxt else None
        next_on = nxt["on"] if nxt else None
    up = [b for b in blocks if b["start"] > now][:max(0, int(upcoming))]
    return {
        "active": cur is not None,
        "on": cur["on"] if cur else None,
        "on_label": _team_label(plan, cur["on"]) if cur else None,
        "block": cur,
        "all_hands": bool(cur and cur["on"] == "ALL"),
        "next_change": change,
        "next_on": next_on,
        "next_on_label": _team_label(plan, next_on) if next_on else None,
        "mins_to_change": round((change - now) / 60.0, 1) if change is not None else None,
        "upcoming": up,
    }


# ---------------------------------------------------------------------------- live edits
# The quick actions crews actually take mid-race. Each returns the EDITED plan (normalized)
# with a log entry appended; the caller persists.

def _log(plan, now, action, detail):
    plan["log"] = ((plan.get("log") or []) + [{"ts": now, "action": action, "detail": detail}])[-LOG_CAP:]
    plan["updated_at"] = now
    return plan


def hold(plan, now, minutes):
    """Extend the CURRENT block by `minutes` and push every later block back the same amount
    ('we'll run this watch another hour'). No current block → no-op."""
    plan, now = normalize(plan), _f(now)
    delta = (_f(minutes) or 0) * 60.0
    st = status_at(plan, now)
    if not st["active"] or delta <= 0:
        return plan
    cur_end = st["block"]["end"]
    for b in plan["blocks"]:
        if b["end"] >= cur_end and b["start"] <= now < b["end"]:
            b["end"] += delta
        elif b["start"] >= cur_end:
            b["start"] += delta
            b["end"] += delta
    return _log(plan, now, "hold", f"held {st['on_label']} +{int(round(delta / 60))}m")


def swap(plan, now):
    """Swap A<->B on the current and every later block ('other team takes it from here').
    ALL blocks unchanged."""
    plan, now = normalize(plan), _f(now)
    flip = {"A": "B", "B": "A"}
    hit = False
    for b in plan["blocks"]:
        if b["end"] > now and b["on"] in flip:
            b["on"] = flip[b["on"]]
            hit = True
    return _log(plan, now, "swap", "teams swapped from here on") if hit else plan


def all_hands(plan, now, minutes):
    """Insert an ALL block from `now` for `minutes`, splitting/eating whatever it overlaps
    ('everyone up for the gybe/squall'). Works with no current block too."""
    plan, now = normalize(plan), _f(now)
    delta = (_f(minutes) or 0) * 60.0
    if delta <= 0:
        return plan
    end = now + delta
    kept = []
    for b in plan["blocks"]:
        if b["end"] <= now or b["start"] >= end:
            kept.append(b)
            continue
        if b["start"] < now:
            kept.append({**b, "end": now})
        if b["end"] > end:
            kept.append({**b, "start": end})
    kept.append({"start": now, "end": end, "on": "ALL", "note": None})
    plan["blocks"] = sorted(kept, key=lambda b: b["start"])
    return _log(plan, now, "all_hands", f"all hands {int(round(delta / 60))}m")


def apply_edit(plan, action, now, **kw):
    """Dispatch a named quick edit; unknown action returns the plan unchanged."""
    if action == "hold":
        return hold(plan, now, kw.get("minutes", 60))
    if action == "swap":
        return swap(plan, now)
    if action == "all_hands":
        return all_hands(plan, now, kw.get("minutes", 30))
    return normalize(plan)
