"""Debrief replay + strip chart (#373, #374).

One recording of a race played back on a map: our track, the fleet's tracks, the course, and the
day's wind field underneath, all driven by one time cursor; under it a strip chart of the logged
channels with markers, mean/SD between them, and saved "tests" (a 2-5 min window kept with its
stats).

Where the data comes from — nothing new is recorded:
  - our track: the debrief's stored track (`track.load_track`) — the boat log, a GPX, or YB.
  - the fleet: the retro archive (`retrostore`), filled by Fleet retro from YB or a fleet file.
  - channels: the boat log via the agent's `/racelog/channels` (BSP, TWS, TWA, TWD, heel, ...).
    A GPX/YB track, or an unreachable boat log, falls back to what the track's fixes carry
    (position/SOG/COG, plus STW/TWS/TWA on a stored boat-log track) and says so.
  - wind: built on request over the track's area and hours (the judge's oracle wind sources),
    sampled into the same grid the Gameplan map draws, and kept next to the track.

Faulty stretches: the trust sweep's danger intervals ride with a boat-log track. Stats leave every
sample inside one out, the same rule the debrief scorer uses, and count how many were left out.

Stored per recording at `<TRACK_DIR>/_replay_<race>[__<recording>].json`: {wind_grid, tests}.
"""
import math
import os
import threading
import time
import uuid

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from shared import polar as SPOL
from shared import race_def

from . import atomicio, track

router = APIRouter()

MAX_OWN_FIXES = int(os.environ.get("REPLAY_MAX_OWN_FIXES", "6000"))
MAX_FLEET_FIXES = int(os.environ.get("REPLAY_MAX_FLEET_FIXES", "1500"))
MAX_FLEET_BOATS = int(os.environ.get("REPLAY_MAX_FLEET_BOATS", "150"))
MAX_CHANNEL_POINTS = 6000
TEST_MIN_S, TEST_MAX_S = 120, 300          # a "test" is a 2-5 min window (#374)
_LOCK = threading.Lock()

# channel -> stats kind: "lin" plain mean/SD; "ang" circular (directions, signed angles)
STAT_KIND = {"bsp": "lin", "sog": "lin", "tws": "lin", "aws": "lin", "heel": "lin", "vmg": "lin",
             "pct_polar": "lin", "twa": "ang", "awa": "ang", "twd": "ang", "cog": "ang",
             "hdg": "ang"}
CHANNELS = tuple(STAT_KIND)


# ---- the per-recording store ----------------------------------------------------------------
def _path(race_id, recording=None):
    rid = track._rid(race_id)
    base = os.path.join(track.TRACK_DIR, f"_replay_{rid}")
    return base + (".json" if recording is None else f"__{int(float(recording))}.json")


def _read(race_id, recording):
    d = track._read(_path(race_id, recording))
    return d if isinstance(d, dict) else {}


def _write(race_id, recording, data):
    os.makedirs(track.TRACK_DIR, exist_ok=True)
    import json
    with atomicio.atomic_open(_path(race_id, recording), "w") as fh:
        json.dump(data, fh)


def _rec(recording):
    return None if recording in (None, "", "null") else float(recording)


# ---- tracks ---------------------------------------------------------------------------------
def thin(seq, cap):
    """Every n-th item so at most `cap` remain, always keeping the last."""
    if len(seq) <= cap:
        return list(seq)
    step = len(seq) / float(cap)
    out = [seq[int(i * step)] for i in range(cap - 1)]
    out.append(seq[-1])
    return out


def clock_offset(fixes, gun):
    """Seconds to add to each fix time to make it a wall clock: 0 for a wall clock, the gun for a
    race-relative (YB) track, None when it is relative and no gun is known."""
    ts = [f.get("t") for f in fixes or () if f.get("t") is not None]
    if not ts:
        return None
    if track.absolute_clock(ts[0]):
        return 0.0
    return float(gun) if gun else None


def own_track(t, gun=None):
    """Our stored track as replay fixes [[t, lat, lon, sog, cog]] on a wall clock, or a note."""
    fixes = [f for f in (t or {}).get("fixes") or ()
             if f.get("lat") is not None and f.get("lon") is not None and f.get("t") is not None]
    if len(fixes) < 2:
        return {"available": False, "note": "No track for this recording."}
    off = clock_offset(fixes, gun)
    if off is None:
        return {"available": False,
                "note": "This track's times start at zero and the race has no start time."}
    fixes = sorted(fixes, key=lambda f: f["t"])
    rows = [[round(f["t"] + off, 1), f["lat"], f["lon"], f.get("sog"), f.get("cog")]
            for f in thin(fixes, MAX_OWN_FIXES)]
    return {"available": True, "source": t.get("source"), "boat": t.get("boat"),
            "fixes": rows, "t0": rows[0][0], "t1": rows[-1][0]}


def danger_intervals(trust):
    """Every danger interval of the trust sweep, any channel, merged and sorted: [[a, b]]."""
    iv = sorted([float(a), float(b)] for segs in ((trust or {}).get("danger") or {}).values()
                for a, b in (segs or ()))
    out = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _same_boat(a, b):
    na = "".join(c for c in str(a or "").lower() if c.isalnum())
    nb = "".join(c for c in str(b or "").lower() if c.isalnum())
    return bool(na) and na == nb


def fleet_tracks(definition, race_id, t0=None, t1=None, own_name=None):
    """The fleet from the retro archive: {"boats": [{name, sail, own, fixes}], "note"}. Looks under
    the race's YB id first, then the race's own id (a fleet file is stored under it)."""
    from . import retro, retrostore as rs
    d = definition or {}
    keys = []
    try:
        yb = retro.setup_for(d).get("yb_id")
    except Exception:  # noqa: BLE001 — a bad tracker config must not break the replay
        yb = None
    for k in (yb, race_id):
        if k and k not in keys:
            keys.append(k)
    rid = next((k for k in keys if rs.get_race(k)), None)
    if not rid:
        return {"boats": [], "note": "No fleet loaded. Load it in Fleet retro below."}
    boats_out = []
    entries = rs.get_entries(rid)
    have = rs.teams_with_tracks(rid)
    gun = (rs.get_race(rid) or {}).get("start_epoch")
    pad = 3600.0
    for e in entries:
        if e["team_id"] not in have:
            continue
        fx = rs.get_track(rid, e["team_id"]) or []
        off = clock_offset(fx, e.get("start_epoch") or gun)
        if off is None:
            continue
        rows = [[round(f["t"] + off, 1), f["lat"], f["lon"]] for f in fx
                if f.get("t") is not None and f.get("lat") is not None and f.get("lon") is not None]
        if t0 is not None and t1 is not None:
            rows = [r for r in rows if t0 - pad <= r[0] <= t1 + pad]
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r[0])
        boats_out.append({"name": e.get("boat") or str(e["team_id"]), "sail": e.get("sail") or "",
                          "division": e.get("division") or "",
                          "own": _same_boat(e.get("boat"), own_name),
                          "fixes": thin(rows, MAX_FLEET_FIXES)})
    boats_out = boats_out[:MAX_FLEET_BOATS]
    note = "" if boats_out else "The fleet file has no tracks during this recording."
    return {"boats": boats_out, "note": note, "retro_id": rid}


def course_marks(definition):
    from . import retro
    d = definition or {}
    if not d.get("courses"):
        return []
    marks, _skipped, _cid = race_def.course_to_marks(d, retro.default_course(d))
    return [{"name": m[1], "lat": m[2], "lon": m[3]} for m in marks]


def build(race_id, recording=None):
    """Everything the replay map needs for one recording (no channels; they come per window)."""
    from . import store, racestart
    rec = _rec(recording)
    t = track.load_track(race_id, rec)
    if not t:
        return {"available": False, "note": "No track for this recording. Load or debrief it first."}
    d = (store.get_race(race_id) if race_id else None) or {}
    gun = (racestart.race_start(d) or {}).get("epoch") if d else None
    own = own_track(t, gun)
    if not own.get("available"):
        return own
    own_name = t.get("boat") or (d.get("boat") or {}).get("name") or (d.get("own") or {}).get("boat")
    fleet = fleet_tracks(d, race_id, own["t0"], own["t1"], own_name) if race_id else {
        "boats": [], "note": "Practice sail: no fleet."}
    saved = _read(race_id, rec)
    return {"available": True, "race_id": race_id, "recording": rec,
            "race_name": d.get("name"), "gun": gun,
            "own": own, "fleet": fleet, "marks": course_marks(d),
            "danger": danger_intervals(t.get("trust")),
            "trust_ran": bool((t.get("trust") or {}).get("available")),
            "full_channels": t.get("source") == "boatlog" and bool(t.get("window")),
            "wind_grid": saved.get("wind_grid"), "tests": saved.get("tests") or []}


# ---- channels -------------------------------------------------------------------------------
def channels_from_fixes(fixes, start=None, end=None, max_points=4000):
    """Strip-chart samples from a stored track's fixes: what a GPX/YB (position/SOG/COG) or a
    stored boat-log track (plus STW/TWS/TWA) carries. TWD here is COG + TWA (no heading)."""
    out = []
    for f in fixes or ():
        tt = f.get("t")
        if tt is None or (start is not None and tt < start) or (end is not None and tt > end):
            continue
        twa, cog = f.get("twa"), f.get("cog")
        if twa is not None and twa > 180:
            twa -= 360.0                    # the scorer wraps 0..360; the chart signs it
        twd = round((cog + twa) % 360.0, 1) if (cog is not None and twa is not None) else None
        out.append({"t": tt, "bsp": f.get("stw"), "sog": f.get("sog"), "cog": cog, "hdg": None,
                    "tws": f.get("tws"), "twa": twa, "twd": twd, "aws": None, "awa": None,
                    "heel": None})
    out.sort(key=lambda s: s["t"])
    return thin(out, max_points)


def enrich(samples, polars=None):
    """Adds VMG (BSP, else SOG, times cos TWA) and % of polar (BSP vs the polar target)."""
    for s in samples:
        spd = s.get("bsp") if s.get("bsp") is not None else s.get("sog")
        twa = s.get("twa")
        s["vmg"] = (round(spd * math.cos(math.radians(twa)), 2)
                    if spd is not None and twa is not None else None)
        pct = None
        if polars and s.get("bsp") is not None and s.get("tws") is not None and twa is not None:
            tgt = SPOL.target(polars, s["tws"], abs(twa))
            if tgt and tgt > 0.5:
                pct = round(100.0 * s["bsp"] / tgt, 1)
        s["pct_polar"] = pct
    return samples


def boat_polars(boat_id=None):
    from . import boats, polars as POL
    b = (boats.get_boat(boat_id) if boat_id else None) or boats.active_boat()
    return POL.polars_stw(boats.polar_for(b)) if b else []


def channels(race_id, recording, start=None, end=None, max_points=4000, boat_id=None,
             agent_get=None):
    """Samples over [start, end] (default: the whole recording). The boat log's full channels
    when the recording came off it; else the track's own fixes, said plainly."""
    rec = _rec(recording)
    t = track.load_track(race_id, rec)
    if not t:
        return {"available": False, "note": "No track for this recording."}
    max_points = max(50, min(MAX_CHANNEL_POINTS, int(max_points or 4000)))
    w = t.get("window") or {}
    a = float(start) if start is not None else w.get("start_ts")
    b = float(end) if end is not None else w.get("end_ts")
    src, note, step, samples = "track", "", None, None
    if t.get("source") == "boatlog" and a is not None and b is not None:
        if agent_get is None:
            from . import monitor
            agent_get = monitor.agent_json
        try:
            r = agent_get(f"/racelog/channels?start={a}&end={b}&max_points={max_points}",
                          timeout=max(30.0, 30.0 + 10.0 * (b - a) / 3600.0))
            if r.get("samples"):
                samples, step, src = r["samples"], r.get("step_s"), "boatlog"
            else:
                note = "The boat log has no channels here. Showing the stored track."
        except Exception as exc:  # noqa: BLE001
            note = (f"The boat log didn't answer ({type(exc).__name__}). Showing the stored "
                    "track: no heel or heading.")
    if samples is None:
        fixes = t.get("fixes") or []
        off = clock_offset(fixes, None) if fixes else 0.0
        if off is None:            # a relative (YB) track: anchor it like the map does
            from . import store, racestart
            d = store.get_race(race_id) if race_id else None
            off = (racestart.race_start(d) or {}).get("epoch") if d else None
        if off is None:
            return {"available": False, "note": "This track has no usable clock."}
        fixes = [{**f, "t": f["t"] + off} for f in fixes if f.get("t") is not None]
        samples = channels_from_fixes(fixes, a, b, max_points)
        if not note and t.get("source") != "boatlog":
            note = "GPX and tracker tracks carry position, SOG and COG only."
    enrich(samples, boat_polars(boat_id))
    present = [c for c in CHANNELS if any(s.get(c) is not None for s in samples)]
    return {"available": True, "source": src, "note": note, "step_s": step, "start": a, "end": b,
            "channels": present, "samples": samples}


# ---- stats ----------------------------------------------------------------------------------
def _in(t, iv, step=None):
    """Does the sample at `t` touch a danger interval? A boat-log sample is the average of the
    step that starts at `t`, so it counts as faulty when ANY of [t, t + step) is (Codex on #397:
    a 7 s step at 100 with a fault from 105 mixed healthy and faulty readings)."""
    span = float(step or 0)
    return any(a < t + span and t <= b if span else a <= t <= b for a, b in iv)


def window_stats(samples, start, end, danger=(), channels_=CHANNELS, step=None):
    """Mean and SD of each channel over [start, end], leaving out samples that touch a danger
    interval (`step`: seconds each sample averages, None for point samples).
    Angles: circular mean (TWA/AWA signed, directions 0..360) and circular SD in degrees."""
    a, b = min(start, end), max(start, end)
    inside = [s for s in samples if a <= s["t"] <= b]
    used = [s for s in inside if not _in(s["t"], danger, step)]
    out = {}
    for ch in channels_:
        vals = [s[ch] for s in used if s.get(ch) is not None]
        if not vals:
            continue
        if STAT_KIND.get(ch) == "ang":
            sx = sum(math.sin(math.radians(v)) for v in vals) / len(vals)
            cx = sum(math.cos(math.radians(v)) for v in vals) / len(vals)
            r = min(1.0, math.hypot(sx, cx))       # float noise can push it past 1
            mean = math.degrees(math.atan2(sx, cx))
            if ch not in ("twa", "awa"):
                mean %= 360.0
            sd = math.degrees(math.sqrt(-2.0 * math.log(r))) if r > 1e-9 else 180.0
        else:
            mean = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 0.0
        out[ch] = {"mean": round(mean, 2), "sd": round(sd, 2), "n": len(vals)}
    return {"start": a, "end": b, "seconds": round(b - a, 1), "n": len(inside),
            "left_out": len(inside) - len(used), "stats": out}


# ---- saved tests ----------------------------------------------------------------------------
def save_test(race_id, recording, name, start, end, boat_id=None, note="", agent_get=None):
    """Keep a 2-5 min window with its stats, next to the race's track. The stats are worked out
    here from the same channels the chart shows, so a saved test never depends on the browser."""
    name = str(name or "").strip()[:80]
    if not name:
        return {"ok": False, "note": "Give the test a name."}
    try:
        a, b = sorted((float(start), float(end)))
    except (TypeError, ValueError):
        return {"ok": False, "note": "Set two markers first."}
    if not TEST_MIN_S <= b - a <= TEST_MAX_S:
        return {"ok": False, "note": "A test is 2 to 5 minutes."}
    rec = _rec(recording)
    t = track.load_track(race_id, rec)
    if not t:
        return {"ok": False, "note": "No track for this recording."}
    ch = channels(race_id, rec, a, b, MAX_CHANNEL_POINTS, boat_id, agent_get)
    if not ch.get("available"):
        return {"ok": False, "note": ch.get("note")}
    st = window_stats(ch["samples"], a, b, danger_intervals(t.get("trust")), step=ch.get("step_s"))
    test = {"id": uuid.uuid4().hex[:10], "name": name, "note": str(note or "")[:300],
            "start": a, "end": b, "created_at": time.time(), "source": ch["source"],
            "boat_id": boat_id, **{k: st[k] for k in ("seconds", "n", "left_out", "stats")}}
    with _LOCK:
        data = _read(race_id, rec)
        data["tests"] = (data.get("tests") or []) + [test]
        _write(race_id, rec, data)
    return {"ok": True, "test": test}


def delete_test(race_id, recording, test_id):
    rec = _rec(recording)
    with _LOCK:
        data = _read(race_id, rec)
        before = data.get("tests") or []
        data["tests"] = [x for x in before if x.get("id") != test_id]
        if len(data["tests"]) == len(before):
            return False
        _write(race_id, rec, data)
    return True


# ---- the day's wind -------------------------------------------------------------------------
def build_wind(race_id, recording, on_progress=None, windfield=None):
    """The wind over the recording's area and hours, as the map's grid; stored with the replay.
    `windfield(bbox, t0, t1, log)` -> a WindField (test seam; default: the judge's oracle wind)."""
    from . import judge, routerun
    log = on_progress or (lambda *_: None)
    rec = _rec(recording)
    d = build(race_id, rec)
    if not d.get("available"):
        return {"ok": False, "note": d.get("note")}
    pts = d["own"]["fixes"]
    lats, lons = [p[1] for p in pts], [p[2] for p in pts]
    pad = 0.15
    bbox = (max(lats) + pad, min(lats) - pad, min(lons) - pad, max(lons) + pad)
    t0, t1 = d["own"]["t0"], d["own"]["t1"]
    if windfield is None:
        def windfield(bb, a, b, lg):
            return judge._oracle_windfield(bb, a, b, None, race_id or "practice", lg)[0]
    wf = windfield(bbox, t0, t1, log)
    if not getattr(wf, "loaded", False):
        return {"ok": False, "note": "No wind data for these hours."}
    log("sampling the wind grid…")
    grid = routerun.wind_grid(wf, bbox, t0, t1)
    with _LOCK:
        data = _read(race_id, rec)
        data["wind_grid"] = grid
        _write(race_id, rec, data)
    return {"ok": True, "frames": len(grid.get("times") or [])}


# ---- routes ---------------------------------------------------------------------------------
def _race(race_id):
    return race_id or None


@router.get("/api/debrief/replay")
async def replay_get(race_id: str = None, recording: str = None):
    return await run_in_threadpool(build, _race(race_id), recording)


@router.get("/api/debrief/replay/channels")
async def replay_channels(race_id: str = None, recording: str = None, start: float = None,
                          end: float = None, max_points: int = 4000, boat_id: str = None):
    return await run_in_threadpool(channels, _race(race_id), recording, start, end, max_points,
                                   boat_id)


@router.post("/api/debrief/replay/tests")
async def replay_save_test(body: dict):
    b = body or {}
    res = await run_in_threadpool(save_test, _race(b.get("race_id")), b.get("recording"),
                                  b.get("name"), b.get("start"), b.get("end"), b.get("boat_id"),
                                  b.get("note") or "")
    return res if res.get("ok") else JSONResponse(res, status_code=400)


@router.post("/api/debrief/replay/tests/delete")
async def replay_delete_test(body: dict):
    b = body or {}
    return {"ok": await run_in_threadpool(delete_test, _race(b.get("race_id")), b.get("recording"),
                                          b.get("id"))}


@router.post("/api/debrief/replay/wind")
async def replay_wind(body: dict):
    from . import jobs
    b = body or {}
    rid, rec = _race(b.get("race_id")), b.get("recording")
    return jobs.start("replaywind", lambda progress: build_wind(rid, rec, progress),
                      meta={"race_id": rid, "recording": rec})


@router.get("/api/debrief/replay/wind/status")
async def replay_wind_status(job_id: str = ""):
    """The wind job this caller started (`job_id` from the POST), never another user's run."""
    from . import jobs
    return jobs.status("replaywind", job_id=job_id)
