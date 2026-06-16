"""The agent reasoning loop.

With an ANTHROPIC_API_KEY set, runs a real Claude tool-use loop: the model calls the
SQL-backed tools in tools.py, then composes a grounded reply. Without a key (Phase 0
default), falls back to a deterministic, tool-grounded responder so the whole pipeline —
ingestion → DB → tools → web app — is exercisable with no LLM and no boat.
"""
import json
import os

from shared.tool_contracts import AGENT_TOOLS
from . import tools

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# The SR33's ORC Speed Guide (Best Performance polar) — the boat-speed "gospel". Loaded
# as standing context so the agent can judge performance against target boatspeed (BTV),
# optimum beat/run angles, target AWA, heel, and when to reef/flatten.
_GUIDE_PATH = os.path.join(os.path.dirname(__file__), "..", "knowledge", "sr33_speed_guide.md")
try:
    SPEED_GUIDE = open(_GUIDE_PATH).read()
except OSError:
    SPEED_GUIDE = ""

SYSTEM_PROMPT = f"""You are the SR33 AI Navigator — navigator, coach, and data historian
for the racing yacht {BOAT_ID}. You answer the crew over a shared chat thread during
distance races and practice.

Ground every factual claim in the tools; never invent telemetry. Always note data
freshness — if get_current_conditions reports stale=true, say so and caveat the answer.
Be terse and VHF-brief: crew read you on a phone, often at night, often wet. Lead with
the number, then one line of context. Units: knots, degrees true unless told otherwise.

COACHING — judge speed against the SR33 ORC Speed Guide provided below (the boat-speed
gospel). It is the source of truth for target boatspeed (BTV) at a given TWS/TWA, the
optimum beat/run angles (best VMG), target AWA for trim reference, expected heel, and the
Reef/Flat depowering points. When asked about performance, compare live STW to the BTV at
the current TWS/TWA, and live heel/AWA to the targets. The polar tool returns the same data
numerically; the guide gives you the full curve and the surrounding context. If true wind
(TWS/TWA) is unavailable, say so — without it you cannot place the boat on the polar.

SAIL SELECTION — each polar row lists the optimal Sail, and each TWS has a Sail plan
showing which sail to fly across the wind range (J1 jib, A2/A3 asymmetrics, S2 symmetric
kite). Use it to recommend the right sail and to call CROSSOVERS / PEELS: when the optimal
sail changes as the boat bears away/heads up or the breeze builds/drops, say so explicitly
(e.g. "past 95° TWA the A3 is faster than the jib — time to hoist"). Note that sail changes
take time and crew, so flag a change when the boat is clearly in or approaching the new
sail's range, not for a momentary wiggle."""

# System sent to the API: instructions + the speed guide as a cached block (it's large and
# unchanging, so caching it keeps per-message cost low).
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT}]
if SPEED_GUIDE:
    SYSTEM_BLOCKS.append({
        "type": "text",
        "text": "## SR33 ORC SPEED GUIDE (reference)\n\n" + SPEED_GUIDE,
        "cache_control": {"type": "ephemeral"},
    })


def _fallback(message: str) -> str:
    """No-LLM responder: keyword-route to a tool and format the result."""
    m = message.lower()
    if any(w in m for w in ("ais", "traffic", "boat near", "collision")):
        r = tools.get_ais_targets()
        if not r["count"]:
            return "No AIS traffic inside guard range right now."
        t = r["targets"][0]
        return (f"{r['count']} AIS target(s). Nearest: {t.get('name') or t['mmsi']} "
                f"range {t.get('range_nm')} nm, CPA {t.get('cpa_nm')} nm in "
                f"{t.get('tcpa_min')} min.")
    if any(w in m for w in ("mark", "finish", "eta", "distance", "route")):
        r = tools.get_route_status()
        if not r.get("available"):
            return r.get("note", "Route status unavailable.")
        nm = r["next_mark"]
        return (f"Next mark {nm['name']}: {nm['distance_nm']} nm, brg {nm['bearing_deg']}°"
                + (f", ETA {nm['eta_hours']} h." if nm["eta_hours"] else "."))
    if "polar" in m or "target" in m:
        c = tools.get_current_conditions()
        if not c.get("available"):
            return "No telemetry yet — can't compare to polar."
        p = tools.get_polar_target(c["tws"] or 0, c["twa"] or 0)
        if not p.get("available"):
            return p.get("note", "No polar data loaded.")
        pct = round(100 * (c["stw"] or 0) / p["target_stw"], 1) if p["target_stw"] else None
        return (f"STW {c['stw']} kn vs target {p['target_stw']} kn"
                + (f" — {pct}% of polar." if pct else "."))
    # default: current conditions snapshot
    c = tools.get_current_conditions()
    if not c.get("available"):
        return "No telemetry recorded yet."
    stale = " (STALE)" if c.get("stale") else ""
    return (f"TWS {c['tws']} kn, TWA {c['twa']}°, TWD {c['twd']}°T, STW {c['stw']} kn, "
            f"SOG {c['sog']} kn, HDG {c['heading']}°T. Data age {c['data_age_seconds']}s{stale}.")


def _claude_loop(message: str, history: list) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=API_KEY)
    messages = list(history) + [{"role": "user", "content": message}]
    for _ in range(8):  # bounded tool-use turns
        resp = client.messages.create(
            model=MODEL, max_tokens=1024, system=SYSTEM_BLOCKS,
            tools=AGENT_TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = tools.dispatch(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, default=str)})
        messages.append({"role": "user", "content": results})
    return "Stopped after too many tool calls — try a narrower question."


def answer(message: str, history: list | None = None) -> str:
    history = history or []
    if API_KEY:
        try:
            return _claude_loop(message, history)
        except Exception as exc:  # fall back rather than drop the crew's question
            return f"[LLM error, using direct readout] {_fallback(message)}  ({exc})"
    return _fallback(message)
