"""Lab-1 optimizer core — isochrone routing over a RaceDefinition course through a WindField.

Self-contained (no agent package): given a course's ordered marks, the boat polars and a multi-model
`WindField`, it routes leg-by-leg with the classic isochrone method — fan every heading over a short
time step, advance each by the polar boatspeed at the local TWA, prune to the outer envelope, repeat
until the envelope lays the mark, then backtrack the optimal path (which naturally tacks upwind /
gybes downwind). It samples the wind field's per-point confidence along the route so the briefing can
honestly flag where the models disagree.

Output = ONE optimal route + per-leg summary + a route-wide confidence + an Opus-written briefing.
Lab-2 will fan this across ensemble members/scenarios into a branching playbook; Lab-1 is the core.
RRS 41: this is pre-race cloud homework, frozen at the gun.
"""
from __future__ import annotations

import math
import os
import time

from shared import race_def
from . import polars as POL

HSTEP = 12          # heading fan resolution (deg)
SECTOR = 3.0        # isochrone pruning bucket (deg of bearing from leg start)
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")


# --- geometry ----------------------------------------------------------------
def _wrap180(d):
    return ((d + 180) % 360) - 180


def _hav_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _advance(lat, lon, brg, dist_nm):
    b = math.radians(brg)
    return (lat + dist_nm * math.cos(b) / 60.0,
            lon + dist_nm * math.sin(b) / (60.0 * max(0.1, math.cos(math.radians(lat)))))


# --- polars ------------------------------------------------------------------
def _polar_speed(P, tws, twa):
    if not P or twa < 30:
        return 0.0
    return min(P, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))[2]


def _point_of_sail(twa):
    return "beat" if twa < 70 else ("reach" if twa < 130 else "run")


# --- one leg -----------------------------------------------------------------
def route_leg(wf, P, slat, slon, t0, dlat, dlon, fallback=(12.0, 0.0), deadline=None,
              obstacles=None):
    """Isochrone-optimal path from (slat,slon)@t0 to (dlat,dlon). Returns dict with path/eta.

    `obstacles` (an ObstacleField) makes the fan reject any heading whose step would cut across land,
    an island, or a race exclusion zone — so the route sails AROUND obstacles instead of through them."""
    direct = _hav_nm(slat, slon, dlat, dlon)
    dt_h = min(1.0, max(0.15, direct / 40.0))          # coarser steps for long legs
    max_steps = 600
    headings = list(range(0, 360, HSTEP))
    blocked_hits = 0

    def wind(lat, lon, epoch):
        w = wf.wind_at(lat, lon, epoch)
        return w if w else fallback

    start = {"lat": slat, "lon": slon, "t": t0, "parent": None, "hdg": None}
    frontier = [start]
    reached = None
    for _ in range(max_steps):
        if deadline and time.time() > deadline:
            break
        cand = {}
        for node in frontier:
            tws, twd = wind(node["lat"], node["lon"], node["t"])
            dmark = _hav_nm(node["lat"], node["lon"], dlat, dlon)
            bmark = _bearing(node["lat"], node["lon"], dlat, dlon)
            twa_m = abs(_wrap180(bmark - twd))
            sp_m = _polar_speed(P, tws, twa_m)
            if sp_m > 0.3 and dmark <= sp_m * dt_h and not (
                    obstacles and obstacles.crosses(node["lat"], node["lon"], dlat, dlon)):
                reached = {"lat": dlat, "lon": dlon, "t": node["t"] + (dmark / sp_m) * 3600,
                           "parent": node, "hdg": bmark}
                break
            for hdg in headings:
                twa = abs(_wrap180(hdg - twd))
                sp = _polar_speed(P, tws, twa)
                if sp < 0.3:
                    continue
                nlat, nlon = _advance(node["lat"], node["lon"], hdg, sp * dt_h)
                if obstacles and obstacles.crosses(node["lat"], node["lon"], nlat, nlon):
                    blocked_hits += 1
                    continue
                rng = _hav_nm(slat, slon, nlat, nlon)
                sec = round(_bearing(slat, slon, nlat, nlon) / SECTOR)
                if sec not in cand or rng > cand[sec]["rng"]:
                    cand[sec] = {"lat": nlat, "lon": nlon, "t": node["t"] + dt_h * 3600,
                                 "parent": node, "hdg": hdg, "rng": rng}
        if reached or not cand:
            break
        frontier = list(cand.values())
        best = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))
        if _hav_nm(best["lat"], best["lon"], dlat, dlon) < 0.05:
            reached = best
            break
    if not reached:
        reached = min(frontier, key=lambda n: _hav_nm(n["lat"], n["lon"], dlat, dlon))

    path, node, hdgs = [], reached, []
    while node is not None:
        path.append({"lat": round(node["lat"], 5), "lon": round(node["lon"], 5), "t": node["t"]})
        if node["hdg"] is not None:
            hdgs.append(node["hdg"])
        node = node["parent"]
    path.reverse(); hdgs.reverse()
    sailed = sum(_hav_nm(path[i]["lat"], path[i]["lon"], path[i + 1]["lat"], path[i + 1]["lon"])
                 for i in range(len(path) - 1))
    # tacks/gybes = sign changes of TWA along the path (port↔starboard)
    tacks = 0
    prev_side = None
    for h in hdgs:
        w = wind(slat, slon, t0)
        side = "stbd" if _wrap180(w[1] - h) > 0 else "port"
        if prev_side and side != prev_side:
            tacks += 1
        prev_side = side
    return {"path": path, "eta": reached["t"], "sailed_nm": round(sailed, 2),
            "direct_nm": round(direct, 2), "tacks": tacks,
            "first_heading": round(hdgs[0]) if hdgs else None,
            "blocked_steps": blocked_hits}


# --- full course -------------------------------------------------------------
def optimize_course(definition: dict, course_id, start_epoch, wf, time_budget_s=90,
                    obstacles=None, avoid=True):
    """Route the whole course from its start through every mark to the finish via `wf`.

    Returns one optimal route with per-leg ETAs, total time/distance/tacks and a route confidence
    (mean of the wind field's per-point model agreement sampled along the path).

    `obstacles` (an ObstacleField) keeps the route off land/islands/exclusion-zones; if None and
    `avoid` is set, one is built from the course bbox + this race's zones + island marks."""
    marks, skipped, cid = race_def.course_to_marks(definition, course_id)
    if len(marks) < 2:
        return {"available": False, "note": "course needs at least a start and one mark/finish",
                "skipped": skipped}
    P = POL.polars_stw()
    if not P:
        return {"available": False, "note": "no polars loaded"}

    if obstacles is None and avoid:
        bbox = course_bbox(definition, course_id)
        if bbox:
            try:
                from .geo import build_for_course
                obstacles = build_for_course(definition, cid or course_id, bbox)
            except Exception:
                obstacles = None

    deadline = time.time() + time_budget_s
    legs = []
    t = float(start_epoch)
    slat, slon = marks[0][2], marks[0][3]
    confs = []
    full_path = [{"lat": slat, "lon": slon, "t": t}]
    for seq, name, dlat, dlon in marks[1:]:
        leg = route_leg(wf, P, slat, slon, t, dlat, dlon, deadline=deadline, obstacles=obstacles)
        # sample wind + confidence at the leg's midpoint and end (for the briefing)
        mid = leg["path"][len(leg["path"]) // 2] if leg["path"] else {"lat": dlat, "lon": dlon}
        det = wf.detail_at(mid["lat"], mid["lon"], (t + leg["eta"]) / 2.0)
        if det:
            confs.append(det["confidence"])
        twa = None
        if det:
            twa = abs(_wrap180(_bearing(slat, slon, dlat, dlon) - det["twd"]))
        legs.append({
            "to": name, "seq": seq,
            "direct_nm": leg["direct_nm"], "sailed_nm": leg["sailed_nm"], "tacks": leg["tacks"],
            "leg_minutes": round((leg["eta"] - t) / 60.0, 1),
            "eta_epoch": round(leg["eta"]),
            "first_heading": leg["first_heading"],
            "blocked_steps": leg.get("blocked_steps", 0),
            "point_of_sail": _point_of_sail(twa) if twa is not None else None,
            "wind": ({"tws": det["tws"], "twd": det["twd"], "confidence": det["confidence"]}
                     if det else None),
        })
        full_path += [p for p in leg["path"][1:]]
        slat, slon, t = dlat, dlon, leg["eta"]

    total_min = round((t - float(start_epoch)) / 60.0, 1)
    return {
        "available": True, "course_id": cid,
        "start_epoch": round(float(start_epoch)), "finish_epoch": round(t),
        "total_minutes": total_min, "total_hours": round(total_min / 60.0, 1),
        "total_sailed_nm": round(sum(l["sailed_nm"] for l in legs), 1),
        "total_direct_nm": round(sum(l["direct_nm"] for l in legs), 1),
        "total_tacks": sum(l["tacks"] for l in legs),
        "route_confidence": round(sum(confs) / len(confs), 2) if confs else None,
        "min_confidence": round(min(confs), 2) if confs else None,
        "legs": legs,
        "skipped_marks": skipped,
        "path": [{"lat": p["lat"], "lon": p["lon"]} for p in full_path],
        "windfield": wf.status(),
        "obstacles": obstacles.summary() if obstacles is not None else {"active": False},
        "obstacle_steps_avoided": sum(l.get("blocked_steps", 0) for l in legs),
        "timed_out": time.time() > deadline,
    }


# --- course extent / horizon -------------------------------------------------
def course_bbox(definition: dict, course_id=None, pad=0.5):
    """(north, south, west, east) bounding the course marks, padded. None if no coords."""
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    pts = [(la, lo) for _s, _n, la, lo in marks if la is not None]
    if not pts:
        return None
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return (max(lats) + pad, min(lats) - pad, min(lons) - pad, max(lons) + pad)


def estimate_hours(definition: dict, course_id=None, kn=5.0, margin=1.6, cap=72):
    """Rough course duration (h) from summed direct mark-to-mark distance / a nominal speed —
    used to size the wind-field time window before the route is known."""
    marks, _skip, _cid = race_def.course_to_marks(definition, course_id)
    dist = sum(_hav_nm(marks[i][2], marks[i][3], marks[i + 1][2], marks[i + 1][3])
               for i in range(len(marks) - 1))
    return min(cap, max(2.0, dist / max(1.0, kn) * margin))


# --- briefing ----------------------------------------------------------------
def briefing(result: dict, race_name: str = "") -> str:
    """An Opus-written pre-race routing briefing from the optimizer result. Falls back to a
    deterministic template when no API key is set, so the optimizer always returns a briefing."""
    if not result.get("available"):
        return result.get("note", "No route available.")
    legs = result["legs"]
    facts = {
        "race": race_name, "total_hours": result["total_hours"],
        "total_sailed_nm": result["total_sailed_nm"], "total_tacks": result["total_tacks"],
        "route_confidence": result["route_confidence"], "min_confidence": result["min_confidence"],
        "models": [m["model"] for m in result["windfield"]["models"]],
        "legs": [{"to": l["to"], "minutes": l["leg_minutes"], "point_of_sail": l["point_of_sail"],
                  "tacks": l["tacks"], "wind": l["wind"]} for l in legs],
    }
    if API_KEY:
        try:
            import json
            import anthropic
            client = anthropic.Anthropic(api_key=API_KEY)
            resp = client.messages.create(
                model=MODEL, max_tokens=1200,
                system="You are a yacht race navigator writing a concise PRE-RACE routing briefing "
                       "for the crew from an optimizer result. Explain the recommended route leg by "
                       "leg, the wind story, where to expect tacks/gybes and sail changes, and — "
                       "importantly — call out where model CONFIDENCE is low (models disagree) so "
                       "the crew sails conservatively there. Be specific and brief; no preamble.",
                messages=[{"role": "user", "content":
                           "Optimizer result:\n" + json.dumps(facts, indent=2)}],
            )
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if txt:
                return txt
        except Exception:
            pass
    # deterministic fallback
    lines = [f"Optimal route: {result['total_sailed_nm']} nm sailed, "
             f"~{result['total_hours']} h, {result['total_tacks']} tacks/gybes.",
             f"Model agreement (confidence): {result['route_confidence']} "
             f"(lowest leg {result['min_confidence']}).", ""]
    for l in legs:
        w = l["wind"] or {}
        lines.append(f"• To {l['to']}: {l['leg_minutes']} min, {l['point_of_sail'] or '?'}, "
                     f"{l['tacks']} tacks; wind {w.get('tws','?')} kn @ {w.get('twd','?')}° "
                     f"(conf {w.get('confidence','?')}).")
    if result.get("skipped_marks"):
        lines.append("")
        lines.append("Marks skipped (no coordinates — review): " + ", ".join(result["skipped_marks"]))
    return "\n".join(lines)
