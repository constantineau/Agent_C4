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

from shared import race_def
from . import auth, store, extract

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
