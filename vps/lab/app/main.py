"""C4 Performance Lab — cloud service (Lab-0).

The browser-based, between-races side of the project: PREP (ingest race docs → RaceDefinition →
review → gameplan → lock-in & deploy) and DEBRIEF (learning loop). Shared team login; the static
shell is public, the /api/* data routes are gated. This service both serves the Lab web app and
exposes the race-library API. The race-day ONBOARD console is a separate, deliberately-simple
surface (pi/console) — not this.

Slice 1: shared-password auth + the race library (list / get / validate) + the web shell. Next:
the dual-input ingestion (auto-find URL / paste-link / upload PDF → Opus extraction → review).
"""
import datetime
import json
import os
import re

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from shared import race_def, boat_profile
from . import auth, store, extract, boats, labstate

INGESTED_DIR = os.environ.get("INGESTED_DIR", "/srv/ingested")

app = FastAPI(title="C4 Performance Lab", version="0.1.0")


@app.middleware("http")
async def gate(request: Request, call_next):
    """Gate /api/* behind the shared team token; the static shell + non-API paths stay open."""
    p = request.url.path
    if request.method == "OPTIONS" or p in auth.OPEN_PATHS or not p.startswith("/api/"):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    tok = header[7:] if header.lower().startswith("bearer ") else None
    if not auth.verify_token(tok):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.post("/api/auth")
async def authenticate(body: dict):
    if not auth.check_password((body or {}).get("password")):
        return JSONResponse({"detail": "invalid password"}, status_code=401)
    return {"token": auth.issue_token()}


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "c4-performance-lab"}


@app.get("/api/races")
def races():
    return {"races": store.list_races()}


@app.get("/api/races/{rid}")
def race(rid: str):
    d = store.get_race(rid)
    return d if d else JSONResponse({"detail": "not found"}, status_code=404)


@app.get("/api/races/{rid}/validate")
def validate(rid: str):
    from shared.race_def import validate as _validate
    d = store.get_race(rid)
    if not d:
        return JSONResponse({"detail": "not found"}, status_code=404)
    errors, warnings = _validate(d)
    return {"errors": errors, "warnings": warnings}


def _today():
    return datetime.date.today().isoformat()


def _drafted(definition):
    """Validate a draft RaceDefinition and return it with its validation for the review step."""
    errors, warnings = race_def.validate(definition)
    return {"definition": definition, "errors": errors, "warnings": warnings}


@app.post("/api/ingest/discover")
async def ingest_discover(body: dict):
    """Auto-find: scrape a race page for candidate document PDF links."""
    url = (body or {}).get("url", "").strip()
    if not url:
        return JSONResponse({"detail": "url required"}, status_code=400)
    try:
        return {"candidates": await run_in_threadpool(extract.discover_pdfs, url)}
    except Exception as exc:
        return JSONResponse({"detail": f"discover failed: {exc}"}, status_code=502)


@app.post("/api/ingest")
async def ingest_urls(body: dict):
    """Ingest from one or more document URLs (auto-find selections or pasted direct links)."""
    urls = [u.strip() for u in (body or {}).get("urls", []) if u and u.strip()]
    if not urls:
        return JSONResponse({"detail": "urls required"}, status_code=400)
    try:
        docs, sources = [], []
        for u in urls:
            label, text = await run_in_threadpool(extract.text_from_url, u)
            docs.append((label, text))
            sources.append({"label": label, "url": u, "retrieved": _today()})
        definition = await run_in_threadpool(extract.extract_race_definition, docs, _today())
        definition.setdefault("provenance", {}).setdefault("sources", [])
        definition["provenance"]["sources"] = sources + definition["provenance"]["sources"]
        return _drafted(definition)
    except Exception as exc:
        return JSONResponse({"detail": f"ingest failed: {exc}"}, status_code=502)


@app.post("/api/ingest/upload")
async def ingest_upload(files: list[UploadFile] = File(...)):
    """Ingest from uploaded PDF(s) — for JS-rendered race hubs a crawler can't reach."""
    try:
        docs, sources = [], []
        for f in files:
            raw = await f.read()
            docs.append((f.filename, await run_in_threadpool(extract.pdf_text, raw)))
            sources.append({"label": f.filename, "url": "(uploaded)", "retrieved": _today()})
        if not docs:
            return JSONResponse({"detail": "no files"}, status_code=400)
        definition = await run_in_threadpool(extract.extract_race_definition, docs, _today())
        definition.setdefault("provenance", {}).setdefault("sources", [])
        definition["provenance"]["sources"] = sources + definition["provenance"]["sources"]
        return _drafted(definition)
    except Exception as exc:
        return JSONResponse({"detail": f"ingest failed: {exc}"}, status_code=502)


@app.post("/api/geocode")
async def geocode_ep(body: dict):
    """Place name → candidate coordinates (Nominatim), for the human to confirm during review."""
    q = (body or {}).get("q", "").strip()
    if not q:
        return JSONResponse({"detail": "q required"}, status_code=400)
    try:
        return {"results": await run_in_threadpool(extract.geocode, q)}
    except Exception as exc:
        return JSONResponse({"detail": f"geocode failed: {exc}"}, status_code=502)


@app.get("/api/models")
def models():
    """Available weather models for the optimizer (Lab-1)."""
    from .wind import available_models
    from .wind.models import DEFAULT_MODELS
    return {"models": available_models(), "default": list(DEFAULT_MODELS)}


def chart_source():
    """The active chart/obstacle source — labstate override, else the COASTLINE_SOURCE env default."""
    from .geo import obstacles
    return (labstate.get("coastline_source") or obstacles.COASTLINE_SOURCE or "natural_earth").lower()


def _run_optimize(definition, course_id, start_epoch, model_names, ensemble_members, avoid=True):
    """Blocking: build the multi-model wind field, route the course, write the briefing."""
    from .wind import build_windfield
    from . import optimizer
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "course has no geocoded marks — review Course & Marks"}
    hours = optimizer.estimate_hours(definition, course_id)
    t_end = start_epoch + hours * 3600
    log = []
    wf = build_windfield(bbox, start_epoch, t_end, models=model_names,
                         ensemble_members=ensemble_members, on_progress=log.append)
    if not wf.loaded:
        return {"available": False, "note": "no weather model data could be loaded (not yet "
                "posted, or no egress)", "windfield": wf.status(), "log": log}
    result = optimizer.optimize_course(definition, course_id, start_epoch, wf, avoid=avoid,
                                       source=chart_source(),
                                       safety_depth=boats.active_safety_depth_m())
    result["briefing"] = optimizer.briefing(result, definition.get("name", ""))
    result["boat"] = boat_profile.summary(boats.active_boat()) if boats.active_boat() else None
    result["wind_grid"] = _wind_grid(wf, bbox, start_epoch, result.get("finish_epoch", t_end))
    result["log"] = log
    return result


WIND_GRID_STEP_H = float(os.environ.get("WIND_GRID_STEP_H", "1"))     # forecast frame cadence (hours)
WIND_GRID_MAX_FRAMES = int(os.environ.get("WIND_GRID_MAX_FRAMES", "72"))
WIND_GRID_CELLS = int(os.environ.get("WIND_GRID_CELLS", "16"))


def _wind_grid(wf, bbox, start_epoch, finish_epoch, n_cells=WIND_GRID_CELLS):
    """Multi-time wind-field grid for the map overlay + forecast time slider ([C]).

    Sampled at HOURLY (`WIND_GRID_STEP_H`) steps across the route window (capped at
    `WIND_GRID_MAX_FRAMES`); ~`n_cells` cells across the bbox. The slider scrubs the frames + moves
    the boat marker; each point carries confidence (model agreement) for the fuzzy-adherence shading."""
    n, s, w, e = bbox
    step = max(0.05, max(n - s, e - w) / n_cells)
    span = max(1.0, float(finish_epoch) - float(start_epoch))
    step_s = max(900.0, WIND_GRID_STEP_H * 3600.0)               # >= 15 min between frames
    n_times = min(WIND_GRID_MAX_FRAMES, max(2, int(span / step_s) + 1))
    times = [round(start_epoch + min(span, i * step_s)) for i in range(n_times)]
    if times[-1] < round(start_epoch + span):
        times.append(round(start_epoch + span))                  # always include the finish time
    frames = [wf.sample_grid(t, step, bbox) for t in times]
    return {"step_deg": round(step, 3), "step_h": WIND_GRID_STEP_H, "bbox": [n, s, w, e],
            "times": times, "frames": frames}


@app.post("/api/optimize")
async def optimize(body: dict):
    """Lab-1: run the multi-model GRIB optimizer over a race course → one route + briefing."""
    body = body or {}
    rid = body.get("race_id")
    d = store.get_race(rid) if rid else None
    if not d:
        return JSONResponse({"detail": "unknown race_id"}, status_code=404)
    course_id = body.get("course_id")
    start_epoch = float(body.get("start_epoch") or datetime.datetime.now(
        datetime.timezone.utc).timestamp())
    from .wind.models import DEFAULT_MODELS, MODELS
    models_req = body.get("models") or list(DEFAULT_MODELS)
    model_names = [m for m in models_req if m in MODELS] or list(DEFAULT_MODELS)
    ens = int(body.get("ensemble_members") or 0)
    avoid = body.get("avoid_land", True)
    try:
        return await run_in_threadpool(_run_optimize, d, course_id, start_epoch, model_names, ens,
                                       avoid)
    except Exception as exc:
        return JSONResponse({"detail": f"optimize failed: {exc}"}, status_code=500)


def _run_enc_prep(definition, course_id):
    """Blocking: download + GDAL-extract the NOAA ENC cells covering a course bbox (cache warm-up)."""
    from . import optimizer
    from .geo import enc, obstacles
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"ok": False, "note": "course has no geocoded marks — review Course & Marks"}
    log = []
    depth = boats.active_safety_depth_m()
    manifest = enc.ensure_bbox(bbox, on_progress=log.append)
    layers = enc.layers_in_bbox(bbox, safety_depth_m=depth)
    return {"ok": True, "bbox": bbox, "cells": list(manifest.keys()),
            "safety_depth_m": round(depth, 2),
            "polys": {k: len(v) for k, v in layers.items()}, "log": log}


@app.post("/api/enc/prep")
async def enc_prep(body: dict):
    """Warm the NOAA ENC cache for a course bbox (download cells + ogr2ogr → cached GeoJSON).

    Optional: the optimizer auto-preps on first run, but this lets the user pre-warm with progress
    and confirm which cells/layers loaded. Requires COASTLINE_SOURCE=enc to take effect in routing."""
    body = body or {}
    rid = body.get("race_id")
    d = store.get_race(rid) if rid else None
    if not d:
        return JSONResponse({"detail": "unknown race_id"}, status_code=404)
    try:
        return await run_in_threadpool(_run_enc_prep, d, body.get("course_id"))
    except Exception as exc:
        return JSONResponse({"detail": f"enc prep failed: {exc}"}, status_code=500)


# --- BoatProfile ([B]): boats library + the active boat + the chart source -----
@app.get("/api/boats")
def list_boats():
    """All boat profiles (summaries with feet conveniences) + the active boat id + chart settings."""
    return {"boats": boats.list_boats(), "active": boats.active_id(),
            "chart_source": chart_source()}


@app.post("/api/boats")
def save_boat(body: dict):
    """Create/update a boat profile. Draft may be sent as `draft_ft` (US convention) — stored as m.

    The optimizer reads the ACTIVE boat's draft as the ENC depth no-go, so editing draft here changes
    the route (deeper boat → shoals block more water)."""
    body = body or {}
    if body.get("draft_ft") is not None and body.get("draft_m") is None:
        body["draft_m"] = round(boat_profile.ft_to_m(body.pop("draft_ft")), 4)
    if body.get("safety_margin_ft") is not None and body.get("safety_margin_m") is None:
        body["safety_margin_m"] = round(boat_profile.ft_to_m(body.pop("safety_margin_ft")), 4)
    errs, warns = boat_profile.validate(body)
    if errs:
        return JSONResponse({"detail": "invalid boat profile", "errors": errs}, status_code=400)
    try:
        saved = boats.save_boat(body)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"boat": saved, "summary": boat_profile.summary(saved), "warnings": warns}


@app.get("/api/boats/active")        # declared before /api/boats/{bid} so 'active' isn't read as an id
def get_active_boat():
    b = boats.active_boat()
    return {"active": boats.active_id(),
            "summary": boat_profile.summary(b) if b else None,
            "chart_source": chart_source()}


@app.post("/api/boats/active")
def set_active_boat(body: dict):
    """Select the active boat and/or the chart source (natural_earth | enc). Both Lab-wide."""
    body = body or {}
    out = {}
    bid = body.get("boat_id") or body.get("active")
    if bid:
        try:
            out["active"] = boats.set_active(bid)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
    src = body.get("chart_source")
    if src is not None:
        src = str(src).strip().lower()
        if src not in ("natural_earth", "enc"):
            return JSONResponse({"detail": "chart_source must be natural_earth or enc"},
                                status_code=400)
        out["chart_source"] = labstate.set("coastline_source", src)
    out.setdefault("active", boats.active_id())
    out.setdefault("chart_source", chart_source())
    b = boats.active_boat()
    out["summary"] = boat_profile.summary(b) if b else None
    return out


@app.get("/api/boats/{bid}")
def get_boat(bid: str):
    b = boats.get_boat(bid)
    if not b:
        return JSONResponse({"detail": "unknown boat_id"}, status_code=404)
    errs, warns = boat_profile.validate(b)
    return {"boat": b, "summary": boat_profile.summary(b), "errors": errs, "warnings": warns}


@app.post("/api/playbook")
async def playbook(body: dict):
    """Lab-2: fan the optimizer across per-model forecast scenarios → strategic variants
    (which side of the first beat each model favors) with the agreement distribution."""
    body = body or {}
    rid = body.get("race_id")
    d = store.get_race(rid) if rid else None
    if not d:
        return JSONResponse({"detail": "unknown race_id"}, status_code=404)
    course_id = body.get("course_id")
    start_epoch = float(body.get("start_epoch") or datetime.datetime.now(
        datetime.timezone.utc).timestamp())
    from .wind.models import DEFAULT_MODELS, MODELS
    models_req = body.get("models") or list(DEFAULT_MODELS)
    model_names = [m for m in models_req if m in MODELS] or list(DEFAULT_MODELS)
    ens = int(body.get("ensemble_members") or 0)
    from . import playbook as pb
    try:
        return await run_in_threadpool(pb.build_playbook, d, course_id, start_epoch,
                                       model_names, ens)
    except Exception as exc:
        return JSONResponse({"detail": f"playbook failed: {exc}"}, status_code=500)


@app.post("/api/races")
async def save_race(body: dict):
    """Save a (human-reviewed) RaceDefinition to the library. Errors don't block saving a draft —
    they're surfaced so the team finishes review — but a race_id is required."""
    definition = (body or {}).get("definition") or body
    rid = definition.get("race_id")
    if not rid:
        return JSONResponse({"detail": "definition.race_id required"}, status_code=400)
    rid = re.sub(r"[^a-z0-9_-]", "", str(rid).lower())
    os.makedirs(INGESTED_DIR, exist_ok=True)
    with open(os.path.join(INGESTED_DIR, f"{rid}.json"), "w") as f:
        json.dump(definition, f, indent=2)
    errors, warnings = race_def.validate(definition)
    return {"saved": True, "race_id": rid, "errors": errors, "warnings": warnings}


# The Lab web shell (static). Declared last so the /api routes match first; html=True serves
# index.html at "/". Client-side hash routing handles the section tabs.
app.mount("/", StaticFiles(directory=os.environ.get("WEB_DIR", "/srv/web"), html=True), name="web")
