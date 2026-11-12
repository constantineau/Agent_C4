"""The bounded tool surface — the ONLY things the LLM is allowed to do.

This is the heart of the decision-support guardrails. The LLM cannot compute, cannot fetch
arbitrary data, cannot take actions. It can only call these read-only engine-fact tools. So
"the engine does the math, the LLM interprets and may recommend" is enforced structurally, not just by prompt:
if the model wants a number it must ask the engine for it, and every tool result is recorded
in a trace that the grounding validator later checks recommendations against.

`TOOL_SPECS` is the OpenAI function-calling schema sent to the model. `dispatch()` runs one
tool call against an `EngineClient` and returns (result_dict, tool_name) — tool_name is what
gets added to the brief's `sources_used`.
"""
from .engine_client import EngineClient

# The route is never the model's to pick: dispatch() pins the brief's own route, so the LLM stays
# on the loaded homework course. None of them write anything.
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_conditions",
            "description": (
                "Current instrument readout (the boat's own sensors): true/apparent wind "
                "(TWS/TWA/TWD/AWS/AWA), boatspeed (STW), SOG, COG, heading, heel, depth, "
                "position, data age + a staleness flag, and the helm fatigue index. Use this "
                "for 'what's happening right now'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_conditions_full",
            "description": (
                "Every source's reading for each channel, with age. Use only to cross-check a "
                "value you suspect is wrong or stale (e.g. two GPS sources disagree)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sources",
            "description": "Which sensors are live and their curated reliability notes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_navigator",
            "description": (
                "Next mark name, distance + bearing to it, ETA, leg type (beat/reach/run), and "
                "the layline call. Use for 'where are we on the course / what's the next mark'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tactics",
            "description": (
                "Tactical read computed from recent wind: lifted vs headed, oscillating vs "
                "persistent shift, favored side, leverage. Use for shift/side decisions."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sail_advice",
            "description": (
                "The optimal sail for the live TWS/TWA against the boat's sail crossovers, plus "
                "whether the sail actually hoisted is right and any imminent peel."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fatigue",
            "description": (
                "Helm fatigue index 0-100 + level (fresh/watch/rotate_soon/rotate_now) + the "
                "components driving it. Use for crew-rotation decisions."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watch",
            "description": (
                "The watch system: which crew team is on deck now, minutes to the next watch "
                "change, who's up next, and the block schedule. Use when timing crew work "
                "(sail changes, roundings) or rotation advice against the watch boundary."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_checklist",
            "description": (
                "The RACE CHECKLIST from the SIs/NOR: required actions with live status "
                "(pending/active/done) and a measure (e.g. 'Cove Island Virtual Gate in 6 nm', "
                "'sunset in 40 min'). Use when asked what the crew must do / has missed — "
                "nav lights, the gate photo, the finish procedure. Never invent requirements."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": (
                "Wind forecast (Open-Meteo — common public data, legal in-race) at the boat's "
                "live position over `hours`. Use for 'what's the breeze doing ahead'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend",
            "description": (
                "Wind TREND from the boat's own archive: 1 h and 3 h build/fade rate (kts/hr) "
                "and which way the breeze is walking (deg/hr, right/left, from->to degrees). "
                "Use for 'what has the wind been doing' — never estimate a trend yourself."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plangap",
            "description": (
                "PLAN GAP: the boat's own observed wind vs what the frozen gameplan's forecast "
                "promised for here/now (promised vs actual TWD/TWS, signed gaps, status). Use "
                "for 'did the plan's breeze show up' — distinct from get_drift, which compares "
                "forecast to forecast."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route",
            "description": (
                "The engine's isochrone optimal route to the next mark (target='next') or the "
                "finish (target='finish') through the forecast wind on the boat's polars: ETA, "
                "number of tacks, recommended first tack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["next", "finish"]},
                },
            },
        },
    },
]

# Map tool name -> (EngineClient method, args the MODEL may set). Anything outside this map is
# rejected — the LLM cannot invent a tool. A tool result counts as a fact that grounds the brief's
# numbers, so the model may only choose WHICH real read it wants (forecast horizon, next mark vs
# finish), never feed in a hypothetical: no tws/twa/hoisted for the sail read (a "what if 25 kts"
# result would ground "wind is 25 kts"), and no route id (#185). The route and hoisted sail are
# the caller's, passed to dispatch() as `pinned`.
_DISPATCH = {
    "get_conditions": ("conditions", []),
    "get_conditions_full": ("conditions_full", []),
    "get_sources": ("sources", []),
    "get_navigator": ("navigator", []),
    "get_tactics": ("tactics", []),
    "get_sail_advice": ("sail", []),
    "get_fatigue": ("fatigue", []),
    "get_watch": ("watch", []),
    "get_checklist": ("checklist", []),
    # lat/lon deliberately NOT accepted from the model — the 7B hallucinated coordinates
    # (wrong-location forecast fails silently); the engine always uses the live position.
    "get_forecast": ("forecast", ["hours"]),
    "get_route": ("route", ["target"]),
    "get_trend": ("trend", []),
    "get_plangap": ("plangap", []),
}

# Caller-owned args, filled from `pinned` for the methods that take them (never from the model).
_PINNED = {
    "navigator": ("route",), "tactics": ("route",), "checklist": ("route",), "route": ("route",),
    "trend": ("route",), "plangap": ("route",), "sail": ("hoisted",),
}

TOOL_NAMES = set(_DISPATCH)


def dispatch(engine: EngineClient, name: str, arguments: dict | None,
             pinned: dict | None = None) -> tuple[dict, str]:
    """Run one tool call. Returns (result, tool_name). Unknown tools are refused (defense in
    depth — the model should only ever see the registered set). `pinned` holds the caller's
    route / hoisted sail; the model's own values for those are ignored."""
    spec = _DISPATCH.get(name)
    if spec is None:
        return ({"available": False, "error": f"unknown tool '{name}' (not permitted)"}, name)
    method, allowed = spec
    args = {k: v for k, v in (arguments or {}).items() if k in allowed}
    for k in _PINNED.get(method, ()):
        if (pinned or {}).get(k) is not None:
            args[k] = pinned[k]
    try:
        result = getattr(engine, method)(**args)
    except TypeError as e:
        return ({"available": False, "error": f"bad arguments: {e}"}, name)
    return (result, name)
