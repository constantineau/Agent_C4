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

# Shared guidance on how to read the dashboard tiles correctly (the 7B otherwise misreads trends).
_READING = (
    "How to read the tiles: values are in the units shown (kts = knots of wind/boat speed). Some "
    "tiles list several time points — 'TWS Trend' shows Now, then −60 min and −120 min (those are "
    "the PAST: −120 min was two hours ago); 'Forecast' shows Now, then +60 min and +120 min (the "
    "FUTURE). To call a trend, compare Now against the other values: wind is BUILDING when Now is "
    "HIGHER than the earlier/older numbers and EASING when LOWER — state the direction correctly. "
    "A status of 'act' needs action now, 'watch' needs attention, 'ok' is fine. Never invent numbers."
)

_SYSTEM = (
    "You are the onboard tactical copilot for the SR33 racing yacht 'C4'. You are shown the crew's "
    "race dashboard: tiles each with a current value and a deterministic status (ok/watch/act) "
    "already computed by the boat's instruments. You do NOT do the math — you INTERPRET what the "
    "instruments report. " + _READING + "\n\nYour job:\n"
    "1. Write a short 'focus' headline (<= 12 words) on what matters most right now.\n"
    "2. Give 2-3 'notes', most important first, each about ONE tile — short, concrete, what to do "
    "or watch, and consistent with that tile's value. Reference only the given tiles (use their key).\n"
    "3. Optionally 'adjust' a tile's status (ok/watch/act) ONLY when the situation clearly warrants "
    "nuance the thresholds miss, each with a one-line reason. Usually adjust nothing.\n"
    "Be calm and practical, like a good navigator. Respond with ONLY a JSON object."
)

# JSON schema for constrained decoding — guarantees the brief's shape (no malformed/parse failures).
_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "focus": {"type": "string"},
        "notes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"tile": {"type": "string"}, "text": {"type": "string"},
                           "confidence": {"type": "string", "enum": ["high", "med", "low"]}},
            "required": ["tile", "text", "confidence"]}},
        "adjust": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"tile": {"type": "string"}, "status": {"type": "string", "enum": ["ok", "watch", "act"]},
                           "reason": {"type": "string"}},
            "required": ["tile", "status", "reason"]}},
    },
    "required": ["focus", "notes", "adjust"],
}

# One worked example (few-shot) — teaches the JSON shape, grounding to given keys, and especially
# reading a trend correctly (9→18 = BUILDING). Keep it to one to limit prefill on the 7B.
_EXAMPLE_USER = ('Dashboard tiles right now:\n' + json.dumps([
    {"key": "wind", "name": "TWS Trend", "value": "Now 18 kts; -60 min 12 kts; -120 min 9 kts", "sub": "", "status": "watch"},
    {"key": "sail", "name": "Sail", "value": "J1", "sub": "in range", "status": "ok"},
    {"key": "data", "name": "Data", "value": "5", "sub": "sources live", "status": "ok"},
]))
_EXAMPLE_ASSISTANT = json.dumps({
    "focus": "Breeze building fast — stay ahead of the gear.",
    "notes": [
        {"tile": "wind", "text": "Wind has built from 9 to 18 kts over the last two hours and is still rising — ease/depower and plan the next sail down.", "confidence": "high"},
        {"tile": "sail", "text": "J1 is right for now, but ready the smaller headsail as it keeps building.", "confidence": "med"},
    ],
    "adjust": [],
})

_DETAIL_SYSTEM = (
    "You are the onboard tactical copilot for the SR33 racing yacht 'C4'. The crew tapped one "
    "dashboard tile for a closer look. " + _READING + "\n\n"
    "In 1-3 short sentences (no JSON, no lists, plain prose), explain what this tile means RIGHT NOW "
    "and what the crew should do or watch — specific to its actual values, calm and practical like a "
    "navigator. If the crew asked a question, answer it directly. Stay on this tile's topic."
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
    user = "Dashboard tiles right now:\n" + json.dumps(compact, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _EXAMPLE_USER},        # few-shot: the worked example…
        {"role": "assistant", "content": _EXAMPLE_ASSISTANT},
        {"role": "user", "content": user},                 # …then the real tiles
    ]
    try:
        # schema-constrained decoding guarantees the JSON shape; falls back to json_object if the
        # runtime ignores the schema (still parsed/validated below either way).
        msg = LLMClient().chat(messages, schema=_SCHEMA, json_object=True)
    except LLMUnavailable as e:
        return {"mode": "deterministic", "reason": "llm unavailable: " + str(e)[:120]}

    parsed = _validate(_extract_json(msg.get("content") or ""), known)
    if not parsed:
        return {"mode": "deterministic", "reason": "ungrounded or unparseable reply"}
    parsed["mode"] = "llm"
    parsed["model"] = config.LLM_MODEL
    return parsed


def detail_stream(domain, question, tiles):
    """Generator: a scoped, streamed explanation of one tile (the tap-to-detail deep-dive).
    `domain` is the tile key; `tiles` is the full dashboard snapshot for context. Yields text
    deltas; yields nothing if the LLM is unavailable (the dashboard keeps its deterministic WHY)."""
    if not config.USE_LLM:
        return
    tiles = [t for t in (tiles or []) if isinstance(t, dict) and t.get("key")]
    focus = next((t for t in tiles if t.get("key") == domain), None)
    if not focus:
        return
    user = ("The crew tapped the '" + str(focus.get("name") or domain) + "' tile.\n"
            "That tile right now: " + json.dumps(focus, ensure_ascii=False) + "\n"
            "Other tiles for context: " + json.dumps([t for t in tiles if t.get("key") != domain], ensure_ascii=False))
    if question and str(question).strip():
        user += "\n\nThe crew asks: " + str(question).strip()
    try:
        for delta in LLMClient().chat_stream(
            [{"role": "system", "content": _DETAIL_SYSTEM}, {"role": "user", "content": user}]):
            yield delta
    except LLMUnavailable:
        return
