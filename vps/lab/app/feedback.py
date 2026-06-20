"""feedback — turn a crew bug report / feature request into a GitHub issue.

Both the C4 Performance Lab (lab.racertracer.net) and the crew dashboard (c4.racertracer.net) embed a
small feedback widget; both POST here, and we file the report as an issue on the monorepo so it lands
in one backlog. Dependency-free: the GitHub REST API over urllib with a token from `GITHUB_TOKEN`
(repo from `GITHUB_REPO`, default the Agent_C4 monorepo). No token → a clear, non-fatal error so the
widget can tell the user instead of silently dropping feedback.
"""
import json
import os
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPO", "constantineau/Agent_C4")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
API = "https://api.github.com"

TYPE_LABELS = {"bug": "bug", "feature": "enhancement", "idea": "enhancement", "other": "question"}
MAX_TITLE = 160
MAX_BODY = 8000

# crude in-process rate limit (the endpoint is unauthenticated) — N issues per window per process
_RL_MAX = int(os.environ.get("FEEDBACK_RATE_MAX", "12"))
_RL_WINDOW_S = float(os.environ.get("FEEDBACK_RATE_WINDOW_S", "600"))
_recent: list = []


def configured() -> bool:
    return bool(TOKEN)


def _rate_ok(now: float) -> bool:
    global _recent
    _recent = [t for t in _recent if now - t < _RL_WINDOW_S]
    if len(_recent) >= _RL_MAX:
        return False
    _recent.append(now)
    return True


def create_issue(kind: str, title: str, body: str, source: str = "", context: dict = None,
                 now: float = None) -> dict:
    """File a feedback issue. Returns {ok, url, number} or {ok:False, error}. Never raises."""
    now = now if now is not None else time.time()
    kind = (kind or "other").strip().lower()
    title = (title or "").strip()[:MAX_TITLE]
    body = (body or "").strip()[:MAX_BODY]
    if not title:
        return {"ok": False, "error": "a short title is required"}
    if not configured():
        return {"ok": False, "error": "feedback is not configured on the server (no GITHUB_TOKEN)"}
    if not _rate_ok(now):
        return {"ok": False, "error": "too many reports just now — please try again shortly"}

    prefix = {"bug": "🐛", "feature": "✨", "idea": "💡"}.get(kind, "📝")
    ctx = context or {}
    meta_lines = [f"- **type:** {kind}", f"- **source:** {source or 'unknown'}"]
    for k in ("app", "page", "url", "userAgent", "viewport", "reportedAt"):
        if ctx.get(k):
            meta_lines.append(f"- **{k}:** {str(ctx[k])[:300]}")
    full_body = (body or "_(no description provided)_") + "\n\n---\n_Filed from the in-app feedback "\
        "widget._\n" + "\n".join(meta_lines)
    labels = ["feedback-widget", TYPE_LABELS.get(kind, "question")]
    if source:
        labels.append("src:" + source[:30])

    payload = json.dumps({"title": f"{prefix} {title}", "body": full_body,
                          "labels": labels}).encode()
    req = urllib.request.Request(
        f"{API}/repos/{REPO}/issues", data=payload, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Agent_C4-feedback/1.0",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        return {"ok": True, "url": d.get("html_url"), "number": d.get("number")}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("message", "")
        except Exception:
            pass
        return {"ok": False, "error": f"GitHub API {e.code}: {detail or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"could not reach GitHub: {e}"}
