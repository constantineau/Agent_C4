"""One-at-a-time background jobs with status polling.

Long Lab work (the v2 synthesis fan ~10 min, a retro fleet batch ~1-2 h, a deep model-skill
backfill ~1 h) can't run inside a request — the nginx gateway caps a request at ~300 s — so an
endpoint starts a NAMED job in a daemon thread and the UI polls its status. Each name is
single-flight: starting while one is running is refused (the heavy jobs saturate the container
CPU anyway). This replaces the near-identical `_JOB`/`_JOB_LOCK` blocks synthesis.py and
retro.py used to carry.
"""
import threading
import time

_JOBS: dict = {}       # name -> {"state": idle|running|done|error, ...}
_LOCK = threading.Lock()


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
        job.clear()
        job.update({"state": "running", "progress": [], "started_at": time.time(),
                    **(meta or {})})

    def _progress(msg):
        job.setdefault("progress", []).append(str(msg))
        del job["progress"][:-keep_progress]

    def _work():
        try:
            job.update({"state": "done", result_key: work(_progress)})
        except Exception as exc:      # noqa: BLE001 — the job must record any failure
            job.update({"state": "error", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "state": "running", **(meta or {})}


def status(name: str, progress_tail: int = 12) -> dict:
    """A snapshot of the named job ({"state": "idle"} if never started)."""
    out = dict(_JOBS.get(name) or {"state": "idle"})
    if "progress" in out:
        out["progress"] = list(out.get("progress") or [])[-progress_tail:]
    return out
