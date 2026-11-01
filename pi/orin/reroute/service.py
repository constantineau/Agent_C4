"""Onboard re-route service (Orin): the Lab's full route optimizer, run ONLY when asked.

A small HTTP API on the Orin's end of the direct Pi↔Orin cable. The Pi engine is its only client
(POST /reroute/run and GET /reroute proxy here); a crew button on the iPad will call those (#125).
There is no timer and no trigger: nothing runs unless a request asks for it, and only one run at a
time. Each run is a child process (job.py) so its memory goes back to the OS when it ends.

    POST /run      body: the run request (see README.md) → 202 {ok, job_id}
                   409 {busy, job_id, progress} while a run is going
    GET  /status   → {state: idle|running|done|error, job_id, progress, last, result_job_id}
    GET  /result   → the last finished route (c4.reroute/v1), 404 if none yet
    GET  /health   → {ok: true}

Only the addresses in REROUTE_ALLOW_FROM may call it (default: the Pi's cable address and
loopback). Run: PYTHONPATH=<repo>/vps/lab:<repo> python3 service.py   (see README.md)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("REROUTE_DIR", os.path.join(os.path.expanduser("~"), "c4-reroute"))
BIND = os.environ.get("REROUTE_BIND", "10.10.10.2")
PORT = int(os.environ.get("REROUTE_PORT", "8210"))
ALLOW_FROM = {a.strip() for a in os.environ.get("REROUTE_ALLOW_FROM", "10.10.10.1,127.0.0.1")
              .split(",") if a.strip()}
JOB_TIMEOUT_S = float(os.environ.get("REROUTE_JOB_TIMEOUT_S", "600"))
EXPECTED_S = float(os.environ.get("REROUTE_EXPECTED_S", "75"))     # until a run has been timed
MAX_BODY = 2 * 1024 * 1024
KEEP_JOBS = 3

# crew-facing words (short and plain — they reach the iPad as-is)
STEPS = {"start": "Starting", "chart": "Checking the chart", "weather": "Loading forecasts",
         "routing": "Finding the fastest route", "saving": "Saving the route"}
BUSY = "A re-route is already running."
FAILED = "Re-route failed. Try again."
TOO_LONG = "Re-route took too long. Try again."


def _log(msg):
    print(f"[reroute] {msg}", flush=True)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


class Jobs:
    """One run at a time. `runner(job_dir) -> Popen-like` starts a run (tests swap it)."""

    def __init__(self, state_dir=STATE_DIR, runner=None, timeout_s=JOB_TIMEOUT_S, clock=time.time):
        self.dir = state_dir
        self.jobs_dir = os.path.join(state_dir, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self.runner = runner or self._spawn
        self.timeout_s = timeout_s
        self.clock = clock
        self.lock = threading.Lock()
        self.current = None
        self.last = _read_json(os.path.join(state_dir, "last.json"))
        # the job behind /result: a later failed run changes `last`, not this
        self.last_done = (_read_json(os.path.join(state_dir, "last_result.json")) or {}).get("job_id")

    # -- start -----------------------------------------------------------------------------------
    @staticmethod
    def _spawn(job_dir):
        log = open(os.path.join(job_dir, "log.txt"), "ab")
        try:
            return subprocess.Popen([sys.executable, os.path.join(HERE, "job.py"), job_dir],
                                    cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()

    def start(self, req):
        """→ (http status, body)."""
        with self.lock:
            self._reap()
            if self.current is not None:
                return 409, {"ok": False, "busy": True, "detail": BUSY,
                             "job_id": self.current["job_id"], "progress": self._progress()}
            job_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self.clock())) + "-" + \
                uuid.uuid4().hex[:6]
            job_dir = os.path.join(self.jobs_dir, job_id)
            os.makedirs(job_dir)
            req = dict(req, job_id=job_id)
            _write_json(os.path.join(job_dir, "request.json"), req)
            try:
                proc = self.runner(job_dir)
            except Exception as exc:  # noqa: BLE001
                _log(f"could not start a run: {exc}")
                return 500, {"ok": False, "detail": FAILED}
            self.current = {"job_id": job_id, "dir": job_dir, "proc": proc,
                            "started_at": self.clock(), "asked_by": req.get("device")}
            _log(f"run {job_id} started (asked by {req.get('device') or '?'})")
            self._prune()
            return 202, {"ok": True, "job_id": job_id, "state": "running"}

    # -- finish ----------------------------------------------------------------------------------
    def _reap(self):
        """Close out the current run if it ended (or ran too long). Caller holds the lock."""
        cur = self.current
        if cur is None:
            return
        rc = cur["proc"].poll()
        elapsed = self.clock() - cur["started_at"]
        if rc is None and elapsed <= self.timeout_s:
            return
        last = {"job_id": cur["job_id"], "asked_by": cur.get("asked_by"),
                "started_at": int(cur["started_at"]), "finished_at": int(self.clock())}
        if rc is None:
            for step in (cur["proc"].terminate, cur["proc"].kill):
                try:
                    step()
                    cur["proc"].wait(timeout=5)
                    break
                except Exception:  # noqa: BLE001
                    continue
            last.update(state="error", detail=TOO_LONG)
        else:
            res = _read_json(os.path.join(cur["dir"], "result.json")) if rc == 0 else None
            if res is not None:
                _write_json(os.path.join(self.dir, "last_result.json"), res)
                self.last_done = cur["job_id"]
                last.update(state="done", run_s=res.get("run_s"),
                            available=bool(res.get("available")))
            else:
                err = _read_json(os.path.join(cur["dir"], "error.json")) or {}
                last.update(state="error", detail=str(err.get("detail") or FAILED)[:120])
        _write_json(os.path.join(self.dir, "last.json"), last)
        self.last = last
        self.current = None
        _log(f"run {last['job_id']} {last['state']}"
             + (f": {last.get('detail')}" if last.get("detail") else ""))

    def tick(self):
        with self.lock:
            self._reap()

    def _prune(self):
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError:
            return
        keep = {self.current["job_id"]} if self.current else set()
        for n in names[:-KEEP_JOBS]:
            if n not in keep:
                shutil.rmtree(os.path.join(self.jobs_dir, n), ignore_errors=True)

    # -- read ------------------------------------------------------------------------------------
    def _expected_s(self):
        r = (self.last or {}).get("run_s") if (self.last or {}).get("state") == "done" else None
        return float(r) if r else EXPECTED_S

    def _progress(self):
        cur = self.current
        if cur is None:
            return None
        phase, note = "start", None
        try:
            with open(os.path.join(cur["dir"], "progress.jsonl")) as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    phase = ev.get("phase", phase)
                    note = ev.get("msg", note)
        except OSError:
            pass
        elapsed = self.clock() - cur["started_at"]
        exp = self._expected_s()
        return {"phase": phase, "step": STEPS.get(phase, STEPS["start"]), "note": note,
                "elapsed_s": round(elapsed), "expected_s": round(exp),
                "pct": min(95, int(100 * elapsed / max(1.0, exp)))}

    def status(self):
        with self.lock:
            self._reap()
            if self.current is not None:
                return {"state": "running", "job_id": self.current["job_id"],
                        "asked_by": self.current.get("asked_by"),
                        "progress": self._progress(), "last": self.last,
                        "result_job_id": self.last_done}
            if self.last:
                return {"state": self.last.get("state"), "job_id": self.last.get("job_id"),
                        "progress": None, "last": self.last, "result_job_id": self.last_done}
            return {"state": "idle", "job_id": None, "progress": None, "last": None,
                    "result_job_id": self.last_done}

    def result(self):
        with self.lock:
            self._reap()
        return _read_json(os.path.join(self.dir, "last_result.json"))


def _validate_request(body):
    """The run request's shape → (ok, detail). Details are crew-facing."""
    if not isinstance(body, dict):
        return False, "Bad request."
    pos = body.get("position")
    if not isinstance(pos, dict) or pos.get("lat") is None or pos.get("lon") is None:
        return False, "No boat position yet."
    if not body.get("definition") and not body.get("marks"):
        return False, "No course loaded."
    dev = body.get("device")
    if dev is not None and (not isinstance(dev, str) or len(dev) > 40):
        return False, "Bad device name."
    return True, None


def make_handler(jobs, allow_from=None):
    allow = ALLOW_FROM if allow_from is None else set(allow_from)

    class Handler(BaseHTTPRequestHandler):
        server_version = "c4-reroute/1"

        def log_message(self, fmt, *args):     # quiet: the journal gets our own lines
            return

        def _send(self, code, body):
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self):
            if self.client_address[0] in allow:
                return True
            self._send(403, {"detail": "Not allowed."})
            return False

        def do_GET(self):
            if not self._allowed():
                return
            p = self.path.split("?", 1)[0]
            if p == "/health":
                return self._send(200, {"ok": True, "service": "orin-reroute"})
            if p == "/status":
                return self._send(200, jobs.status())
            if p == "/result":
                r = jobs.result()
                return self._send(200, r) if r else self._send(404, {"detail": "No re-route yet."})
            return self._send(404, {"detail": "Not found."})

        def do_POST(self):
            if not self._allowed():
                return
            if self.path.split("?", 1)[0] != "/run":
                return self._send(404, {"detail": "Not found."})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > MAX_BODY:
                return self._send(400, {"ok": False, "detail": "Bad request."})
            try:
                body = json.loads(self.rfile.read(n).decode())
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"ok": False, "detail": "Bad request."})
            ok, why = _validate_request(body)
            if not ok:
                return self._send(400, {"ok": False, "detail": why})
            code, out = jobs.start(body)
            return self._send(code, out)

    return Handler


def main():
    jobs = Jobs()

    def watchdog():                 # saves a finished run even if nobody polls
        while True:
            time.sleep(1.0)
            try:
                jobs.tick()
            except Exception as exc:  # noqa: BLE001
                _log(f"watchdog: {exc}")

    threading.Thread(target=watchdog, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), make_handler(jobs))
    _log(f"listening on {BIND}:{PORT}; runs only when asked")
    srv.serve_forever()


if __name__ == "__main__":
    main()
