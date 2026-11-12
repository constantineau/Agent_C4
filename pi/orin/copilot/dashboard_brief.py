"""Dashboard brief — the LLM layer behind the crew dashboard grid (Phase 3).

The iPad dashboard owns the truth: every tile already has a live value and a DETERMINISTIC
status (ok/watch/act) computed by the boat's instruments, and it works fully with the LLM off.
This endpoint adds the *interpretation* layer the design calls for: given the dashboard's
current tiles, the onboard LLM (Orin) writes the commentary panel — a focus headline + a few
ordered "what matters now" notes — and may RAISE a tile's status when context warrants, with a
reason (never lower it: the instruments own "fine"). It NEVER invents numbers and may only reference the tiles it was given (grounding); on
any LLM failure it returns mode "deterministic" and the dashboard keeps its own engine-read
commentary. This keeps the engine as the source of truth and the LLM strictly as interpreter.
"""
import json
import re

from . import boatname, config, ground
from .llm import LLMClient, LLMUnavailable

_STATUSES = {"ok", "watch", "act"}
_SEV = {"ok": 0, "watch": 1, "act": 2}        # an adjust may only move a tile UP this scale
_CONF = {"high", "med", "low"}

# All-clear wording. On a tile the instruments rate watch/act, a note saying "fine"/"normal"/"no
# action" tells the crew the opposite of the tile (#53: "bank is fine" under a red bank tile). The
# model may add caution, never take it away, so such a note (or focus) is dropped and the
# dashboard keeps its own engine-read text. Errs toward dropping ("not fine" is dropped too).
_ALL_CLEAR = re.compile(
    r"\b(fine|ok|okay|normal|healthy|good|nominal|stable|safe|fresh|plenty|comfortable|"
    r"all\s+(good|clear|set)|on\s+track|under\s+control|no\s+(action|problem|issue|concern|worry|need)s?|"
    r"nothing\s+to\s+(do|worry)|not\s+(a\s+)?(concern|problem|issue|worry))\b", re.I)


def _all_clear(text: str) -> bool:
    return bool(_ALL_CLEAR.search(text or ""))

# Shared guidance on how to read the dashboard tiles correctly (the 7B otherwise misreads trends).
_READING = (
    "How to read the tiles: values are in the units shown (kts = knots of wind/boat speed). Some "
    "tiles list several time points — 'TWS Trend' shows Now, then −60 min and −120 min (those are "
    "the PAST: −120 min was two hours ago); 'Forecast' shows Now, then +60 min and +120 min (the "
    "FUTURE). To call a trend, compare Now against the other values: wind is BUILDING when Now is "
    "HIGHER than the earlier/older numbers and EASING when LOWER — state the direction correctly. "
    "Two tiles are about ENERGY and they are different things: 'Crew' is the HUMAN driver's tank "
    "(0-100, higher is fresher, fix it with a helm rotation), while 'House Bank' is the BOAT's "
    "battery in volts (lower is worse, fix it by shedding load or running a charge source). Never "
    "swap them: a flat bank is what silently killed the data recorder and the AIS transceiver "
    "mid-race on 2026-07-18. "
    "A status of 'act' needs action now, 'watch' needs attention, 'ok' is fine. Never invent numbers."
)

_SYSTEM = (
    "You are the onboard tactical copilot for {boat}. You are shown the crew's "
    "race dashboard: tiles each with a current value and a deterministic status (ok/watch/act) "
    "already computed by the boat's instruments. You do NOT do the math — you INTERPRET what the "
    "instruments report. " + _READING + "\n\nYour job:\n"
    "1. Write a short 'focus' headline (<= 12 words) on what matters most right now.\n"
    "2. Give 2-3 'notes', most important first, each about ONE tile — one short, plain sentence "
    "(about 12 words), leading with what to do or watch, keeping the numbers, and consistent with "
    "that tile's value. No jargon. Reference only the given tiles (use their key).\n"
    "3. Optionally 'adjust' a tile's status UP (ok to watch, or watch to act) ONLY when the "
    "situation clearly warrants more caution than the thresholds show, each with a one-line reason. "
    "Never lower a status. Usually adjust nothing.\n"
    "WIND LANGUAGE: never say 'veer', 'veered', 'back' or 'backed' — say the wind shifted "
    "RIGHT/LEFT, and whenever you "
    "mention a wind-direction change state the baseline→now in degrees (e.g. 'from 250° to 265°'), "
    "never a bare delta or current value.\n"
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
    "You are the onboard tactical copilot for {boat}. The crew tapped one "
    "dashboard tile for a closer look. " + _READING + "\n\n"
    "In 1-3 short, plain sentences (about 12 words each; no JSON, no lists), lead with what the crew "
    "should do or watch, then what this tile means RIGHT NOW — specific to its actual values, keeping "
    "the numbers, calm and practical like a navigator. No jargon. Say the wind shifted right or left, "
    "never veer/veered/back/backed. If the crew asked a question, answer it directly. Stay on this "
    "tile's topic."
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


def _validate(obj, tiles):
    """Ground the LLM output against the tiles it was shown (`tiles`: {key: tile}).

    - A note must name a real tile and may carry only numbers THAT tile shows (wind 12 kts plus
      bank 25 V must not ground "wind now 25 kts"); the focus may use any tile's numbers (a dropped focus/note leaves the dashboard's own engine-read text).
    - A note on a watch/act tile may not sound all-clear ("fine", "normal", "no action"), and the
      focus may not while any tile is at watch/act: that would contradict the instruments.
    - A status adjust may only RAISE a tile (ok → watch → act), never lower it and never touch an
      n/a tile. The instruments own "fine"; the LLM can add caution, never take it away (#176)."""
    if not isinstance(obj, dict):
        return None
    facts = list(tiles.values())
    focus = str(obj.get("focus") or "").strip()
    alarmed = any(_SEV.get(str(t.get("status") or "").strip().lower(), 0) > 0 for t in facts)
    if not ground.grounded(focus, facts) or (alarmed and _all_clear(focus)):
        focus = ""
    notes_out = []
    for n in (obj.get("notes") or [])[:3]:
        if not isinstance(n, dict):
            continue
        tile = str(n.get("tile") or "").strip()
        text = str(n.get("text") or "").strip()
        sev = _SEV.get(str(tiles.get(tile, {}).get("status") or "").strip().lower(), 0)
        if sev > 0 and _all_clear(text):
            continue                  # contradicts the tile's own watch/act status
        if tile in tiles and text and ground.grounded(text, tiles[tile]):
            conf = str(n.get("confidence") or n.get("conf") or "med").strip().lower()
            notes_out.append({"tile": tile, "text": text, "conf": conf if conf in _CONF else "med"})
    adj_out = []
    for a in (obj.get("adjust") or []):
        if not isinstance(a, dict):
            continue
        tile = str(a.get("tile") or "").strip()
        status = str(a.get("status") or "").strip().lower()
        reason = str(a.get("reason") or "").strip()
        engine = str((tiles.get(tile) or {}).get("status") or "").strip().lower()
        if (tile in tiles and status in _STATUSES and reason and engine in _SEV
                and _SEV[status] > _SEV[engine] and ground.grounded(reason, tiles[tile])):
            # `from` is the status the model saw: the iPad drops the raise once the tile moves.
            adj_out.append({"tile": tile, "status": status, "reason": reason, "from": engine})
    if not focus and not notes_out:
        return None      # nothing usable → let the dashboard fall back to engine-read
    return {"focus": focus, "notes": notes_out, "adjust": adj_out}


def make(tiles):
    """tiles: [{key, name, value, sub, status}] from the dashboard. Returns the LLM commentary
    + grounded status adjustments, or {mode: 'deterministic'} when the LLM can't help."""
    tiles = [t for t in (tiles or []) if isinstance(t, dict) and t.get("key")]
    by_key = {t["key"]: t for t in tiles}
    known = set(by_key)
    if not config.USE_LLM or not known:
        return {"mode": "deterministic", "reason": "llm disabled" if known else "no tiles"}

    compact = [{"key": t.get("key"), "name": t.get("name"), "value": t.get("value"),
                "sub": t.get("sub"), "status": t.get("status")} for t in tiles]
    user = "Dashboard tiles right now:\n" + json.dumps(compact, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _SYSTEM.replace("{boat}", boatname.describe())},
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

    parsed = _validate(_extract_json(msg.get("content") or ""), by_key)
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
            [{"role": "system", "content": _DETAIL_SYSTEM.replace("{boat}", boatname.describe())}, {"role": "user", "content": user}]):
            yield delta
    except LLMUnavailable:
        return
