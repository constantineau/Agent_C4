"""No-dependency runner for the copilot's dashboard brief (Phase 3).

The full copilot is a FastAPI app (pi/orin/copilot/app.py); on a bare Orin without fastapi/
uvicorn this stdlib HTTP server exposes just what the crew dashboard needs — POST /dashboard
(LLM commentary + grounded status nudges) and GET /health — by calling the exact same
`copilot.dashboard_brief.make()` logic. Pure stdlib + the urllib LLM client, so it runs with
nothing installed. Listens on :8300 (the copilot port); reaches the local Ollama via env.

Run from this directory (pi/orin) so the `copilot` package imports:
    COPILOT_USE_LLM=true LLM_BASE_URL=http://127.0.0.1:11434/v1 python3 serve_dashboard.py
or via pi/systemd/sr33-orin-copilot-dashboard.service.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `copilot` package imports
from copilot import config, copilot as copilot_mod, dashboard_brief  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send({"status": "ok", "service": "copilot-dashboard", "model": config.LLM_MODEL,
                        "llm": config.LLM_BASE_URL, "use_llm": config.USE_LLM})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        path = self.path.rstrip("/")
        if path == "/dashboard":
            try:
                tiles = json.loads(raw).get("tiles", [])
            except Exception:
                tiles = []
            try:
                self._send(dashboard_brief.make(tiles))
            except Exception as e:                       # never 500 the dashboard — degrade
                self._send({"mode": "deterministic", "reason": "server error: " + str(e)[:120]})
        elif path == "/strategy":
            # in-race STRATEGY SYNTHESIS — the LLM phrases the engine's deterministic cross-signal
            # digest (narrative + condition-matching only; never re-recommends). Never 500 — the
            # copilot already falls back to the deterministic digest internally on any LLM trouble.
            try:
                body = json.loads(raw)
            except Exception:
                body = {}
            try:
                self._send(copilot_mod.strategy_brief(route=body.get("route"),
                                                      hoisted=body.get("hoisted"),
                                                      use_llm=body.get("use_llm")))
            except Exception as e:
                self._send({"available": False, "mode": "deterministic",
                            "reason": "server error: " + str(e)[:120]})
        elif path == "/detail":
            # streamed tile deep-dive — text/plain, flushed token-by-token (X-Accel-Buffering:no
            # tells nginx not to buffer so the words arrive live).
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for chunk in dashboard_brief.detail_stream(
                        payload.get("domain"), payload.get("question"), payload.get("tiles", [])):
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except Exception:
                pass     # client keeps the deterministic WHY it already showed
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = config.COPILOT_PORT
    print(f"copilot dashboard server on :{port} (model {config.LLM_MODEL}, llm {config.LLM_BASE_URL})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
