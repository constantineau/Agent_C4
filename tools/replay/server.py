"""Race Rewind — the review server.

Serves three things over one origin so the console's relative `/api/...` calls can be shimmed:

  1. **The real crew console**, straight out of `pi/console/dashboard/`, with a small shim
     injected ahead of `dashboard.js`. Nothing in the console is forked or reimplemented — what
     you are looking at is the shipped iPad code, so a criticism of it is a criticism of the
     real thing and a fix to it is a real fix.
  2. **Ground truth** (`truth.json`) — raw archive readings per source, no engine.
  3. **Annotations** — timestamped notes you write while scrubbing, appended to `notes.jsonl`
     next to the timeline and exportable as Markdown for the v2 backlog.

Frames are parsed once into memory and served one at a time, so the browser never downloads the
whole ~30 MB timeline and scrubbing costs a localhost round trip.

Usage:
    python tools/replay/server.py --timeline /home/constantineau/backups/replay-jul18/timeline
    # then open http://localhost:8110/
"""
import argparse
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CONSOLE = os.path.join(ROOT, "pi", "console", "dashboard")
WEB = os.path.join(HERE, "web")

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".map": "application/json"}

STATE = {"frames": [], "manifest": {}, "truth": {}, "timeline_dir": None}


def load(timeline_dir):
    STATE["timeline_dir"] = timeline_dir
    frames_path = os.path.join(timeline_dir, "frames.jsonl")
    frames, bad = [], 0
    with open(frames_path) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                frames.append(json.loads(line))
            except ValueError:
                bad += 1          # a build still in flight leaves one partial trailing line
    STATE["frames"] = frames
    if bad:
        print(f"[review] skipped {bad} unparseable line(s) — is the build still running?")
    for name, key in (("manifest.json", "manifest"), ("truth.json", "truth")):
        p = os.path.join(timeline_dir, name)
        STATE[key] = json.load(open(p)) if os.path.exists(p) else {}
    print(f"[review] {len(STATE['frames'])} frames from {frames_path}")
    if not STATE["truth"]:
        print("[review] NOTE: no truth.json — run tools/replay/truth.py for the ground-truth pane")
    else:
        n_t = len(STATE["truth"].get("t") or [])
        if n_t != len(STATE["frames"]):
            # The panes are indexed by the same frame number, so a length mismatch silently
            # blanks the ground-truth side. Almost always: the two were built over different
            # windows, or the server was started while the frame build was still running.
            print(f"[review] WARNING: {len(STATE['frames'])} frames but {n_t} truth stamps — "
                  f"rebuild both over the same --start/--end/--step, or the truth pane will "
                  f"be blank past frame {min(n_t, len(STATE['frames']))}")
    return STATE


def _inject_shim(html):
    """Put the shim ahead of dashboard.js so it can patch fetch/setInterval before the app runs."""
    return re.sub(r'(<script src="dashboard\.js)',
                  '<script src="/static/replay-shim.js"></script>\n  \\1', html, count=1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # quiet — the scrub loop would flood the terminal
        pass

    # --- helpers ---------------------------------------------------------
    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, transform=None):
        if not os.path.isfile(path):
            return self._send({"error": "not found", "path": path}, status=404)
        mode = "r" if transform else "rb"
        with open(path, mode) as fh:
            body = fh.read()
        if transform:
            body = transform(body)
        self._send(body, MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    # --- routes ----------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)

        if p == "/":
            return self._file(os.path.join(WEB, "review.html"))
        if p.startswith("/static/"):
            return self._file(os.path.join(WEB, os.path.basename(p)))
        if p in ("/dashboard", "/dashboard/"):
            return self._file(os.path.join(CONSOLE, "index.html"), transform=_inject_shim)
        if p.startswith("/dashboard/"):
            return self._file(os.path.join(CONSOLE, os.path.basename(p).split("?")[0]))
        if p == "/sun.js":
            return self._file(os.path.join(ROOT, "vps", "web", "public", "sun.js"))
        if p == "/config.js":
            return self._send("window.SR33_ONBOARD = true;", MIME[".js"])

        if p == "/replay/index":
            return self._send({
                "manifest": STATE["manifest"],
                "count": len(STATE["frames"]),
                "t": [f["t"] for f in STATE["frames"]],
            })
        if p == "/replay/frame":
            i = max(0, min(int(q.get("i", ["0"])[0]), len(STATE["frames"]) - 1))
            return self._send(STATE["frames"][i])
        if p == "/replay/truth":
            return self._send(STATE["truth"])
        if p == "/replay/notes":
            return self._send(self._notes())
        if p == "/replay/notes.md":
            return self._send(self._notes_md(), "text/markdown; charset=utf-8")

        # The console's own calls. Frames answer /api/*; the copilot is gone (never archived),
        # and 404 is the right answer — dashboard.js already falls back to deterministic text.
        if p.startswith("/api/") or p.startswith("/copilot/"):
            return self._send({"error": "replay: served from frames, not live"}, status=404)
        return self._send({"error": "not found", "path": p}, status=404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if u.path == "/replay/notes":
            try:
                note = json.loads(raw)
            except ValueError:
                return self._send({"error": "bad json"}, status=400)
            with open(os.path.join(STATE["timeline_dir"], "notes.jsonl"), "a") as fh:
                fh.write(json.dumps(note) + "\n")
            return self._send({"ok": True, "count": len(self._notes())})
        return self._send({"error": "not found"}, status=404)

    # --- annotations -----------------------------------------------------
    def _notes(self):
        p = os.path.join(STATE["timeline_dir"], "notes.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _notes_md(self):
        notes = self._notes()
        if not notes:
            return "# Race Rewind notes\n\n_(none yet)_\n"
        by_tile = {}
        for x in notes:
            by_tile.setdefault(x.get("tile") or "general", []).append(x)
        out = ["# Race Rewind notes — Bayview Mackinac 2026 replay", "",
               f"{len(notes)} note(s) across {len(by_tile)} area(s). "
               "Each is anchored to a race timestamp with the numbers that were on screen.", ""]
        for tile in sorted(by_tile):
            out.append(f"## {tile}")
            out.append("")
            for x in sorted(by_tile[tile], key=lambda r: r.get("t") or ""):
                out.append(f"- **{x.get('t', '?')}** — {x.get('text', '').strip()}")
                if x.get("context"):
                    out.append(f"  - context: `{x['context']}`")
            out.append("")
        return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Race Rewind review server.")
    ap.add_argument("--timeline", required=True, help="directory holding frames.jsonl")
    ap.add_argument("--port", type=int, default=8110)
    a = ap.parse_args()
    load(a.timeline)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"[review] http://localhost:{a.port}/   (console iframe at /dashboard/)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
