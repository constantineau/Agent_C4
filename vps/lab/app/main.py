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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from shared import race_def, boat_profile
from . import auth, store, extract, boats, labstate, feedback, pbstore, deploy, monitor, judge, track, learning

INGESTED_DIR = os.environ.get("INGESTED_DIR", "/srv/ingested")

app = FastAPI(title="C4 Performance Lab", version="0.1.0")

# CORS so the crew dashboard (c4.racertracer.net) can POST feedback to the Lab's issue endpoint
# cross-origin. Other /api routes stay team-token-gated regardless; this only adds the headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("FEEDBACK_ALLOW_ORIGINS",
        "https://c4.racertracer.net,https://lab.racertracer.net,http://localhost:8091").split(","),
    allow_methods=["POST", "OPTIONS"], allow_headers=["*"])


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


@app.get("/api/feedback")
def feedback_status():
    """Whether the feedback widget can file issues (the GitHub token is set) + the target repo."""
    return {"configured": feedback.configured(), "repo": feedback.REPO}


@app.post("/api/feedback")
async def submit_feedback(body: dict, request: Request):
    """Crew bug report / feature request → a GitHub issue on the monorepo. Open (no team login) so
    the c4 crew dashboard can use it too; validated + rate-limited server-side."""
    body = body or {}
    ctx = dict(body.get("context") or {})
    ctx.setdefault("userAgent", request.headers.get("user-agent", ""))
    ctx.setdefault("reportedAt", datetime.datetime.now(datetime.timezone.utc).isoformat())
    res = await run_in_threadpool(feedback.create_issue, body.get("type"), body.get("title"),
                                  body.get("body"), body.get("source", ""), ctx)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


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


def _active_jibs():
    """The active boat's upwind jib change-down bands (J1/J2/J3 by TWS) — drives the per-leg jib
    in the route/playbook. Empty if the boat doesn't carry them (→ the generic J1)."""
    b = boats.active_boat() or {}
    return b.get("jib_crossovers") or []


def _active_adjustments():
    """The active boat's human-APPROVED refined-polar overlay (Lab-4) — applied to the optimizer's
    polars. Empty until a learning proposal is approved (→ routes on the raw ORC cert)."""
    return boats.active_polar_adjustments()


def _run_optimize(definition, course_id, start_epoch, model_names, ensemble_members, avoid=True,
                  per_model=False, resolution="auto", use_waves=True):
    """Blocking: build the multi-model wind field, route the course, write the briefing."""
    from .wind import build_windfield
    from . import optimizer, current, wave
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
    cur = current.build_currentfield(bbox, start_epoch, t_end, on_progress=log.append)  # GLOFS (no-op until wired)
    # sea state (phase 1 seam) — opt-out per run; helm factor still applies (it's crew efficiency, not waves)
    waves = (wave.build_wavefield(bbox, start_epoch, t_end, on_progress=log.append)
             if use_waves else wave.ZeroWave())
    result = optimizer.optimize_course(definition, course_id, start_epoch, wf, avoid=avoid,
                                       source=chart_source(),
                                       safety_depth=boats.active_safety_depth_m(),
                                       jib_crossovers=_active_jibs(), per_model=per_model,
                                       resolution=resolution, cur=cur,
                                       waves=waves, helm_factor=boats.active_helm_factor(),
                                       polar_adjustments=_active_adjustments())
    result["current"] = cur.status()
    result["waves"] = waves.status()
    result["briefing"] = optimizer.briefing(result, definition.get("name", ""))
    result["boat"] = boat_profile.summary(boats.active_boat()) if boats.active_boat() else None
    wg = _wind_grid(wf, bbox, start_epoch, result.get("finish_epoch", t_end))
    result["wind_grid"] = wg
    if cur.status().get("loaded"):                       # current overlay on the SAME times as wind
        cg = _current_grid(cur, wg["bbox"], wg["times"], wg["step_deg"])
        if cg:                                           # only when some cell actually flows
            result["current_grid"] = cg
    if waves.status().get("loaded"):                     # sea-state heatmap on the SAME times as wind
        sg = _wave_grid(waves, wg["bbox"], wg["times"], wg["step_deg"])
        if sg:                                           # only when there's meaningful sea state
            result["wave_grid"] = sg
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


def _current_grid(cur, bbox, times, step):
    """Set/drift grid for the map's CURRENT overlay (routing fidelity 2d follow-up), on the SAME bbox +
    times as `_wind_grid` so one slider scrubs both. Returns None when nothing flows (drift < 0.05 kn
    everywhere) so a no-current race draws no overlay. Frames mirror the wind frames' lattice."""
    frames = [cur.sample_grid(t, step, bbox) for t in times]
    peak = max((c.get("drift", 0.0) for fr in frames for c in fr), default=0.0)
    if peak < 0.05:
        return None
    return {"step_deg": step, "bbox": bbox, "times": times, "frames": frames,
            "peak_drift_kn": round(peak, 2)}


WAVE_GRID_MIN_HS = float(os.environ.get("WAVE_GRID_MIN_HS", "0.25"))   # below this = effectively flat


def _wave_grid(waves, bbox, times, step):
    """Significant-wave-height grid for the map's SEA-STATE heatmap (realized-speed follow-up), on the
    SAME bbox + times as `_wind_grid` so one slider scrubs wind/current/waves together. Returns None
    when the sea is effectively flat (peak Hs < `WAVE_GRID_MIN_HS`) so a calm day draws no overlay."""
    frames = [waves.sample_grid(t, step, bbox) for t in times]
    peak = max((c.get("hs", 0.0) for fr in frames for c in fr), default=0.0)
    if peak < WAVE_GRID_MIN_HS:
        return None
    return {"step_deg": step, "bbox": bbox, "times": times, "frames": frames,
            "peak_hs_m": round(peak, 1)}


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
    per_model = bool(body.get("per_model"))
    resolution = body.get("resolution") or "auto"
    use_waves = body.get("use_waves", True)
    try:
        return await run_in_threadpool(_run_optimize, d, course_id, start_epoch, model_names, ens,
                                       avoid, per_model, resolution, use_waves)
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
                                       model_names, ens, jib_crossovers=_active_jibs(),
                                       helm_factor=boats.active_helm_factor(),
                                       polar_adjustments=_active_adjustments(),
                                       use_waves=body.get("use_waves", True))
    except Exception as exc:
        return JSONResponse({"detail": f"playbook failed: {exc}"}, status_code=500)


def _playbook_params(body):
    """Shared race/course/start/model resolution for the Lab-2b synthesize + freeze routes."""
    body = body or {}
    rid = body.get("race_id")
    d = store.get_race(rid) if rid else None
    if not d:
        return None, None, None, None, None
    course_id = body.get("course_id")
    start_epoch = float(body.get("start_epoch") or datetime.datetime.now(
        datetime.timezone.utc).timestamp())
    from .wind.models import DEFAULT_MODELS, MODELS
    models_req = body.get("models") or list(DEFAULT_MODELS)
    model_names = [m for m in models_req if m in MODELS] or list(DEFAULT_MODELS)
    ens = int(body.get("ensemble_members") or 0)
    return d, course_id, start_epoch, model_names, ens


@app.get("/api/crossovers")
async def crossovers():
    """The sail crossover model the optimizer attaches per leg + freezes into the playbook bundle —
    for the Boat-model review panel. The per-TWA×TWS zones come from the ORC cert; the upwind jib
    change-downs (J1/J2/J3 by TWS) come from the active boat (the cert rates only the J1)."""
    from . import sailplan
    m = sailplan.model()
    b = boats.active_boat() or {}
    jibs = b.get("jib_crossovers") or []
    if b.get("sail_inventory"):
        m["inventory"] = b["sail_inventory"]        # the boat's real inventory (incl. J2/J3)
    m["jib_crossovers"] = jibs
    m["crossovers"] = sailplan.crossovers_specialized(jibs)   # chart shows the real jib per TWS row
    m["boat_id"] = b.get("boat_id") or m.get("boat_id")
    return m


@app.post("/api/boats/jib-crossovers")
async def save_jib_crossovers(body: dict):
    """Update the active boat's upwind jib change-downs (J1/J2/J3 by TWS) — the editable, not-from-
    the-cert crossover the user reviews. Body: {jib_crossovers:[{sail,tws_min?,tws_max?}]}."""
    b = boats.active_boat()
    if not b:
        return JSONResponse({"detail": "no active boat"}, status_code=404)
    bands = (body or {}).get("jib_crossovers")
    if not isinstance(bands, list):
        return JSONResponse({"detail": "jib_crossovers must be a list"}, status_code=400)
    b["jib_crossovers"] = bands
    boats.save_boat(b)
    return {"saved": True, "boat_id": b.get("boat_id"), "jib_crossovers": bands}


@app.get("/api/polars")
async def polars_grid():
    """The boat polar grid (TWS × TWA → target STW) for the Boat-model review panel."""
    from . import polars
    rows = polars.polars_stw()
    tws = sorted({r[0] for r in rows})
    twa = sorted({r[1] for r in rows})
    k = lambda x: "%g" % x                 # match JS String() (4.0 -> "4", 45.3 -> "45.3")
    grid = {k(t): {k(a): None for a in twa} for t in tws}
    for t, a, stw in rows:
        grid[k(t)][k(a)] = stw
    return {"tws_buckets": tws, "twa_buckets": twa, "grid": grid, "n_points": len(rows)}


@app.post("/api/playbook/synthesize")
async def playbook_synthesize(body: dict):
    """Lab-2b: the 2a fan-out → Opus synthesis (per-variant rationale/tradeoffs/what-flips-it +
    decision tree) → an UNSIGNED draft bundle (the copilot-loadable c4.playbook/v1 schema). Freeze
    it to sign + persist for onboard use."""
    d, course_id, start_epoch, model_names, ens = _playbook_params(body)
    if not d:
        return JSONResponse({"detail": "unknown race_id"}, status_code=404)
    from . import synthesis
    try:
        return await run_in_threadpool(synthesis.synthesize, d, course_id, start_epoch,
                                       model_names, ens, jib_crossovers=_active_jibs(),
                                       helm_factor=boats.active_helm_factor(),
                                       polar_adjustments=_active_adjustments(),
                                       use_waves=(body or {}).get("use_waves", True))
    except Exception as exc:
        return JSONResponse({"detail": f"synthesis failed: {exc}"}, status_code=500)


@app.post("/api/playbook/freeze")
async def playbook_freeze(body: dict):
    """Sign + persist a playbook bundle — the frozen-at-the-gun homework deployed onboard. Pass a
    `bundle` already synthesized (the common path: review then freeze), or race params to synthesize
    and freeze in one shot. Returns the id + signature; the file IS the onboard-loadable artifact."""
    from . import synthesis
    body = body or {}
    bundle = body.get("bundle")
    if not bundle:
        d, course_id, start_epoch, model_names, ens = _playbook_params(body)
        if not d:
            return JSONResponse({"detail": "provide a bundle or a known race_id"}, status_code=404)
        bundle = await run_in_threadpool(synthesis.synthesize, d, course_id, start_epoch,
                                         model_names, ens, jib_crossovers=_active_jibs(),
                                         helm_factor=boats.active_helm_factor(),
                                         polar_adjustments=_active_adjustments(),
                                         use_waves=body.get("use_waves", True))
        if not bundle.get("variants"):
            return JSONResponse({"detail": "nothing to freeze — no variants",
                                 "bundle": bundle}, status_code=422)
    bundle = synthesis.sign_bundle(bundle)
    pid = pbstore.save(bundle)
    return {"frozen": True, "id": pid, "signature": bundle["signature"],
            "verified": synthesis.verify_bundle(bundle), "bundle": bundle}


@app.get("/api/playbooks")
async def playbooks_list():
    return {"playbooks": pbstore.list_bundles()}


@app.get("/api/playbooks/{pid}")
async def playbook_get(pid: str):
    b = pbstore.get(pid)
    if not b:
        return JSONResponse({"detail": "unknown playbook id"}, status_code=404)
    from . import synthesis
    return {"bundle": b, "verified": synthesis.verify_bundle(b)}


@app.get("/api/playbooks/{pid}/download")
async def playbook_download(pid: str):
    """The exact signed bytes — scp this to the Orin and point the copilot's PLAYBOOK_PATH at it."""
    raw = pbstore.get_raw(pid)
    if raw is None:
        return JSONResponse({"detail": "unknown playbook id"}, status_code=404)
    from fastapi.responses import Response
    return Response(content=raw, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{pid}.json"'})


# ---- Debrief (Lab-4 post-race judge loop: oracle re-route → regret → critique → write-back) ---
@app.post("/api/debrief/run")
async def debrief_run(body: dict):
    race_id = (body or {}).get("race_id")
    if not race_id:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    return await run_in_threadpool(judge.run_judge, race_id, (body or {}).get("playbook_id"))


@app.post("/api/debrief/apply")
async def debrief_apply(body: dict):
    race_id = (body or {}).get("race_id")
    if not race_id:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    return await run_in_threadpool(judge.apply_writeback, race_id, (body or {}).get("learnings"))


# ---- Debrief: ACTUAL boat-track ingestion (GPX upload / YB our-boat) → helm-vs-optimal scoring ---
@app.get("/api/debrief/track")
async def debrief_track_get(race_id: str):
    t = track.load_track(race_id)
    if not t:
        return {"available": False}
    fixes = t.get("fixes") or []
    return {"available": True, "source": t.get("source"), "boat": t.get("boat"),
            "matched_by": t.get("matched_by"), "n": len(fixes),
            "fixes": [[f["lat"], f["lon"]] for f in fixes]}   # lightweight polyline for the map


@app.post("/api/debrief/track/upload")
async def debrief_track_upload(race_id: str, file: UploadFile = File(...)):
    if not store.get_race(race_id):
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    raw = await file.read()
    try:
        trk = await run_in_threadpool(track.parse_gpx, raw)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    meta = await run_in_threadpool(track.save_track, race_id, trk)
    return {"ok": True, **meta}


@app.post("/api/debrief/track/fetch")
async def debrief_track_fetch(body: dict):
    race_id = (body or {}).get("race_id")
    d = store.get_race(race_id) if race_id else None
    if not d:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    res = await run_in_threadpool(track.fetch_yb_track, d, (body or {}).get("boat"))
    if not res.get("ok"):
        return JSONResponse(res, status_code=502 if "failed" in (res.get("note") or "") else 200)
    meta = await run_in_threadpool(track.save_track, race_id, res)
    return {"ok": True, **meta}


@app.post("/api/debrief/track/clear")
async def debrief_track_clear(body: dict):
    race_id = (body or {}).get("race_id")
    if not race_id:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    return {"ok": await run_in_threadpool(track.clear_track, race_id)}


# ---- Lab-4 learning loop: ongoing performance archive + HUMAN-APPROVED boat-model refinement ----
@app.get("/api/learning/debriefs")
async def learning_debriefs(boat_id: str = None, race_id: str = None):
    bid = boat_id or (boats.active_boat() or {}).get("boat_id")
    return {"boat_id": bid, "debriefs": await run_in_threadpool(learning.list_debriefs, bid, race_id)}


@app.get("/api/learning/debriefs/{debrief_id}")
async def learning_debrief(debrief_id: int):
    d = await run_in_threadpool(learning.get_debrief, debrief_id)
    return d or JSONResponse({"detail": "not found"}, status_code=404)


@app.get("/api/learning/proposals")
async def learning_proposals(boat_id: str = None):
    bid = boat_id or (boats.active_boat() or {}).get("boat_id")
    return {"boat_id": bid, "proposals": await run_in_threadpool(learning.list_proposals, bid)}


@app.post("/api/learning/propose")
async def learning_propose(body: dict = None):
    bid = (body or {}).get("boat_id") or (boats.active_boat() or {}).get("boat_id")
    if not bid:
        return JSONResponse({"detail": "no active boat"}, status_code=400)
    return await run_in_threadpool(learning.propose, bid)


@app.post("/api/learning/proposals/{pid}/apply")
async def learning_apply(pid: int, body: dict = None):
    """HUMAN-APPROVED apply — writes the (optionally edited) helm_factor + polar overlay to the boat."""
    body = body or {}
    return await run_in_threadpool(learning.apply_proposal, pid, body.get("helm_factor"),
                                   body.get("adjustments"), body.get("note", ""))


@app.post("/api/learning/proposals/{pid}/reject")
async def learning_reject(pid: int, body: dict = None):
    return await run_in_threadpool(learning.reject_proposal, pid, (body or {}).get("note", ""))


# ---- Monitor (shore-side live view: fleet via public tracker + our boat via cloud telemetry) --
@app.get("/api/monitor")
async def monitor_view(race_id: str, demo: bool = False):
    d = store.get_race(race_id)
    if not d:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    return await run_in_threadpool(monitor.snapshot, d, demo)


# ---- Checklist prep progress (team check-off; kept in labstate, not the RaceDefinition) -------
@app.get("/api/checklist")
async def checklist_get(race_id: str):
    return {"checked": labstate.get("checklist:" + race_id) or {}}


@app.post("/api/checklist")
async def checklist_set(body: dict):
    race_id = (body or {}).get("race_id")
    if not race_id:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    checked = {k: True for k, v in ((body or {}).get("checked") or {}).items() if v}
    labstate.set("checklist:" + race_id, checked)
    return {"saved": True, "checked": checked}


# ---- Lock-in & Deploy -------------------------------------------------------------------------
@app.get("/api/deploy")
async def deploy_readiness(race_id: str, course_id: str = None):
    """Per-race deploy readiness (course/fleet/checklists/playbook status) + frozen playbooks + the
    locked-in selection + onboard targets."""
    r = deploy.readiness(race_id, course_id)
    if r is None:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    return r


@app.post("/api/deploy/lock-in")
async def deploy_lock_in(body: dict):
    """Lock a frozen playbook in as the race's deploy homework. Body: {race_id, playbook_id}."""
    race_id = (body or {}).get("race_id")
    playbook_id = (body or {}).get("playbook_id")
    if not race_id or not playbook_id:
        return JSONResponse({"detail": "race_id and playbook_id required"}, status_code=400)
    state = deploy.lock_in(race_id, playbook_id)
    if state is None:
        return JSONResponse({"detail": "playbook not found for this race"}, status_code=404)
    return {"locked_in": True, "lock_in": state}


@app.get("/api/deploy/package/{race_id}/download")
async def deploy_package(race_id: str, course_id: str = None):
    """The combined homework package (ready-to-POST course_load + fleet_load + iPad checklists)."""
    pkg = deploy.package(race_id, course_id)
    if pkg is None:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    from fastapi.responses import Response
    return Response(content=json.dumps(pkg, indent=2), media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="homework_{race_id}.json"'})


@app.post("/api/races")
async def save_race(body: dict):
    """Save a (human-reviewed) RaceDefinition to the library. Errors don't block saving a draft —
    they're surfaced so the team finishes review — but a race_id is required."""
    definition = (body or {}).get("definition") or body
    if not definition.get("race_id"):
        return JSONResponse({"detail": "definition.race_id required"}, status_code=400)
    rid = _write_race(definition)
    errors, warnings = race_def.validate(definition)
    return {"saved": True, "race_id": rid, "errors": errors, "warnings": warnings,
            "reviewed": bool(definition.get("reviewed"))}


def _write_race(definition):
    rid = re.sub(r"[^a-z0-9_-]", "", str(definition.get("race_id", "")).lower())
    os.makedirs(INGESTED_DIR, exist_ok=True)
    with open(os.path.join(INGESTED_DIR, f"{rid}.json"), "w") as f:
        json.dump(definition, f, indent=2)
    return rid


@app.post("/api/races/{rid}/approve")
async def approve_race(rid: str, body: dict):
    """Sign-off: mark a reviewed RaceDefinition approved (persists any edits sent with it). Refuses
    to approve a definition with BLOCKING errors — warnings (needs-review items) don't block sign-off
    but are returned. `{"approved": false}` clears the flag (un-approve)."""
    definition = (body or {}).get("definition") or store.get_race(rid)
    if not definition or not definition.get("race_id"):
        return JSONResponse({"detail": "race not found / missing race_id"}, status_code=404)
    approved = (body or {}).get("approved", True)
    errors, warnings = race_def.validate(definition)
    if approved and errors:
        return JSONResponse({"detail": f"cannot approve — {len(errors)} blocking error(s); "
                                       "fix them first", "errors": errors, "warnings": warnings,
                             "reviewed": False}, status_code=400)
    if approved:
        definition["reviewed"] = True
        definition["reviewed_at"] = _today()
    else:
        definition["reviewed"] = False
        definition.pop("reviewed_at", None)
    saved_rid = _write_race(definition)
    return {"saved": True, "race_id": saved_rid, "reviewed": approved,
            "reviewed_at": definition.get("reviewed_at"), "errors": errors, "warnings": warnings}


@app.get("/favicon.ico")
async def favicon():
    """A tiny inline SVG mark so browsers stop logging a 404 for /favicon.ico (and the tab gets an
    icon). Declared before the static mount so it wins."""
    from fastapi.responses import Response
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           b'<rect width="32" height="32" rx="6" fill="#0e1622"/>'
           b'<path d="M16 5 L25 24 H7 Z" fill="#3aa0ff"/>'
           b'<rect x="6" y="24" width="20" height="3" rx="1.5" fill="#7bc0ff"/></svg>')
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# The Lab web shell (static). Declared last so the /api routes match first; html=True serves
# index.html at "/". Client-side hash routing handles the section tabs.
app.mount("/", StaticFiles(directory=os.environ.get("WEB_DIR", "/srv/web"), html=True), name="web")
