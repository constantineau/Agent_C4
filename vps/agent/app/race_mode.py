"""Server-side, fail-closed race-mode gate (RRS 41 / Bayview Mackinac NOR §2.1(d)).

While RACING, the cloud agent must not deliver customized tactical/routing/performance/navigation
ADVICE computed off-boat — that is prohibited "outside help". It may still serve safety, the boat's
OWN instrument readings, and information available to all boats (e.g. a public forecast) verbatim.
This module is the single source of truth for that decision; agent.py and main.py consult it.

Design:
- One authoritative flag, persisted in `app_state` (key 'race_mode'), value 'race' | 'practice'.
- FAIL-CLOSED: if the flag is missing / unreadable, we treat the boat as RACING (withhold). The
  default for a fresh DB is RACE_MODE_DEFAULT (env; prod omits it -> 'race', dev compose -> 'practice').
- Every withheld request + every mode change is written to `audit_log` (a tamper-evident record for
  a protest committee).

This is the Phase-9 STOPGAP for the cloud build; the real fix is the onboard engine (the boat's own
gear is not an "outside source"). See docs/RRS41_COMPLIANCE.md §4 and docs/ONBOARD_ENGINE_SCOPING.md.
"""
import json
import os

from .db import pool

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
# Fail-closed: unknown -> race. Dev compose sets RACE_MODE_DEFAULT=practice.
DEFAULT_MODE = os.environ.get("RACE_MODE_DEFAULT", "race").strip().lower()
if DEFAULT_MODE not in ("race", "practice"):
    DEFAULT_MODE = "race"

# Tools that compute customized tactical / routing / performance / navigation ADVICE — withheld
# while racing. (Allowed: get_current_conditions, get_strip, get_sources, get_history,
# get_ais_targets, get_alerts, fetch_forecast, get_summaries, log_note — own data, safety, common
# data verbatim, recall, logging.)
GATED_TOOLS = frozenset({
    "get_tactics", "get_route", "get_polar_analysis", "get_polar_target",
    "get_sail_advice", "get_fatigue", "get_navigator", "get_route_status",
})

REFUSAL = (
    "Racing — outside tactical help withheld (RRS 41). I can give safety (traffic/depth/alerts), "
    "your own instrument readings, and information available to all boats. Switch to Practice for "
    "tactics, routing, polar/sail coaching, fatigue, and navigation calls."
)

# Keyword groups for the no-LLM fallback path: if a question matches one of these while racing,
# it would route to a gated tool, so refuse instead. (Mirrors the routing in agent._fallback.)
_GATED_INTENT_KEYWORDS = (
    "tactic", "favored", "favoured", "which side", "which way", "lifted", "headed", "shift", "leverage",
    "route", "routing", "best way", "weather route", "fastest", "which tack first",
    "polar", "target", "where are we slow", "leave speed", "leaving speed", "vs polar",
    "sail", "peel", "kite", "spinnaker", "jib", "hoist", "change up", "change down",
    "fatigue", "tired", "rotate", "helm change", "driver", "shift change",
    "mark", "finish", "eta", "distance", "layline", "lay ", "next leg",
)

_cache: dict = {}  # {'mode': 'race'|'practice'} — populated lazily, updated on set_mode


def _load_mode() -> str:
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = 'race_mode'"
            ).fetchone()
        if row and row["value"] in ("race", "practice"):
            return row["value"]
    except Exception:
        pass  # fail-closed
    return DEFAULT_MODE


def current_mode() -> str:
    if "mode" not in _cache:
        _cache["mode"] = _load_mode()
    return _cache["mode"]


def racing() -> bool:
    return current_mode() == "race"


def set_mode(mode: str, actor: str | None = None) -> str:
    mode = (mode or "").strip().lower()
    if mode not in ("race", "practice"):
        raise ValueError("mode must be 'race' or 'practice'")
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO app_state (key, value, updated_at) VALUES ('race_mode', %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (mode,),
        )
        conn.execute(
            "INSERT INTO audit_log (boat_id, event, detail) VALUES (%s, 'mode_change', %s)",
            (BOAT_ID, json.dumps({"mode": mode, "actor": actor})),
        )
    _cache["mode"] = mode
    return mode


def audit_refusal(channel: str, **detail) -> None:
    """Record that a request was withheld while racing (best-effort; never raise into the caller)."""
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (boat_id, event, detail) VALUES (%s, 'refusal', %s)",
                (BOAT_ID, json.dumps({"channel": channel, **detail})),
            )
    except Exception:
        pass


def is_gated(tool_name: str) -> bool:
    """True if this tool is withheld right now (racing AND in the gated set)."""
    return racing() and tool_name in GATED_TOOLS


def allowed_tools(all_tools: list) -> list:
    """Filter a tool-contract list down to what's permitted in the current mode."""
    if not racing():
        return all_tools
    return [t for t in all_tools if t.get("name") not in GATED_TOOLS]


def gated_intent(message_lower: str) -> bool:
    """Heuristic for the no-LLM fallback: would this question route to a gated tool?"""
    return any(k in message_lower for k in _GATED_INTENT_KEYWORDS)
