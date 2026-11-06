"""One-at-a-time background jobs with status polling.

Long Lab work (the v2 synthesis fan ~10 min, a retro fleet batch ~1-2 h, a deep model-skill
backfill ~1 h) can't run inside a request — the nginx gateway caps a request at ~300 s — so an
endpoint starts a NAMED job in a daemon thread and the UI polls its status. Each name is
single-flight: starting while one is running is refused (the heavy jobs saturate the container
CPU anyway). This replaces the near-identical `_JOB`/`_JOB_LOCK` blocks synthesis.py and
retro.py used to carry.

Every run gets a `job_id`. The Lab is shared, so the slot under a name can hold someone else's
run by the time a page polls; a page that polls WITH its job_id gets its own run (kept for a while
after it finishes, even once the next run takes the slot) or a plain "lost" error, never another
user's progress or result (#182-9).
"""
import threading
import time
import uuid

_JOBS: dict = {}       # name -> {"state": idle|running|done|error, "job_id", ...}
_DONE: dict = {}       # name -> {job_id: finished job snapshot}, the last few per name
_LOCK = threading.Lock()
KEEP_S, KEEP_N = 3600, 4
LOST = "This run was lost (the Lab restarted or it expired). Run it again."


def start(name: str, work, meta: dict = None, result_key: str = "result",
          keep_progress: int = 40) -> dict:
    """Run `work(progress)` in a daemon thread under `name`.

    `work`'s return value lands in the job dict under `result_key`; `progress(msg)` appends to
    a rolling tail the status endpoint serves. Returns {"ok": False, ...} when the named job is
    already running."""
    with _LOCK:
        job = _JOBS.setdefault(name, {"state": "idle"})
        if job.get("state") == "running":
            return {"ok": False, "note": f"a {name} job is already in progress", "job": dict(job)}
        meta = dict(meta or {})
        meta.setdefault("job_id", uuid.uuid4().hex[:12])
        job_id = meta["job_id"]
        job.clear()
        job.update({"state": "running", "progress": [], "started_at": time.time(), **meta})

    def _progress(msg):
        job.setdefault("progress", []).append(str(msg))
        del job["progress"][:-keep_progress]

    def _work():
        try:
            fin = {"state": "done", result_key: work(_progress)}
        except Exception as exc:      # noqa: BLE001 — the job must record any failure
            fin = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
        with _LOCK:
            job.update(fin)
            _keep(name, job_id, dict(job, finished_at=time.time()))

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "state": "running", **meta}


def _keep(name, job_id, snap):
    """Remember a finished run by id (caller holds _LOCK): the last KEEP_N per name, KEEP_S long."""
    kept = _DONE.setdefault(name, {})
    kept[job_id] = snap
    now = time.time()
    for k in [k for k, v in kept.items() if now - v["finished_at"] > KEEP_S]:
        del kept[k]
    while len(kept) > KEEP_N:                        # oldest first (insertion order)
        del kept[next(iter(kept))]


def status(name: str, progress_tail: int = 12, job_id: str = "") -> dict:
    """A snapshot of the named job ({"state": "idle"} if never started). With `job_id`, only that
    run: the slot if it still holds it, else the kept finished run, else a "lost" error."""
    with _LOCK:
        out = dict(_JOBS.get(name) or {"state": "idle"})
        if job_id and out.get("job_id") != job_id:
            kept = (_DONE.get(name) or {}).get(job_id)
            if kept and time.time() - kept["finished_at"] <= KEEP_S:
                out = dict(kept)
            else:
                return {"state": "error", "job_id": job_id, "error": LOST}
    if "progress" in out:
        out["progress"] = list(out.get("progress") or [])[-progress_tail:]
    return out
