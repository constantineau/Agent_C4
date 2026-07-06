"""The multi-labeler ranking service (FastAPI).

Sailors log in with their name + the shared team password, then rank candidate briefs best→worst and
flag each candidate's confidence/urgency calibration. The queue (sampling.next_snapshot_for) gives
full single coverage then ~OVERLAP_FRAC double coverage for inter-rater agreement. Candidates are
served BLIND + shuffled per labeler (render.py) so origin/position can't bias the ranking.

Run it (from pi/orin/):
    python3 -m training.labeling.app
    # → http://127.0.0.1:8400   (host it behind nginx on the shared Lab VM for the real push)
"""
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, sampling
from . import render, store

app = FastAPI(title="C4 Strategy-LoRA labeling")
_STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.middleware("http")
async def _no_store_api(request, call_next):
    """Never let a browser/proxy cache an /api/* response — a stale cached `next` (e.g. a
    done:true from a deploy window with no corpus loaded) would strand a labeler on the
    'All done' screen that a hard refresh can't clear."""
    resp = await call_next(request)
    p = request.url.path
    # no-store the API *and* the HTML shell: the shell carries versioned ?v= asset URLs, so it must
    # never itself be cached (esp. on iPad Safari, which has no hard-refresh) or the new JS/CSS never
    # loads. Versioned /static/*.js|css stay cacheable — the ?v bump is the cache key.
    if p.startswith("/api/") or p == "/" or p.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# --- request models --------------------------------------------------------------------------
class LoginReq(BaseModel):
    name: str
    password: str


class RankReq(BaseModel):
    labeler_id: str
    snapshot_id: str
    order: list[str]                 # best -> worst candidate_ids (a permutation of what was served)
    calibration: dict[str, str] = {}  # candidate_id -> "right"|"too_high"|"too_low"
    notes: str = ""
    elapsed_ms: int | None = None


# --- helpers ---------------------------------------------------------------------------------
def _candidates_for(snapshot_id: str) -> list[dict]:
    return sampling.load_candidates_by_snapshot().get(snapshot_id, [])


def _snapshot(snapshot_id: str) -> dict | None:
    for s in sampling.load_snapshots():
        if s["snapshot_id"] == snapshot_id:
            return s
    return None


# --- endpoints -------------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "snapshots": len(sampling.load_snapshots()),
            "candidates": sum(len(v) for v in sampling.load_candidates_by_snapshot().values())}


@app.post("/api/login")
def login(req: LoginReq):
    if req.password != config.LABEL_PASSWORD:
        raise HTTPException(status_code=401, detail="wrong team password")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    lid = store.upsert_labeler(req.name)
    return {"labeler_id": lid, "name": req.name.strip()}


@app.get("/api/next")
def next_task(labeler_id: str):
    if not labeler_id:
        raise HTTPException(status_code=400, detail="labeler_id required")
    sid = sampling.next_snapshot_for(labeler_id, store)
    prog = sampling.progress(store)
    my_done = len(store.snapshots_done_by(labeler_id))
    if sid is None:
        return {"done": True, "progress": prog, "my_done": my_done}
    snap = _snapshot(sid)
    cands = render.shuffled_candidates(_candidates_for(sid), sid, labeler_id)
    return {
        "done": False,
        "snapshot": render.render_snapshot(snap),
        "candidates": [render.render_candidate(c) for c in cands],
        "progress": prog,
        "my_done": my_done,
    }


@app.post("/api/rank")
def rank(req: RankReq):
    cands = _candidates_for(req.snapshot_id)
    if not cands:
        raise HTTPException(status_code=404, detail="unknown snapshot")
    valid_ids = {c["candidate_id"] for c in cands}
    if set(req.order) != valid_ids:
        raise HTTPException(status_code=400,
                            detail="order must be a permutation of the served candidates")
    for cid, flag in req.calibration.items():
        if cid not in valid_ids or flag not in ("right", "too_high", "too_low"):
            raise HTTPException(status_code=400, detail=f"bad calibration entry: {cid}={flag}")
    store.record_ranking(req.snapshot_id, req.labeler_id, req.order, req.calibration,
                         req.notes, req.elapsed_ms)
    return {"ok": True, "stats": store.stats()}


@app.get("/api/stats")
def stats():
    return {"store": store.stats(), "progress": sampling.progress(store),
            "labelers": store.labelers()}


# --- static UI -------------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


def main():
    import uvicorn
    print(f"labeling app on http://127.0.0.1:{config.LABEL_PORT}  (team password: "
          f"{'set via TRAIN_LABEL_PASSWORD' if config.LABEL_PASSWORD != 'label-dev' else 'label-dev'})")
    uvicorn.run(app, host=config.LABEL_HOST, port=config.LABEL_PORT)


if __name__ == "__main__":
    main()
