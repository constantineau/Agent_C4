"""C4 Performance Lab — cloud service (Lab-0).

The browser-based, between-races side of the project: PREP (ingest race docs → RaceDefinition →
review → gameplan → lock-in & deploy) and DEBRIEF (learning loop). Shared team login; the static
shell is public, the /api/* data routes are gated. This service both serves the Lab web app and
exposes the race-library API. The race-day ONBOARD console is a separate, deliberately-simple
surface (pi/console) — not this.

Slice 1: shared-password auth + the race library (list / get / validate) + the web shell. Next:
the dual-input ingestion (auto-find URL / paste-link / upload PDF → Opus extraction → review).
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, store

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


# The Lab web shell (static). Declared last so the /api routes match first; html=True serves
# index.html at "/". Client-side hash routing handles the section tabs.
app.mount("/", StaticFiles(directory=os.environ.get("WEB_DIR", "/srv/web"), html=True), name="web")
