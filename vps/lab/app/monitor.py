"""Monitor — shore-side live view during the race.

Two sources, both shore-side (the boat itself uses the onboard console):
  - FLEET: the race's tracker, through its provider (`trackers.live`, #310): a public feed such
    as YB, delayed, or the last positions in an uploaded fleet file (never live). Every fix
    carries its own timestamp so the UI can age it. Shore-side viewing of a public tracker
    is always fine — the onboard-use gate (`rules_profile.tracker_permitted`) is a separate, in-race
    concern, surfaced here only as a note.
  - OWN BOAT: our boat's live position + instruments from the cloud agent (the uplinked telemetry),
    via GET /conditions. Best-effort: if the agent is unreachable or the boat isn't uplinking, the
    panel says so.
"""
import json
import os
import time
import urllib.request

from shared import race_def

from . import trackers

_TIMEOUT = float(os.environ.get("MONITOR_TIMEOUT_S", "8"))
_AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000").rstrip("/")
_BOAT_PASSWORD = os.environ.get("BOAT_PASSWORD", "")

# How old the NEWEST fleet fix may be before the Monitor stops calling the fleet live. A public
# tracker runs ~15 min behind and boats ping every 10-30 min, so a healthy feed's newest fix is
# well under an hour old. Past an hour something is wrong (feed stalled, race over, wrong race id);
# past three hours the picture is useless for watching a race.
FRESH_MAX_S = 60 * 60
STALE_MAX_S = 3 * 60 * 60
_tok = {"value": None, "exp": 0.0}


def _http_json(url, data=None, headers=None, timeout=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as r:
        return json.loads(r.read().decode())


def freshness(fixes, now=None):
    """Age of the newest fix and a fresh / stale / very_stale verdict (None when no fix has a time).

    Measured from the fixes themselves, never from the configured `delay_min` — that is only what
    the tracker promises, and a dead feed keeps promising it."""
    times = [f.get("time") for f in fixes or [] if f.get("time")]
    if not times:
        return {"newest_fix_time": None, "newest_age_s": None, "freshness": None}
    newest = max(times)
    age = max(0.0, (time.time() if now is None else now) - newest)
    state = "fresh" if age <= FRESH_MAX_S else "stale" if age <= STALE_MAX_S else "very_stale"
    return {"newest_fix_time": newest, "newest_age_s": round(age), "freshness": state}


def _course_centre(definition):
    """Mean lat/lon of the race's first course marks (start, marks, finish), or None (#312)."""
    try:
        marks, _skip, _cid = race_def.course_to_marks(definition or {})
    except Exception:
        return None
    ll = [(m[2], m[3]) for m in marks if m[2] is not None and m[3] is not None]
    if not ll:
        return None
    return sum(a for a, _ in ll) / len(ll), sum(b for _, b in ll) / len(ll)


# demo boats as offsets (deg) from the course centre: never a fixed venue (#312)
_SAMPLE = [("Il Mostro", 0.00, -0.10, 7.1, 18), ("Windquest", 0.12, 0.05, 6.4, 25),
           ("Natalie J", -0.08, -0.20, 5.9, 12), ("Equation", 0.25, 0.18, 7.8, 30)]


def _sample_fleet(definition=None):
    centre = _course_centre(definition)
    if centre is None:
        return []
    now = time.time()
    return [{"name": n, "lat": round(centre[0] + dla, 4), "lon": round(centre[1] + dlo, 4), "sog": s,
             "cog": c, "time": now - 900, "dtf_nm": None} for (n, dla, dlo, s, c) in _SAMPLE]


def _norm(name):
    return " ".join(str(name or "").split()).casefold()


def mark_own(fixes, own_name):
    """Flag the tracker fix that is OUR boat (the race's `own.boat`, matched by name) so the map can
    draw it as ours instead of as one more blue dot. Returns how many fixes matched."""
    key = _norm(own_name)
    n = 0
    for f in fixes or []:
        if key and _norm(f.get("name")) == key:
            f["own"] = True
            n += 1
    return n


def fleet(definition, demo=False):
    out = _fleet(definition, demo=demo)
    out["own_name"] = (((definition or {}).get("own") or {}).get("boat") or "").strip() or None
    mark_own(out["fixes"], out["own_name"])
    out.update(freshness(out["fixes"]))
    out["fresh_max_s"], out["stale_max_s"] = FRESH_MAX_S, STALE_MAX_S
    return out


def _fleet(definition, demo=False):
    cfg = (definition or {}).get("tracker") or {}
    rp = (definition or {}).get("rules_profile") or {}
    base = {"fixes": [], "provider": (cfg.get("provider") or None), "delay_min": cfg.get("delay_min"),
            # the race rule, tri-state: True / False / None (not set in the rules yet)
            "onboard_permitted": (rp.get("tracker_permitted")
                                  if isinstance(rp.get("tracker_permitted"), bool) else None),
            "reason": ""}
    if demo or (cfg.get("provider") or "").lower() == "sample":
        fixes = _sample_fleet(definition)
        base.update(fixes=fixes, provider="sample",
                    reason="demo fixture" if fixes else "demo needs a course with positions")
        return base
    # the race's tracker provider (#310): YB fetches live; a fleet file shows its last positions;
    # any other tracker says plainly it has no live positions here
    res = trackers.live(definition, get_json=lambda url: _http_json(url))
    base.update(fixes=res.get("fixes") or [], reason=res.get("note") or "",
                label=res.get("label"), live=bool(res.get("fetches")))
    return base


def agent_json(path, timeout=None):
    """Authed GET against the cloud agent (shared by Monitor and the debrief's boat-log
    track source). Raises on auth failure / network error. `timeout` overrides the 8 s default
    for calls that legitimately walk a whole race (a 7-hour full-res track is ~20-30 s)."""
    tok = _agent_token()
    if not tok:
        raise RuntimeError("agent auth failed (BOAT_PASSWORD)")
    return _http_json(_AGENT_URL + path, headers={"Authorization": "Bearer " + tok},
                      timeout=timeout)


def _agent_token():
    now = time.time()
    if _tok["value"] and _tok["exp"] > now:
        return _tok["value"]
    if not _BOAT_PASSWORD:
        return None
    j = _http_json(_AGENT_URL + "/auth", data=json.dumps({"password": _BOAT_PASSWORD}).encode(),
                   headers={"Content-Type": "application/json"})
    t = j.get("token")
    if t:
        _tok["value"], _tok["exp"] = t, now + 3600
    return t


def own():
    if not _BOAT_PASSWORD:
        return {"available": False, "reason": "agent link not configured (BOAT_PASSWORD unset)"}
    try:
        tok = _agent_token()
        if not tok:
            return {"available": False, "reason": "agent auth failed"}
        c = _http_json(_AGENT_URL + "/conditions", headers={"Authorization": "Bearer " + tok})
    except Exception as e:
        return {"available": False, "reason": f"agent unreachable: {type(e).__name__}"}
    if not c.get("available") or c.get("lat") is None:
        return {"available": False, "reason": "no live position from the boat yet"}
    out = {"available": True, "lat": c.get("lat"), "lon": c.get("lon"), "sog": c.get("sog"),
           "cog": c.get("cog"), "heading": c.get("heading"), "stw": c.get("stw"),
           "tws": c.get("tws"), "twd": c.get("twd"), "as_of": c.get("as_of"),
           "age_s": c.get("data_age_seconds"), "stale": c.get("stale"),
           "heading_ref": None, "heading_age_s": None, "cog_age_s": None}
    out.update(_heading_detail(tok))
    return out


def _heading_detail(tok):
    """Which compass the heading came from (true / magnetic) and how old heading and COG are.

    /conditions folds heading_true and heading_mag into one bare `heading`, and gives one age for
    the whole strip. The map may only point the hull along a heading that is fresh and whose
    reference is known (#240), so read the per-channel view and take the heading from the same
    channel we name. Best-effort: on any failure the reference stays unknown (neutral symbol)."""
    try:
        full = _http_json(_AGENT_URL + "/conditions/full", headers={"Authorization": "Bearer " + tok})
    except Exception:
        return {}
    ch = (full or {}).get("channels") or {}
    out = {}
    for name, ref in (("heading_true", "true"), ("heading_mag", "magnetic")):   # strip's order
        p = (ch.get(name) or {}).get("preferred") or {}
        if p.get("value") is not None:
            out.update(heading=p["value"], heading_ref=ref, heading_age_s=p.get("age_s"))
            break
    p = (ch.get("cog") or {}).get("preferred") or {}
    if p.get("value") is not None:
        out.update(cog=p["value"], cog_age_s=p.get("age_s"))
    return out


def course_marks(definition):
    """Every positioned mark of every course in the race, in sailing order (start, marks,
    gate/finish midpoints), tagged with its course's index so the map can rebuild each course's
    legs. The Monitor map fits to these plus our boat, so a stray tracker fix far inland can't zoom
    the view out to the whole lake (#102 G86)."""
    out, done = [], set()
    for i, c in enumerate((definition or {}).get("courses") or []):
        if not isinstance(c, dict):
            continue
        marks, _skipped, cid = race_def.course_to_marks(definition, c.get("id"))
        if cid in done:            # two courses without an id both resolve to the first
            continue
        done.add(cid)
        for _seq, name, lat, lon in marks:
            out.append({"name": name, "lat": float(lat), "lon": float(lon), "course": i})
    return out


def snapshot(definition, demo=False):
    try:
        marks = course_marks(definition)
    except Exception:
        marks = []
    return {"fleet": fleet(definition, demo=demo), "own": own(), "marks": marks}
