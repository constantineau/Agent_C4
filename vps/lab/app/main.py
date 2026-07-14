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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from shared import race_def, boat_profile
from . import auth, store, extract, boats, labstate, feedback, pbstore, deploy, monitor, judge, track, learning, fleetimport, report, retro, retrostore, share

INGESTED_DIR = os.environ.get("INGESTED_DIR", "/srv/ingested")

app = FastAPI(title="C4 Performance Lab", version="0.1.0")

# gzip the static shell + JSON responses (app.js alone is ~175 KB raw / ~49 KB gzipped — the Lab
# is used over hotel Wi-Fi and Starlink; optimize results are large JSON too)
app.add_middleware(GZipMiddleware, minimum_size=1024)

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


@app.post("/api/watchplan/generate")
def watchplan_generate(body: dict):
    """Emit watch blocks from a pattern (shared/watchplan.py — the SAME generator the tests
    exercise, so the Lab editor and the onboard resolver can't drift). Body: {anchor: epoch,
    total_hours, pattern: name|[hours], first_on?}. The client lays the blocks into
    definition.watch_plan and hand-edits from there."""
    from shared import watchplan
    body = body or {}
    blocks = watchplan.generate(body.get("anchor"), body.get("total_hours"),
                                body.get("pattern"), body.get("first_on") or "A")
    return {"blocks": blocks, "patterns": sorted(watchplan.PATTERNS)}


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


def _active_sails():
    """The active boat's Code 0 band + main reef points (crew label overlays, not in the cert)."""
    return boats.active_sail_config()


def _active_adjustments():
    """The active boat's human-APPROVED refined-polar overlay (Lab-4) — applied to the optimizer's
    polars. Empty until a learning proposal is approved (→ routes on the raw ORC cert)."""
    return boats.active_polar_adjustments()


def _active_wave():
    """The active boat's human-APPROVED sea-state degradation coefficients (Lab-4 calibration), or None
    → the optimizer uses the conservative ROUTE_WAVE_* env priors."""
    return boats.active_wave_coeffs()


def _run_optimize(definition, course_id, start_epoch, model_names, ensemble_members, avoid=True,
                  per_model=False, resolution="auto", use_waves=True, use_current=True,
                  on_progress=None):
    """Blocking: build the multi-model wind field, route the course, write the briefing.
    `on_progress(msg)` (optional) mirrors the log lines out live — the background-job status
    endpoint serves them so the UI can show which weather source it's waiting on."""
    from .wind import build_windfield
    from . import optimizer, current, wave
    bbox = optimizer.course_bbox(definition, course_id)
    if not bbox:
        return {"available": False, "note": "course has no geocoded marks — review Course & Marks"}
    hours = optimizer.estimate_hours(definition, course_id)
    t_end = start_epoch + hours * 3600
    class _Log(list):                      # a list whose .append also mirrors to on_progress
        def append(self, msg):
            list.append(self, msg)
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass
    log = _Log()
    # venue-specific model-skill weighting: weight each model by its measured past accuracy here,
    # de-bias its persistent offset. Best-effort — any failure falls back to the static-priority blend.
    skill = {"enabled": False}
    mweights = mbias = None
    try:
        from . import venue as venue_mod, modelskill
        v = venue_mod.resolve(definition, course_id, bbox=bbox)
        race_date = datetime.datetime.fromtimestamp(start_epoch, datetime.timezone.utc).date()
        skill = (modelskill.venue_weights(v, models=model_names, race_date=race_date)
                 if v else {"enabled": False})
        if skill.get("enabled"):
            mweights, mbias = skill["model_weights"], skill["model_bias"]
            log.append(f"model-skill: {v['key']} via {skill['station']} → "
                       + ", ".join(f"{m}×{w}" for m, w in mweights.items()))
    except Exception as exc:
        log.append(f"model-skill: skipped ({exc})")
    wf = build_windfield(bbox, start_epoch, t_end, models=model_names,
                         ensemble_members=ensemble_members, on_progress=log.append,
                         model_weights=mweights, model_bias=mbias)
    if not wf.loaded:
        return {"available": False, "note": "no weather model data could be loaded (not yet "
                "posted, or no egress)", "windfield": wf.status(), "log": log}
    # water current: opt-out per run (compare with vs without) — LMHOFS surface currents, ZeroCurrent on any miss
    cur = (current.build_currentfield(bbox, start_epoch, t_end, on_progress=log.append)
           if use_current else current.ZeroCurrent())
    # sea state (phase 1 seam) — opt-out per run; helm factor still applies (it's crew efficiency, not waves)
    waves = (wave.build_wavefield(bbox, start_epoch, t_end, on_progress=log.append)
             if use_waves else wave.ZeroWave())
    result = optimizer.optimize_course(definition, course_id, start_epoch, wf, avoid=avoid,
                                       source=chart_source(),
                                       safety_depth=boats.active_safety_depth_m(),
                                       jib_crossovers=_active_jibs(), sail_config=_active_sails(), per_model=per_model,
                                       resolution=resolution, cur=cur,
                                       waves=waves, helm_factor=boats.active_helm_factor(),
                                       polar_adjustments=_active_adjustments(), wave_coeffs=_active_wave())
    result["current"] = cur.status()
    result["waves"] = waves.status()
    result["model_skill"] = skill                # venue model-skill weights + scorecard (UI panel)
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
    """Lab-1: run the multi-model GRIB optimizer over a race course → one route + briefing.

    STARTS a background job and returns immediately; poll `/api/optimize/status` for the result.
    A slow/rate-limited weather source (NOMADS on a busy cycle, the ECMWF open feed) routinely
    pushed the old synchronous request past the nginx ~300 s gateway cap → a 504 with the work
    thrown away. Same job pattern as the synthesis fan; single-flight per Lab."""
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
    use_current = body.get("use_current", True)
    from . import jobs

    def _work(progress):
        return _run_optimize(d, course_id, start_epoch, model_names, ens, avoid, per_model,
                             resolution, use_waves, use_current, on_progress=progress)

    return jobs.start("optimize", _work, meta={"race_id": rid})


@app.get("/api/optimize/status")
async def optimize_status():
    """Poll the background optimize job; the route rides on state=done under `result`."""
    from . import jobs
    return jobs.status("optimize")


@app.post("/api/gameplan/pdf")
async def gameplan_pdf(body: dict):
    """Render the current gameplan (the client's optimize result + optional synthesized playbook) into
    a shareable PDF report. The frontend posts what it already has — no re-optimize."""
    body = body or {}
    if not (body.get("result") or {}).get("legs"):
        return JSONResponse({"detail": "no optimized route to report — run the optimizer first"},
                            status_code=400)
    try:
        # The PDF carries a live route-player link + QR; the share is a bonus — never blocks the PDF.
        body["share_url"] = share.create(body)["url"]
    except Exception:
        pass
    try:
        pdf = await run_in_threadpool(report.build_gameplan_pdf, body)
    except Exception as exc:
        return JSONResponse({"detail": f"pdf render failed: {exc}"}, status_code=500)
    name = re.sub(r"[^\w.\-]", "_", f"gameplan_{body.get('race_name') or (body.get('result') or {}).get('race_id') or 'c4'}")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})


@app.post("/api/share")
async def create_share(body: dict):
    """Freeze the current gameplan into a public read-only route-player link (app/share.py)."""
    try:
        return await run_in_threadpool(share.create, body or {})
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/share/{token}")
async def share_page(token: str):
    """The crew-facing route player. No team login — the unguessable token is the access."""
    if not share.load(token):
        return JSONResponse({"detail": "unknown share link"}, status_code=404)
    return FileResponse(os.path.join(os.environ.get("WEB_DIR", "/srv/web"), "player.html"),
                        media_type="text/html")


@app.get("/share/{token}/data")
async def share_data(token: str):
    bundle = share.load(token)
    if not bundle:
        return JSONResponse({"detail": "unknown share link"}, status_code=404)
    return JSONResponse(bundle)


def _skill_venue(body):
    """(definition, venue, race_date) for a model-skill request, or (None, ...) if unresolved."""
    d = store.get_race(body.get("race_id")) if body.get("race_id") else None
    if not d:
        return None, None, None
    from . import venue as venue_mod
    v = venue_mod.resolve(d, body.get("course_id"))
    se = body.get("start_epoch")
    rd = (datetime.datetime.fromtimestamp(float(se), datetime.timezone.utc).date() if se else None)
    return d, v, rd



@app.post("/api/model-skill/backfill")
def model_skill_backfill(body: dict):
    """Start the OFFLINE deep backfill (pre-2021 HRRR + GEFS-reforecast GRIB archives) for a race's
    venue as a BACKGROUND JOB — a full seasonal span is ~an hour of byte-range GRIB gets, far past
    the gateway's ~300 s request cap (the real Mackinac run took 77 min as a blocking request).
    Poll GET /api/model-skill/backfill/status; on `done` the refreshed weights ride in `result`."""
    body = body or {}
    d, v, rd = _skill_venue(body)
    if not d:
        return JSONResponse({"detail": "unknown race_id"}, status_code=404)
    if not v or not v.get("station"):
        return JSONResponse({"detail": "venue has no observation station — can't score skill"},
                            status_code=422)
    from . import jobs, modelskill

    def _work(progress):
        modelskill.backfill_deep(v, modelskill.DEFAULT_MODELS, rd, log=progress)
        return modelskill.venue_weights(v, refresh=False)

    return jobs.start("model_skill_backfill", _work, meta={"venue": v.get("tag") or v.get("station")})


@app.get("/api/model-skill/backfill/status")
def model_skill_backfill_status():
    from . import jobs
    return jobs.status("model_skill_backfill")




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



@app.get("/api/crossovers")
async def crossovers():
    """The sail crossover model the optimizer attaches per leg + freezes into the playbook bundle —
    for the Boat-model review panel. The per-TWA×TWS zones come from the ORC cert; the upwind jib
    change-downs (J1/J2/J3 by TWS) come from the active boat (the cert rates only the J1)."""
    from . import sailplan
    m = sailplan.model()
    b = boats.active_boat() or {}
    jibs = b.get("jib_crossovers") or []
    cfg = {"code0": b.get("code0") or {}, "main_reefs": b.get("main_reefs") or {}}
    if b.get("sail_inventory"):
        m["inventory"] = b["sail_inventory"]        # the boat's real inventory (incl. J2/J3, C0)
    m["jib_crossovers"] = jibs
    m["crossovers"] = sailplan.crossovers_specialized(jibs, cfg)  # real jib per TWS row + C0 carve
    m["overlaps"] = sailplan.overlaps_specialized(jibs)       # toss-up bands, jib relabelled to match
    m["code0"] = cfg["code0"]                       # light-air reacher band (crew, not cert)
    m["main_reefs"] = cfg["main_reefs"]             # reef points (depower + A3 slot)
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


@app.post("/api/boats/sail-config")
async def save_sail_config(body: dict):
    """Update the active boat's crew sail-config overlays — the Code 0 band and/or the main reef
    points. Body: {code0?: {enabled, tws_max, twa_min, twa_max}, main_reefs?: {r1_tws_kn,
    r1_a3_slot_tws_kn}}. These are label overlays (the ORC cert rates neither) — they set the sail
    CALLS across routing legs, the crossover chart, the frozen bundle and the copilot digest."""
    b = boats.active_boat()
    if not b:
        return JSONResponse({"detail": "no active boat"}, status_code=404)
    c0 = (body or {}).get("code0")
    mr = (body or {}).get("main_reefs")
    if c0 is not None:
        if not isinstance(c0, dict):
            return JSONResponse({"detail": "code0 must be an object"}, status_code=400)
        if c0.get("enabled", True):
            try:
                tmax, alo, ahi = float(c0["tws_max"]), float(c0["twa_min"]), float(c0["twa_max"])
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"detail": "code0 needs numeric tws_max/twa_min/twa_max"},
                                    status_code=400)
            if not (0 < tmax <= 30 and 0 <= alo < ahi <= 180):
                return JSONResponse({"detail": "code0 band out of range"}, status_code=400)
        b["code0"] = c0
        inv = b.get("sail_inventory") or []
        if c0.get("enabled", True) and "C0" not in inv:
            b["sail_inventory"] = inv + ["C0"]
    if mr is not None:
        if not isinstance(mr, dict):
            return JSONResponse({"detail": "main_reefs must be an object"}, status_code=400)
        for k in ("r1_tws_kn", "r1_a3_slot_tws_kn"):
            v = mr.get(k)
            if v is not None and not (isinstance(v, (int, float)) and 4 <= v <= 45):
                return JSONResponse({"detail": f"{k} must be 4–45 kn (or null)"}, status_code=400)
        b["main_reefs"] = mr
    boats.save_boat(b)
    return {"saved": True, "boat_id": b.get("boat_id"),
            "code0": b.get("code0") or {}, "main_reefs": b.get("main_reefs") or {}}


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
    kw = dict(jib_crossovers=_active_jibs(), sail_config=_active_sails(), helm_factor=boats.active_helm_factor(),
              polar_adjustments=_active_adjustments(), wave_coeffs=_active_wave(),
              use_waves=(body or {}).get("use_waves", True),
              fan_depth=(body or {}).get("fan_depth", "standard"))
    if (body or {}).get("sync"):
        # legacy synchronous path (fits the gateway only for small fans) — the UI uses the job
        try:
            return await run_in_threadpool(synthesis.synthesize, d, course_id, start_epoch,
                                           model_names, ens, **kw)
        except Exception as exc:
            return JSONResponse({"detail": f"synthesis failed: {exc}"}, status_code=500)
    return synthesis.start_job(d, course_id, start_epoch, model_names, ens, **kw)


@app.get("/api/playbook/synthesize/status")
async def playbook_synthesize_status():
    """Poll the background synthesis job; `bundle` rides on state=done."""
    from . import synthesis
    return synthesis.job_status()


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
                                         model_names, ens, jib_crossovers=_active_jibs(), sail_config=_active_sails(),
                                         helm_factor=boats.active_helm_factor(),
                                         polar_adjustments=_active_adjustments(),
                                         wave_coeffs=_active_wave(),
                                         use_waves=body.get("use_waves", True),
                                         fan_depth=body.get("fan_depth", "standard"))
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


@app.get("/api/playbooks/{pid}/gpx")
async def playbook_gpx(pid: str, variants: str = "recommended"):
    """The frozen gameplan as GPX for the boat's CHARTPLOTTER (Garmin GPSMAP 943 imports GPX
    from a memory card / ActiveCaptain and draws it): course marks as waypoints, the recommended
    variant as a navigable route (+ every variant as a drawn track with variants=all)."""
    b = pbstore.get(pid)
    if not b:
        return JSONResponse({"detail": "unknown playbook id"}, status_code=404)
    marks = []
    d = store.get_race(b.get("race_id") or "")
    if d:
        try:
            rows, _sk, _cid = race_def.course_to_marks(d, b.get("course_id"))
            # course_to_marks returns (seq, name, lat, lon) tuples — reshape for the GPX builder
            marks = [{"name": name, "lat": lat, "lon": lon} for (_seq, name, lat, lon) in rows]
        except Exception:
            marks = []
    from . import gpx as gpx_mod
    text = gpx_mod.bundle_gpx(b, marks=marks, variants=variants)
    from fastapi.responses import Response
    return Response(content=text, media_type="application/gpx+xml", headers={
        "Content-Disposition": f'attachment; filename="{pid}.gpx"'})


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


# ---- Debrief: FLEET RETRO study (docs/RETRO_STUDY.md) — past-race archive + per-boat runs -------
@app.get("/api/retro/races")
async def retro_races():
    return {"races": retrostore.list_races(), "grib_archive": retrostore.grib_stats()}


@app.post("/api/retro/ingest")
async def retro_ingest(body: dict):
    """Pull a YB race (entries + full-fleet tracks + results) into the persistent retro archive."""
    rid = (body or {}).get("race_id")
    if not rid:
        return JSONResponse({"detail": "race_id required (the YB id, e.g. bayviewmack2025)"},
                            status_code=400)
    try:
        return await run_in_threadpool(retro.ingest_race, rid)
    except Exception as exc:
        return JSONResponse({"detail": f"retro ingest failed: {exc}"}, status_code=500)


@app.post("/api/retro/run")
async def retro_run(body: dict):
    """R4: per-boat gun-forecast optimize + own-track scoring for the archived fleet. A full fleet
    runs 1-2h — far past the gateway timeout — so this STARTS a background job; poll
    GET /api/retro/run/status. Scope a pilot with teams/limit."""
    b = body or {}
    rid = b.get("race_id")
    if not rid:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    return retro.start_fleet_job(rid, b.get("def_race_id") or "bayview-mackinac-2026",
                                 b.get("course_id") or "cove_island", b.get("teams"),
                                 b.get("limit"), b.get("resolution") or "auto")


@app.get("/api/retro/run/status")
async def retro_run_status():
    return retro.fleet_job_status()


@app.get("/api/retro/report")
async def retro_report(race_id: str):
    """R5: adherence-vs-finish analysis over the archived fleet runs."""
    try:
        return await run_in_threadpool(retro.report, race_id)
    except Exception as exc:
        return JSONResponse({"detail": f"retro report failed: {exc}"}, status_code=500)


@app.post("/api/retro/polars")
async def retro_polars(body: dict):
    """Match every ingested entry to its public ORC cert + store the converted polar."""
    rid = (body or {}).get("race_id")
    if not rid:
        return JSONResponse({"detail": "race_id required"}, status_code=400)
    try:
        return await run_in_threadpool(retro.match_polars, rid,
                                       (body or {}).get("country") or "USA")
    except Exception as exc:
        return JSONResponse({"detail": f"retro polar match failed: {exc}"}, status_code=500)


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


@app.get("/api/racelog/sessions")
def racelog_sessions():
    """Race-session windows backfilled from the boat (the iPad record switch) — the debrief's
    boat-log source list."""
    from . import monitor
    try:
        return monitor.agent_json("/racelog/sessions")
    except Exception as exc:
        return {"sessions": [], "note": f"agent unreachable: {exc}"}


@app.post("/api/debrief/track/from-log")
def debrief_track_from_log(body: dict):
    """Build the debrief track from the BOAT'S OWN backfilled log (full-res position/SOG —
    far denser than a public tracker) over a race-session window: {race_id, start_ts, end_ts,
    name?}. Stores it exactly like a GPX/YB track; the sail log rides along."""
    from . import monitor
    body = body or {}
    rid = body.get("race_id")
    if not rid or body.get("start_ts") is None or body.get("end_ts") is None:
        return JSONResponse({"detail": "race_id, start_ts and end_ts are required"},
                            status_code=422)
    try:
        r = monitor.agent_json(f"/racelog/track?start={float(body['start_ts'])}"
                               f"&end={float(body['end_ts'])}")
    except Exception as exc:
        return JSONResponse({"detail": f"agent unreachable: {exc}"}, status_code=502)
    fixes = r.get("fixes") or []
    if len(fixes) < 10:
        return JSONResponse({"detail": "the boat log has no track in that window — has the "
                                       "backfill run since the session?"}, status_code=404)
    meta = track.save_track(rid, {"source": "boatlog", "boat": body.get("name"),
                                      "fixes": fixes, "n": len(fixes),
                                      "sail_log": r.get("sail_log") or []})
    return {"ok": True, **meta, "sail_changes": len(r.get("sail_log") or [])}


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


@app.post("/api/learning/calibrate-waves")
async def learning_calibrate_waves(body: dict = None):
    """Fit the sea-state degradation coefficients (ROUTE_WAVE_K_* per point of sail) from the boat's
    realized-polar archive → a PROPOSED wave_coeffs refinement for human review (never auto-applied)."""
    bid = (body or {}).get("boat_id") or (boats.active_boat() or {}).get("boat_id")
    if not bid:
        return JSONResponse({"detail": "no active boat"}, status_code=400)
    return await run_in_threadpool(learning.calibrate_waves, bid)


@app.get("/api/learning/config-polars")
def learning_config_polars(boat_id: str = None):
    """Observed polars BY SAIL CONFIGURATION (the sails-bar log attributed to every fix) —
    curves for combinations the crossover chart doesn't rate accumulate here over time."""
    return learning.config_polars(boat_id)


@app.get("/api/learning/trend")
async def learning_trend(boat_id: str = None):
    """Per-race performance series (latest debrief per race) + applied-refinement milestones — the
    multi-race trend of helm%/regret/time-behind so you can see the boat model improving."""
    bid = boat_id or (boats.active_boat() or {}).get("boat_id")
    if not bid:
        return JSONResponse({"detail": "no active boat"}, status_code=400)
    return await run_in_threadpool(learning.trend, bid)


@app.post("/api/learning/proposals/{pid}/apply")
async def learning_apply(pid: int, body: dict = None):
    """HUMAN-APPROVED apply — writes the (optionally edited) refinement to the boat: helm_factor + polar
    overlay for a boat_model proposal, or the sea-state coefficients for a wave_coeffs proposal."""
    body = body or {}
    return await run_in_threadpool(learning.apply_proposal, pid, body.get("helm_factor"),
                                   body.get("adjustments"), body.get("note", ""), body.get("wave_coeffs"))


@app.post("/api/learning/proposals/{pid}/reject")
async def learning_reject(pid: int, body: dict = None):
    return await run_in_threadpool(learning.reject_proposal, pid, (body or {}).get("note", ""))


# ---- Fleet roster auto-import (public data: YB entry list + ORC handicaps) → DRAFT for review ----
@app.post("/api/fleet/import")
async def fleet_import(body: dict):
    """Build a DRAFT fleet roster from public data — the entry list (YB tracker OR a regatta-website
    URL/pasted text) + ORC cert handicaps. Returns the proposed roster (with match stats); the human
    reviews/edits in the Fleet tab and Saves via POST /api/races (nothing auto-committed). Body:
    {race_id, source: yb|both|website|orc, country?, course_id?, url?, text?}."""
    b = body or {}
    d = store.get_race(b.get("race_id")) if b.get("race_id") else None
    if not d:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    return await run_in_threadpool(fleetimport.import_fleet, d, b.get("source", "both"),
                                   b.get("country", "USA,CAN"), b.get("course_id"),
                                   b.get("url"), b.get("text"))


@app.post("/api/fleet/orc-candidates")
def fleet_orc_candidates(body: dict):
    """Fuzzy ORC-cert candidates for ONE unrated roster boat: {race_id, boat, sail?, country?}.
    The human picks a candidate in the Fleet tab; nothing auto-applies."""
    body = body or {}
    d = store.get_race(body.get("race_id")) if body.get("race_id") else None
    return fleetimport.orc_candidates(body.get("boat"), sail=body.get("sail"),
                                      country=body.get("country") or "USA,CAN",
                                      definition=d, course_id=body.get("course_id"))


@app.post("/api/fleet/import/upload")
async def fleet_import_upload(race_id: str, country: str = "USA,CAN", course_id: str = None,
                             file: UploadFile = File(...)):
    """Extract a fleet roster from an uploaded entry-list PDF → ORC-enrich → DRAFT for review."""
    d = store.get_race(race_id)
    if not d:
        return JSONResponse({"detail": "unknown race"}, status_code=404)
    raw = await file.read()
    r = await run_in_threadpool(fleetimport.roster_from_pdf, raw)
    if not r.get("ok"):
        return r
    return await run_in_threadpool(fleetimport.pack_with_orc, r["entries"], "website", country,
                                   d, course_id)


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


# Fields OWNED by other tabs / built up over prep — a re-ingested draft (e.g. the SIs landing
# ~race week) must not wipe them when the fresh extraction doesn't carry them.
_PRESERVE_ON_SAVE = ("fleet", "own", "division_starts", "tracker", "learnings_notes",
                     "watch_plan")


def _write_race(definition):
    rid = re.sub(r"[^a-z0-9_-]", "", str(definition.get("race_id", "")).lower())
    prior = store.get_race(rid)
    if prior:
        for k in _PRESERVE_ON_SAVE:
            if not definition.get(k) and prior.get(k):
                definition[k] = prior[k]
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
