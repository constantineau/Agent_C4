"""Tool contracts for the Agent_C4 agent.

The agent answers crew questions by calling SQL-backed tools (never raw NMEA). This
module is the canonical list of tool names + JSON-schema parameter definitions, shared
between the agent's tool dispatcher (vps/agent) and any client that needs to know the
surface. The Claude tool-use loop (Phase 4) feeds AGENT_TOOLS straight to the API.
"""

AGENT_TOOLS = [
    {
        "name": "get_current_conditions",
        "description": (
            "Every live quantity (wind AWS/AWA/TWS/TWA/TWD, STW, SOG/COG, heading, heel, "
            "pitch, rate-of-turn, rudder, depth, water temp, position) from EVERY reporting "
            "source, with per-source freshness and a disagreement flag. Sources are redundant "
            "by design — cross-check them; don't trust a single value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_age_minutes": {"type": "integer", "default": 5,
                                    "description": "only include readings newer than this"},
            },
            "required": [],
        },
    },
    {
        "name": "get_sources",
        "description": (
            "List the sensor sources currently reporting — which device, how fresh, how many "
            "paths, and curated RELIABILITY notes (e.g. 'needs-calibration', 'unreliable'). "
            "Use to decide which source to trust when readings disagree."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"max_age_minutes": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    {
        "name": "get_fatigue",
        "description": (
            "Helm fatigue index (0–100) for the CURRENT driver (anonymous), with a rotation "
            "recommendation. Blends steering instability (heading/heel/AWA variance), steering "
            "reversal rate, and boatspeed deficit vs polar, each scored against the boat's own "
            "recent baseline so it auto-normalises for conditions. Use when asked how the driver "
            "is doing, whether to rotate the helm, or when proactively advising a crew change. "
            "Returns components, level (fresh/watch/rotate_soon/rotate_now) and a recommendation; "
            "may report available=false with status 'warming_up' early in a sail."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sail_advice",
        "description": (
            "Sail-range advice from the SR33 Speed Guide: the optimal sail for the current "
            "TWS/TWA, where the boat sits within that sail's TWA band, and the next crossover/peel. "
            "Pass the crew's currently hoisted sail (J1/A2/A3/S2) to flag flying the wrong one. "
            "tws/twa default to the latest live values. Use when asked what sail to fly, whether "
            "to peel, or how close a crossover is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tws": {"type": "number", "description": "true wind speed (kn); omit to use live"},
                "twa": {"type": "number", "description": "true wind angle (deg); omit to use live"},
                "hoisted": {"type": "string", "description": "currently hoisted sail: J1/A2/A3/S2"},
            },
            "required": [],
        },
    },
    {
        "name": "get_navigator",
        "description": (
            "Where the boat is on the course: the next mark (bearing, distance, ETA), the leg "
            "type (beat/reach/run), the windward/leeward laylines, and a layline call (on the "
            "layline / how far below it). Computed from live position + wind. Use when asked "
            "what's next, distance/ETA to a mark, whether we can lay it, or which tack."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"route": {"type": "string", "default": "default",
                                     "description": "course/route name (e.g. 'practice')"}},
            "required": [],
        },
    },
    {
        "name": "get_tactics",
        "description": (
            "Tactical read of the beat: whether the current tack is LIFTED or HEADED, whether "
            "the breeze is oscillating or in a persistent trend (and which SIDE is favored), and "
            "how much LEVERAGE the boat has banked (cross-track from the rhumb line). Use for "
            "'which way / which tack / are we lifted / favored side' questions. Note RRS 41: this "
            "is practice/debrief advice unless the RC has cleared shore help — caveat in a race."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"route": {"type": "string", "description": "course/route name"}},
            "required": [],
        },
    },
    {
        "name": "get_history",
        "description": "Trend/stats for one channel (or raw Signal K path) over a window, "
                       "optionally restricted to a single source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "e.g. tws, stw, heel, heading_mag"},
                "window_minutes": {"type": "integer", "description": "look-back in minutes"},
                "aggregation": {
                    "type": "string",
                    "enum": ["avg", "min", "max", "series"],
                    "default": "avg",
                },
                "source": {"type": "string", "description": "optional: restrict to one source"},
            },
            "required": ["channel", "window_minutes"],
        },
    },
    {
        "name": "get_polar_target",
        "description": "Target boatspeed/VMG from the boat polar for a given TWS/TWA.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tws": {"type": "number"},
                "twa": {"type": "number"},
            },
            "required": ["tws", "twa"],
        },
    },
    {
        "name": "get_ais_targets",
        "description": "Current AIS traffic with range, bearing, CPA and TCPA.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_range_nm": {"type": "number", "default": 12},
            },
            "required": [],
        },
    },
    {
        "name": "get_route_status",
        "description": "Distance/bearing/ETA to the next mark and to the finish.",
        "input_schema": {
            "type": "object",
            "properties": {"route": {"type": "string", "default": "default"}},
            "required": [],
        },
    },
    {
        "name": "fetch_forecast",
        "description": ("Wind forecast (Open-Meteo GFS, 10 m) for a position — next N hours of "
                        "TWS/TWD. Defaults to the live position if lat/lon omitted."),
        "input_schema": {
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                           "hours": {"type": "integer", "default": 12}},
            "required": [],
        },
    },
    {
        "name": "get_route",
        "description": (
            "Isochrone optimal weather route from the boat to the next mark (target='next') or "
            "the course finish (target='finish'), computed on the polars through the forecast "
            "wind (falls back to current measured wind). Returns ETA, distance sailed vs direct, "
            "number of tacks/gybes, the recommended first heading/tack, and the route path. Use "
            "for 'best way to the mark / which tack first / routing / ETA'. Practice/debrief — "
            "RRS 41 in a race."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"route": {"type": "string"},
                           "target": {"type": "string", "enum": ["next", "finish"], "default": "next"}},
            "required": [],
        },
    },
    {
        "name": "log_note",
        "description": "Write a crew observation to the timeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "author": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in AGENT_TOOLS]

# Telemetry batch the Pi uplink POSTs to the ingestion API.
#   { "boat_id": "sr33", "points": [ { "time": "<iso8601>", "tws": 12.3, ... }, ... ] }
# Each point carries a "time" plus any subset of shared.units.TELEMETRY_CHANNELS.
