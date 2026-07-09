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

# All tools take a `route` where the engine accepts one, so the LLM stays on the loaded
# homework course. None of them write anything.
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
            "parameters": {
                "type": "object",
                "properties": {"route": {"type": "string", "description": "route id; omit for the active route"}},
            },
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
                "properties": {"route": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sail_advice",
            "description": (
                "The optimal sail for a given TWS/TWA against the SR33 sail crossovers, plus "
                "whether what's hoisted is right and any imminent peel. Omit tws/twa to use the "
                "live values; pass `hoisted` (e.g. 'A2') to check the current sail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tws": {"type": "number"},
                    "twa": {"type": "number"},
                    "hoisted": {"type": "string"},
                },
            },
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
            "name": "get_forecast",
            "description": (
                "Wind forecast (Open-Meteo — common public data, legal in-race) for the live "
                "position or a given lat/lon over `hours`. Use for 'what's the breeze doing "
                "ahead'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                    "hours": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route",
            "description": (
                "The engine's isochrone optimal route to the next mark (target='next') or the "
                "finish (target='finish') through the forecast wind on the SR33 polars: ETA, "
                "number of tacks, recommended first tack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {"type": "string"},
                    "target": {"type": "string", "enum": ["next", "finish"]},
                },
            },
        },
    },
]

# Map tool name -> (EngineClient method, allowed arg names). Anything outside this map is
# rejected — the LLM cannot invent a tool.
_DISPATCH = {
    "get_conditions": ("conditions", []),
    "get_conditions_full": ("conditions_full", []),
    "get_sources": ("sources", []),
    "get_navigator": ("navigator", ["route"]),
    "get_tactics": ("tactics", ["route"]),
    "get_sail_advice": ("sail", ["tws", "twa", "hoisted"]),
    "get_fatigue": ("fatigue", []),
    "get_watch": ("watch", []),
    "get_forecast": ("forecast", ["lat", "lon", "hours"]),
    "get_route": ("route", ["route", "target"]),
}

TOOL_NAMES = set(_DISPATCH)


def dispatch(engine: EngineClient, name: str, arguments: dict | None) -> tuple[dict, str]:
    """Run one tool call. Returns (result, tool_name). Unknown tools are refused (defense in
    depth — the model should only ever see the registered set)."""
    spec = _DISPATCH.get(name)
    if spec is None:
        return ({"available": False, "error": f"unknown tool '{name}' (not permitted)"}, name)
    method, allowed = spec
    args = {k: v for k, v in (arguments or {}).items() if k in allowed}
    try:
        result = getattr(engine, method)(**args)
    except TypeError as e:
        return ({"available": False, "error": f"bad arguments: {e}"}, name)
    return (result, name)
