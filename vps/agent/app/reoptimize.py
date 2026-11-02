"""Onboard RE-OPTIMIZER — the Pi's own small re-route, the fallback when the Orin is down.

A FRESH route ONBOARD from the boat's live position through the REMAINING course marks to the
finish, on the boat's OWN polars through the common Open-Meteo forecast. The Pi has no cfgrib, so it
reuses the onboard isochrone (`routing.route_leg`) chained per remaining mark, NOT the GRIB optimizer
(that runs on the Orin: orinroute.py).

It is LEGAL in-race (own computer + own polars + own position + common public data available to
all), but it is NOT the frozen homework, so it is flagged `off_playbook` and reports how far it
DIVERGES from the active variant's frozen track.

BUTTON ONLY (#125, Cole 2026-11-01): a route is computed only when a crew member presses Re-route
now (`POST /reoptimize/run`, the iPad's fallback when the Orin can't be reached). Nothing else starts
one: not /strategy going off-book, not the console's selector poll, not the GPS output. `GET
/reoptimize` only returns the last route made, kept on disk so it survives a restart. One run at a
time, in a background thread (~15 s on a Pi 4); a second press while it runs is refused.
"""
import json
import os
import tempfile
import threading
import time
import uuid

from . import deviation
from . import navigator as NAV
from . import orinroute
from . import reroutelog
from . import routing
from . import sails

EXPECTED_S = 30           # what the progress bar assumes until a run has been timed
BUSY = "A re-route is already running."
FAILED = "Re-route failed. Try again."
NO_ROUTE_YET = "No Pi route yet."
PI_NO_ROUNDING = "Pi route doesn't check rounding sides."
NO_ROUNDING = "No rounding sides aboard. Redeploy the course."

_LOCK = threading.Lock()
_job = {"state": "idle", "job_id": None, "asked_by": None, "started_at": None,
        "finished_at": None, "detail": None, "last_run_s": None}
_thread = None            # the running job's thread (tests join it)


def _dir():
    return os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))


def _result_path():
    return os.path.join(_dir(), "reroute-pi.json")


def _device(raw):
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:40]
    return s or None


def _next_course_index(nav, marks):
    """The navigator's next mark as a POSITION in the course — by the index it reports, checked
    against the name; else its seq; else the name only when it occurs once. A lapped course
    (Top → Bottom → Top) repeats names, so the first name match can be a lap behind."""
    nm = nav.get("next_mark") or {}
    name, idx, seq = nm.get("name"), nm.get("index"), nm.get("seq")
    if isinstance(idx, int) and 0 <= idx < len(marks) and marks[idx].get("name") == name:
        return idx
    if seq is not None:
        hit = [i for i, m in enumerate(marks) if m.get("seq") == seq and m.get("name") == name]
        if len(hit) == 1:
            return hit[0]
    hit = [i for i, m in enumerate(marks) if m.get("name") == name]
    return hit[0] if len(hit) == 1 else None


def _remaining_marks(nav, marks):
    """The marks from the next one through the finish (inclusive) — what's left to sail. Each
    carries its `course_index` so the re-route can say WHICH occurrence of a lapped mark it
    starts at."""
    idx = _next_course_index(nav, marks)
    if idx is None:
        idx = 0
    return [dict(m, course_index=i) for i, m in enumerate(marks)][idx:]


def _avoid_disks(bundle):
    """The frozen island obstacle homework → the (lat,lon,radius_nm) disks route_leg prunes against.
    Empty when the bundle carries no obstacles (the router then routes open-water, honestly flagged)."""
    isls = ((bundle or {}).get("obstacles") or {}).get("islands") or []
    return [(i["lat"], i["lon"], i.get("radius_nm") or 0.5)
            for i in isls if i.get("lat") is not None and i.get("lon") is not None]


def _parse_zones(bundle):
    """The frozen exclusion/TSS/hazard ZONES → (extra disks, polygon rings) for the router. Mirrors
    the lab's `geo._zone_rings` geometry contract: a GeoJSON polygon ({"coordinates": [[[lon,lat]..]]}
    or a bare ring), a bbox ({north,south,west,east}), or a circle ({center:[lat,lon], radius_nm})."""
    zdisks, polys = [], []
    for z in (((bundle or {}).get("obstacles") or {}).get("zones") or []):
        g = z.get("geometry") or {}
        if not g:
            continue
        try:
            if g.get("center") and g.get("radius_nm"):
                c = g["center"]                       # [lat, lon]
                if len(c) == 2:
                    zdisks.append((float(c[0]), float(c[1]), float(g["radius_nm"])))
            elif "coordinates" in g:
                coords = g["coordinates"]
                if coords and isinstance(coords[0][0], (int, float)):
                    coords = [coords]                 # bare ring → one-ring polygon
                for ring in coords:
                    if ring:
                        polys.append([(float(x), float(y)) for x, y in ring])
            elif all(k in g for k in ("north", "south", "west", "east")):
                n, s, w, e = g["north"], g["south"], g["west"], g["east"]
                polys.append([(w, s), (e, s), (e, n), (w, n)])
        except (TypeError, ValueError, KeyError):
            continue                                  # a malformed zone is skipped, not fatal
    return zdisks, polys


def _vs_playbook(bundle, fresh_path):
    """How far the fresh route departs from the active frozen variant's optimal track — so the crew
    sees how off-script it is. Reuses deviation's polyline projection (max/mean cross-track of the
    fresh path points onto the frozen variant polyline)."""
    try:
        if not bundle:
            return {"available": False, "note": "No gameplan to compare."}
        v = deviation._pick_variant(bundle, None)
        frozen = ((v.get("route") or {}).get("path")) if v else None
        if not frozen or len(frozen) < 2:
            return {"available": False, "note": "Gameplan option has no track."}
        xtes = [fix["perp"] for p in fresh_path
                if (fix := deviation._project(p["lat"], p["lon"], frozen))]
        if not xtes:
            return {"available": False, "note": "Could not compare."}
        return {"available": True, "variant": str(v.get("id")),
                "max_divergence_nm": round(max(xtes), 2),
                "mean_divergence_nm": round(sum(xtes) / len(xtes), 2)}
    except Exception:
        return {"available": False, "note": "Comparison failed."}


def course_avoid(nav_route, marks, bundle):
    """Every obstacle aboard for this course: the playbook bundle's islands + zones AND the
    deployed definition's (when it is the course the navigator holds: orinroute._definition_for),
    the same homework the Orin routes around. → (disks, polygons, definition, course_id,
    number of bundle zones). The re-route and the GPS export both use this, so a waypoint thinned
    for the plotter is checked against exactly what the route was computed around. Caller holds
    orinroute.COURSE_LOCK."""
    zdisks, polys = _parse_zones(bundle)
    disks = _avoid_disks(bundle) + zdisks                 # islands + circular zones = disks
    n_bundle_zones = len(zdisks) + len(polys)
    defn, cid = orinroute._definition_for(nav_route, marks) if nav_route else (None, None)
    if defn:
        from shared.race_def import course_obstacles
        dob = {"obstacles": course_obstacles(defn, cid)}
        dz, dp = _parse_zones(dob)
        disks = disks + _avoid_disks(dob) + dz
        polys = polys + dp
    return disks, polys, defn, cid, n_bundle_zones


def _inputs(route=None):
    """What a run needs, read once: (inputs dict, None) or (None, crew-facing reason)."""
    nav = NAV.get_navigator(route)
    if not nav.get("available"):
        return None, nav.get("note", "No position yet.")
    marks = NAV._marks(nav["route"])
    if not marks:
        return None, "No course loaded."
    s = NAV._latest()
    slat, slon = s.get("lat"), s.get("lon")
    if slat is None or slon is None:
        return None, "No position yet."
    bundle = deviation._load_playbook()
    avoid, polys, defn, cid, n_bundle_zones = course_avoid(nav["route"], marks, bundle)
    warnings = []
    if defn:
        course = next((c for c in defn.get("courses") or [] if c.get("id") == cid), None) or {}
        if any(m.get("type") == "island" and m.get("rounding") in ("port", "starboard")
               for m in course.get("marks") or []):
            warnings.append(PI_NO_ROUNDING)
    elif not n_bundle_zones:
        warnings.append(orinroute.NO_ZONES)               # never route silently without zones
    else:
        warnings.append(NO_ROUNDING)
    return {"nav": nav, "remaining": _remaining_marks(nav, marks), "slat": slat, "slon": slon,
            "live": (s.get("tws"), s.get("twd")), "bundle": bundle,
            "avoid": avoid, "polys": polys, "warnings": warnings,
            "course_hash": orinroute.course_hash(nav["route"], marks)}, None


def compute(route=None):
    """Compute one fresh route now, synchronously, and return it. Only the button's job (and
    tests) call this; it stores nothing."""
    with orinroute.COURSE_LOCK:
        inp, why = _inputs(route)
    if inp is None:
        return {"available": False, "note": why}
    out = _compute(inp["nav"], inp["remaining"], inp["slat"], inp["slon"], inp["live"],
                   inp["bundle"], inp["avoid"], inp["polys"])
    return _with_warnings(out, inp)


def _with_warnings(out, inp):
    if out.get("available") and inp.get("warnings"):
        out = dict(out, warnings=list(inp["warnings"]) + list(out.get("warnings") or []))
        if orinroute.NO_ZONES in out["warnings"]:
            out["no_zones"] = True
    return out


# ---- the stored route -------------------------------------------------------------------------
def _read_stored():
    try:
        with open(_result_path()) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _store(result):
    os.makedirs(_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(), suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(result, separators=(",", ":")))
        os.replace(tmp, _result_path())
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_reoptimize(route=None):
    """The last route the Pi made (full, with its track), with its age. NEVER computes one: that
    only happens on the crew's button (`run`). `available: False` until a run has finished."""
    r = _read_stored()
    running = _job["state"] == "running"
    if not r or not r.get("available"):
        return {"available": False, "note": NO_ROUTE_YET, "running": running}
    r = dict(r)
    fin = r.get("computed_at")
    r["age_s"] = max(0, int(time.time() - fin)) if fin else None
    r["running"] = running
    return r


def compact(r):
    """A route without its heavy arrays (track, legs' detail), for status polls."""
    if not r:
        return None
    return {k: v for k, v in r.items() if k not in ("path", "leg_end_idx")}


def status():
    """The Pi re-route's state for the iPad: idle|running|done|error, progress while running, the
    reason a run failed, and the last route (compact)."""
    with _LOCK:
        j = dict(_job)
    out = {"state": j["state"], "job_id": j["job_id"], "asked_by": j["asked_by"],
           "detail": j["detail"] if j["state"] == "error" else None}
    if j["state"] == "running" and j["started_at"]:
        el = max(0.0, time.time() - j["started_at"])
        exp = float(j["last_run_s"] or EXPECTED_S)
        out["progress"] = {"elapsed_s": round(el), "expected_s": round(exp),
                           "pct": min(95, int(100 * el / max(1.0, exp)))}
    last = get_reoptimize()
    out["result"] = compact(last) if last.get("available") else None
    return out


# ---- the button -------------------------------------------------------------------------------
def run(route=None, device=None):
    """The crew pressed Re-route now and chose the Pi (the Orin can't be reached) → (http status,
    body). 202 started, 409 busy or nothing to route. The route is computed in a background thread;
    `status()` reports it."""
    global _thread
    dev = _device(device)
    # the course lock first (the order /reroute/run uses too): a /course/load can't land between
    # reading the next mark and reading the marks, so the next-mark index, the marks and the
    # course hash stamped on the route all come from one course
    with orinroute.COURSE_LOCK, _LOCK:
        if _job["state"] == "running":
            el = round(time.time() - (_job["started_at"] or time.time()))
            reroutelog.record("pressed", "pi", device=dev, outcome="busy")
            return 409, {"ok": False, "busy": True, "engine": "pi", "detail": BUSY,
                         "progress": {"elapsed_s": el}}
        inp, why = _inputs(route)
        if inp is None:
            reroutelog.record("pressed", "pi", device=dev, outcome="refused", detail=why)
            return 409, {"ok": False, "engine": "pi", "detail": why}
        job_id = uuid.uuid4().hex[:12]
        _job.update(state="running", job_id=job_id, asked_by=dev, started_at=time.time(),
                    finished_at=None, detail=None)
    reroutelog.record("pressed", "pi", device=dev, outcome="started", job_id=job_id)
    _thread = threading.Thread(target=_work, args=(job_id, dev, inp), name="reoptimize-run",
                               daemon=True)
    _thread.start()
    return 202, {"ok": True, "engine": "pi", "job_id": job_id, "state": "running"}


def _work(job_id, dev, inp):
    t0 = time.time()
    asked_at = int(t0)
    try:
        out = _compute(inp["nav"], inp["remaining"], inp["slat"], inp["slon"], inp["live"],
                       inp["bundle"], inp["avoid"], inp["polys"])
    except Exception as exc:  # noqa: BLE001 — reported to the crew; the details go to the log
        print(f"[reoptimize] run failed: {type(exc).__name__}: {exc}", flush=True)
        out = {"available": False, "note": FAILED}
    out = _with_warnings(out, inp)
    run_s = round(time.time() - t0, 1)
    now = int(time.time())
    if out.get("available"):
        out = dict(out, job_id=job_id, asked_by=dev, asked_at=asked_at, computed_at=now,
                   run_s=run_s, source="pi", label="Off the playbook",
                   course_hash=inp.get("course_hash"),
                   computed_from={"lat": round(inp["slat"], 5), "lon": round(inp["slon"], 5)})
        try:
            _store(out)
        except OSError as exc:
            print(f"[reoptimize] could not store the route: {exc}", flush=True)
            out = {"available": False, "note": "Couldn't save the route. Try again."}
    if out.get("available"):
        reroutelog.record("done", "pi", once_key=job_id, job_id=job_id, asked_by=dev,
                          asked_at=asked_at, finished_at=now, run_s=run_s,
                          weather={"models": [{"model": "open-meteo"}],
                                   "source": out.get("wind_source")},
                          eta_min=out.get("eta_min"), sailed_nm=out.get("sailed_nm"),
                          tacks=out.get("tacks"), marks=out.get("marks"),
                          warnings=(out.get("warnings") or [])[:4],
                          no_zones=out.get("no_zones"))
        state, detail = "done", None
    else:
        detail = str(out.get("note") or FAILED)[:120]
        reroutelog.record("failed", "pi", once_key=job_id, job_id=job_id, asked_by=dev,
                          detail=detail, run_s=run_s)
        state = "error"
    with _LOCK:
        if _job["job_id"] == job_id:
            _job.update(state=state, finished_at=now, detail=detail,
                        last_run_s=run_s if state == "done" else _job["last_run_s"])


def _compute(nav, remaining, slat, slon, live, bundle, avoid, polys):
    """The heavy isochrone chain (~15 s for a long course on a Pi 4)."""
    wind, use_fcst = routing.make_wind_fn(slat, slon, live)
    t0 = t = time.time()
    cur = (slat, slon)
    full_path = [{"lat": round(slat, 5), "lon": round(slon, 5)}]
    all_legs, leg_summ = [], []
    leg_end_idx = []          # full_path index where each leg reaches its mark (gpsout's mark vertices)
    for m in remaining:
        # which sail this leg wants: the direct-course TWA at the leg midpoint → the onboard sail
        # model (same crossovers the sail dial uses). Clamps to the up sail on a beat, like the dial.
        mid_lat, mid_lon = (cur[0] + m["lat"]) / 2, (cur[1] + m["lon"]) / 2
        tws_l, twd_l = wind(mid_lat, mid_lon, t)
        sail = None
        if tws_l:
            twa_l = abs(NAV._wrap180(NAV._bearing(cur[0], cur[1], m["lat"], m["lon"]) - twd_l))
            sail = (sails.get_sail_advice(tws_l, twa_l) or {}).get("optimal_sail")
        leg = routing.route_leg(cur[0], cur[1], m["lat"], m["lon"], wind, t, live[1] or 0,
                                avoid=avoid, avoid_polys=polys)
        if not leg.get("reached", True):
            # #31: the leg never closed on its mark (search horizon out on a long light-air leg, or
            # every heading blocked by the island/zone homework). Chaining the next leg from the
            # mark would jump the path there for free and understate the ETA — so refuse by name.
            done = [x["mark"] for x in leg_summ]
            out = {"available": False, "off_playbook": True, "route": nav["route"],
                   "degraded": True, "complete": False,
                   "incomplete": {"to": m["name"], "short_nm": leg.get("short_nm"),
                                  "routed_marks": done,
                                  "horizon_h": round(routing.MAX_STEPS * routing.DT_H, 1)},
                   "wind_source": "forecast" if use_fcst else "live wind (no forecast)",
                   # Why: no clear path within the routing.MAX_STEPS * DT_H search horizon, or the
                   # obstacle homework blocks it. No route offered rather than a wrong one.
                   "note": (f"No new route. Couldn't reach {m['name']} "
                            f"({leg.get('short_nm')} nm short).")}
            return out
        full_path += leg["path"][1:]          # skip the duplicated leg-start point
        leg_end_idx.append(len(full_path) - 1)
        all_legs += leg["legs"]
        leg_summ.append({"mark": m["name"], "eta_min": round((leg["reached_t"] - t) / 60, 1),
                         "sailed_nm": round(leg["sailed_nm"], 2), "sail": sail})
        t = leg["reached_t"]
        cur = (m["lat"], m["lon"])

    # the peel sequence across the remaining course (consecutive de-dup), for the crew
    sail_plan = []
    for lg in leg_summ:
        if lg.get("sail") and (not sail_plan or sail_plan[-1] != lg["sail"]):
            sail_plan.append(lg["sail"])

    tacks = sum(1 for a, b in zip(all_legs, all_legs[1:]) if a["tack"] != b["tack"])
    sailed = sum(NAV._hav_nm(full_path[i]["lat"], full_path[i]["lon"],
                             full_path[i + 1]["lat"], full_path[i + 1]["lon"])
                 for i in range(len(full_path) - 1))

    out = {
        "available": True, "off_playbook": True, "route": nav["route"],
        "wind_source": "forecast" if use_fcst else "live wind (no forecast)",
        "marks": [m["name"] for m in remaining], "legs": leg_summ,
        "sail_plan": sail_plan,
        "mark_idx": [m.get("course_index") for m in remaining],   # course positions (laps)
        "eta_min": round((t - t0) / 60, 1), "tacks": tacks, "sailed_nm": round(sailed, 2),
        # the routing's own clock: it departs at t0 (when the run read the boat) and arrives at t,
        # so the finish time doesn't drift by however long the computation took
        "depart_epoch": int(round(t0)), "finish_eta": int(round(t)),
        "recommended_heading": all_legs[0]["hdg"] if all_legs else None,
        "first_tack": all_legs[0]["tack"] if all_legs else None,
        "path": full_path,
        "leg_end_idx": leg_end_idx,           # one per remaining mark, in order
        "avoids_islands": len(avoid), "avoids_zones": len(polys),
        "vs_playbook": _vs_playbook(bundle, full_path),
        # Fresh route on the boat's own polars, through the forecast (or measured wind when there
        # is none), from here through the remaining marks. Off the frozen playbook; legal in-race.
        "note": ("New route from here, off the gameplan"
                 + (", avoiding " + " and ".join(
                     ([f"{len(avoid)} island(s)"] if avoid else [])
                     + ([f"{len(polys)} no-go zone(s)"] if polys else []))
                    + "."
                    if (avoid or polys) else
                    ". No obstacles loaded, check the chart.")),
    }
    return out
