"""Dashboard brief — the LLM layer behind the crew dashboard grid (Phase 3).

The iPad dashboard owns the truth: every tile already has a live value and a DETERMINISTIC
status (ok/watch/act) computed by the boat's instruments, and it works fully with the LLM off.
This endpoint adds the *interpretation* layer the design calls for: given the dashboard's
current tiles, the onboard LLM (Orin) writes the commentary panel — a focus headline + a few
ordered "what matters now" notes — and may nudge a tile's status when context warrants, with a
reason. It NEVER invents numbers and may only reference the tiles it was given (grounding); on
any LLM failure it returns mode "deterministic" and the dashboard keeps its own engine-read
commentary. This keeps the engine as the source of truth and the LLM strictly as interpreter.
"""
import json

from . import config
from .llm import LLMClient, LLMUnavailable

_STATUSES = {"ok", "watch", "act"}
_CONF = {"high", "med", "low"}

_SYSTEM = (
    "You are the onboard tactical copilot for the SR33 racing yacht 'C4'. You are shown the "
    "crew's race dashboard: a set of tiles, each with a current value and a deterministic status "
    "(ok, watch, or act) already computed by the boat's instruments. You do NOT do the math and "
    "you NEVER invent numbers — you interpret what the instruments report. Your job:\n"
    "1. Write a short 'focus' headline (<= 12 words) on what matters most to the crew right now.\n"
    "2. Give 2-3 'notes', most important first, each about ONE tile — short, concrete, what to do "
    "or watch. Reference only the tiles you were given (use their key).\n"
    "3. Optionally 'adjust' a tile's status (ok/watch/act) ONLY when the situation clearly "
    "warrants nuance the thresholds miss, each with a one-line reason. Usually adjust nothing.\n"
    "Be calm and practical, like a good navigator. Respond with ONLY a JSON object."
)


def _extract_json(text: str):
    """Pull the first {...} object out of the model's reply (robust to stray prose)."""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


def _validate(obj, known_keys):
    """Ground the LLM output: drop anything referencing an unknown tile or an illegal status."""
    if not isinstance(obj, dict):
        return None
    focus = str(obj.get("focus") or "").strip()
    notes_out = []
    for n in (obj.get("notes") or [])[:3]:
        if not isinstance(n, dict):
            continue
        tile = str(n.get("tile") or "").strip()
        text = str(n.get("text") or "").strip()
        if tile in known_keys and text:
            conf = str(n.get("confidence") or n.get("conf") or "med").strip().lower()
            notes_out.append({"tile": tile, "text": text, "conf": conf if conf in _CONF else "med"})
    adj_out = []
    for a in (obj.get("adjust") or []):
        if not isinstance(a, dict):
            continue
        tile = str(a.get("tile") or "").strip()
        status = str(a.get("status") or "").strip().lower()
        reason = str(a.get("reason") or "").strip()
        if tile in known_keys and status in _STATUSES and reason:
            adj_out.append({"tile": tile, "status": status, "reason": reason})
    if not focus and not notes_out:
        return None      # nothing usable → let the dashboard fall back to engine-read
    return {"focus": focus, "notes": notes_out, "adjust": adj_out}


def make(tiles):
    """tiles: [{key, name, value, sub, status}] from the dashboard. Returns the LLM commentary
    + grounded status adjustments, or {mode: 'deterministic'} when the LLM can't help."""
    tiles = [t for t in (tiles or []) if isinstance(t, dict) and t.get("key")]
    known = {t["key"] for t in tiles}
    if not config.USE_LLM or not known:
        return {"mode": "deterministic", "reason": "llm disabled" if known else "no tiles"}

    compact = [{"key": t.get("key"), "name": t.get("name"), "value": t.get("value"),
                "sub": t.get("sub"), "status": t.get("status")} for t in tiles]
    user = ("Dashboard tiles right now:\n" + json.dumps(compact, ensure_ascii=False) +
            "\n\nRespond with ONLY this JSON shape:\n"
            '{"focus": "<headline>", '
            '"notes": [{"tile": "<key>", "text": "<short note>", "confidence": "high|med|low"}], '
            '"adjust": [{"tile": "<key>", "status": "ok|watch|act", "reason": "<why>"}]}')

    try:
        msg = LLMClient().chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            json_object=True,
        )
    except LLMUnavailable as e:
        return {"mode": "deterministic", "reason": "llm unavailable: " + str(e)[:120]}

    parsed = _validate(_extract_json(msg.get("content") or ""), known)
    if not parsed:
        return {"mode": "deterministic", "reason": "ungrounded or unparseable reply"}
    parsed["mode"] = "llm"
    parsed["model"] = config.LLM_MODEL
    return parsed
