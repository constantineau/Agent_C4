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
            "Latest wind (AWS/AWA/TWS/TWA/TWD), STW, SOG/COG, heading, position, and "
            "data freshness. Use for 'what's it doing right now' questions."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_history",
        "description": "Trend/stats for one channel over a time window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "e.g. tws, twd, stw, sog"},
                "window_minutes": {"type": "integer", "description": "look-back in minutes"},
                "aggregation": {
                    "type": "string",
                    "enum": ["avg", "min", "max", "last", "series"],
                    "default": "avg",
                },
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
        "description": "Publicly available GFS/NOAA forecast guidance for a position.",
        "input_schema": {
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
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
