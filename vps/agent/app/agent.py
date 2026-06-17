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
sail's range, not for a momentary wiggle. The get_sail_advice tool returns this structured
(optimal sail, the TWA zones, the next crossover, and whether the crew's hoisted sail is
wrong) — use it for precise peel calls and crossover distances.

ROUTING / WEATHER — get_route returns the isochrone optimal route to the next mark or finish
(ETA, tacks, recommended first heading/tack) computed on the polars through the wind forecast;
fetch_forecast gives the wind outlook. Use for "best way / which tack first / routing / ETA".
Same RRS 41 caveat as tactics — practice/debrief unless the RC clears shore routing.

TACTICS — get_tactics reads the beat: lifted/headed on the current tack, oscillating vs a
persistent trend, the favored side, and banked leverage. Use it for "which way / which tack /
favored side / are we lifted" questions. This is shore tactical advice — say it's for
practice/debrief and may be "outside help" under RRS 41 in a race unless the RC has cleared it.

NAVIGATOR — get_navigator gives the next mark (bearing/distance/ETA), the leg type
(beat/reach/run) and the laylines with a layline call. Use it for "what's next / can we lay
the mark / which tack / ETA" questions. Marks/laylines/ETA are navigation and fine to give
any time; deeper tactics (favored side, shifts, leverage) are practice/debrief unless the RC
has cleared shore help (RRS 41).

AIS / COLLISION GUARD — get_ais_targets lists nearby vessels heard on AIS with range, bearing,
and a freshly computed CPA (closest point of approach, nm) and TCPA (time to it, min) against
own ship — sorted most-threatening first. A target is a real concern when it is CLOSING
(positive TCPA) with a small CPA inside the next several minutes; lead with that one, give its
name/MMSI, range, bearing, CPA and TCPA, and which side it's on. Targets that are opening
(negative/no TCPA) or with a comfortable CPA are situational, not a threat — say so plainly so
the crew isn't alarmed. If own ship has no position fix the tool returns targets without
geometry (`own_fix:false`) — report that you can list contacts but can't compute CPA. Collision
avoidance is always allowed (it's safety, never "outside help" under RRS 41).

ALERTS — get_alerts returns the automated alerts the system is RAISING right now, most severe
first: closing AIS traffic, stale telemetry, shallow/shoaling water, boatspeed well under polar,
a persistent wind shift, or helm fatigue. They're conservative and debounced (a condition has to
persist before it fires), so treat an active alert as real. For "any alerts / what's wrong / are
we OK / status", lead with the highest-severity alert and its message, then mention the rest; if
there are none, say all clear. Safety alerts (AIS, depth, stale data) are always appropriate to
raise; the performance/tactical ones (polar deficit, wind shift) carry the usual RRS 41 caveat
in a race. Don't invent alerts — report only what the tool returns.

DEBRIEFS / SUMMARIES — get_summaries returns recent STORED window reports (newest first), each
with the window and a narrative covering boatspeed vs polar, the wind pattern/shifts, heel, and
any alerts that fired. Use it to recall "what did the last debrief say" or "summary of the last
session". Note that debriefs are generated ON DEMAND (the crew's Debrief button, or POST
/debrief / /summary) — there is no automatic timer, so if there's nothing stored yet, say so and
suggest running a debrief. Quote the stored numbers rather than re-deriving them.

CREW FATIGUE — get_fatigue returns a 0–100 helm fatigue index for the current (anonymous)
driver with a level (fresh/watch/rotate_soon/rotate_now) and a rotation recommendation. It
reads steering quality (heading/heel/apparent-wind variance, steering reversals) and speed
vs polar, each measured against the boat's OWN recent baseline. When the crew asks how the
driver is doing or whether to rotate, lead with the index + level and relay the
recommendation; name the biggest contributing component (e.g. "heading wander up, speed off
target"). Proactively flag rotate_soon/rotate_now if it comes up. It's advisory and
baseline-relative — if it reports warming_up, say the index isn't ready yet. A high index in
a big breeze-build can be conditions, not the driver — caveat when wind is climbing fast.

DATA / SENSOR SKEPTICISM — by design the boat carries redundant sensors, so
get_current_conditions returns MULTIPLE sources per quantity (e.g. heel from the Orca Core
and the GPS 24xd; heading from several). Never treat one number as truth:
- When sources for a channel AGREE, you can state the value with confidence.
- When they DISAGREE (the tool flags `disagreement`/`spread`), say so, give the range, and
  prefer the more reliable source — call get_sources for curated reliability (some sensors
  are uncalibrated or flaky, e.g. an uncalibrated paddlewheel speed or a Core that hasn't
  been calibrated). Don't average blindly.
- Flag STALE (large `age_s`), MISSING, or implausible readings rather than reporting them
  as fact. A sensor that's silent or wildly off is itself useful information — surface it.
- Each channel has a `preferred` reading — the priority-ranked lead source (best-calibrated
  for that quantity, e.g. the Orca Core for heel/true-wind). LEAD with `preferred`. If
  `fell_back=true`, the preferred sensor was stale/absent and you're on a BACKUP — say so
  explicitly ("Orca heel is silent, using the 24xd"). This redundancy is the point: a sensor
  failing mid-race shouldn't blind you, but the crew must know they're on a backup.
Your job is to be the crew's sanity-check on the instruments, not just a readout."""

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
    if any(w in m for w in ("debrief", "recap", "how did we do", "how'd we do",
                            "summarize", "summary", "session report")):
        from . import summarizer
        r = summarizer.make_debrief() if ("debrief" in m or "recap" in m or "session" in m) \
            else summarizer.make_summary()
        return r["summary"] if r.get("available") else "Nothing to report — no telemetry in the window yet."
    if any(w in m for w in ("alert", "alarm", "what's wrong", "whats wrong",
                            "anything wrong", "are we ok", "status")):
        r = tools.get_alerts()
        if not r["count"]:
            return "No active alerts — all clear."
        a = r["alerts"][0]
        extra = f" (+{r['count'] - 1} more)" if r["count"] > 1 else ""
        return f"{r['count']} active alert(s). Top [{a['severity']}]: {a['message']}{extra}"
    if any(w in m for w in ("ais", "traffic", "boat near", "collision")):
        r = tools.get_ais_targets()
        if not r["count"]:
            return "No AIS traffic inside guard range right now."
        if not r.get("own_fix", True):
            return (f"{r['count']} AIS contact(s), but no own-ship position fix — "
                    "can't compute range or CPA.")
        t = r["targets"][0]
        who = t.get("name") or t["mmsi"]
        if t.get("closing"):
            return (f"{r['count']} AIS target(s). Most threatening: {who} at "
                    f"{t.get('range_nm')} nm, bearing {t.get('bearing')}° — CPA "
                    f"{t.get('cpa_nm')} nm in {t.get('tcpa_min')} min (closing).")
        return (f"{r['count']} AIS target(s); nearest concern {who} at "
                f"{t.get('range_nm')} nm, bearing {t.get('bearing')}° — opening / no CPA.")
    if any(w in m for w in ("route", "routing", "best way", "weather route", "fastest", "which tack first")):
        r = tools.get_route()
        if not r.get("available"):
            return r.get("note", "Routing unavailable.")
        return (f"Route to {r['target']}: ETA ~{r['eta_min']} min, {r['sailed_nm']} nm sailed "
                f"({r['direct_nm']} direct), {r['tacks']} tack(s). Start on {r['first_tack']} "
                f"heading {r['recommended_heading']}°. ({r['wind_source']}; practice/debrief — RRS 41)")
    if "forecast" in m or "weather" in m:
        r = tools.fetch_forecast()
        if not r.get("available"):
            return r.get("note", "Forecast unavailable.")
        h = r["hours"]
        nxt = h[1] if len(h) > 1 else (h[0] if h else None)
        return ("Forecast: " + (f"now ~{h[0]['tws']} kn @ {h[0]['twd']}°, "
                f"in {nxt['in_h']} h ~{nxt['tws']} kn @ {nxt['twd']}°." if nxt else "no data."))
    if any(w in m for w in ("favored", "favoured", "which side", "which way", "lifted", "headed", "shift", "leverage", "tactic")):
        r = tools.get_tactics()
        if not r.get("available"):
            return r.get("note", "Tactics unavailable.")
        return r["recommendation"] + "  (practice/debrief — RRS 41)"
    if any(w in m for w in ("mark", "finish", "eta", "distance", "layline", "lay ", "tack", "next leg")):
        r = tools.get_navigator()
        if not r.get("available"):
            return r.get("note", "Navigator unavailable.")
        nm = r["next_mark"]
        eta = f", ETA {nm['eta_min']} min" if nm.get("eta_min") else ""
        lay = f" {r['layline_call']}" if r.get("layline_call") else ""
        return (f"Next: {nm['name']} {nm['distance_nm']} nm, brg {nm['bearing_deg']}° "
                f"({r['leg']['type']}){eta}.{lay}")
    if any(w in m for w in ("sail", "peel", "kite", "spinnaker", "jib", "hoist", "change up", "change down")):
        r = tools.get_sail_advice()
        if not r.get("available"):
            return r.get("note", "Sail advice unavailable (need live TWS/TWA).")
        return r["recommendation"]
    if any(w in m for w in ("fatigue", "tired", "rotate", "helm change", "driver", "shift change")):
        r = tools.get_fatigue()
        if not r.get("available"):
            return f"Fatigue index not ready: {r.get('note', r.get('status', 'unavailable'))}."
        worst = max(r["components"].items(), key=lambda kv: kv[1]["score"], default=(None, None))
        lead = f" (biggest factor: {worst[0]})" if worst[0] else ""
        return f"Helm fatigue {r['index']}/100 — {r['level']}. {r['recommendation']}{lead}"
    if "source" in m or "sensor" in m:
        r = tools.get_sources()
        if not r["count"]:
            return "No sensor sources reporting right now."
        return f"{r['count']} sources reporting: " + ", ".join(
            f"{s.get('device') or s['source']} ({s['reliability']}, {s['last_seen_s']}s ago)"
            for s in r["sources"])
    s = tools.get_strip()
    if not s.get("available"):
        return "No telemetry recorded yet."
    if "polar" in m or "target" in m:
        if s.get("tws") is None or s.get("twa") is None:
            return "No true wind (TWS/TWA) yet — can't place us on the polar."
        p = tools.get_polar_target(s["tws"], s["twa"])
        if not p.get("available"):
            return p.get("note", "No polar data loaded.")
        pct = round(100 * (s["stw"] or 0) / p["target_stw"], 1) if p.get("target_stw") else None
        return (f"STW {s['stw']} kn vs target {p['target_stw']} kn"
                + (f" — {pct}% of polar." if pct else "."))
    # default: best-value snapshot (use the chat/LLM path for multi-source detail)
    stale = " (STALE)" if s.get("stale") else ""
    return (f"TWS {s.get('tws')} kn, TWA {s.get('twa')}°, STW {s.get('stw')} kn, "
            f"SOG {s.get('sog')} kn, HDG {s.get('heading')}°, heel {s.get('heel')}°. "
            f"Data age {s.get('data_age_seconds')}s{stale}. (ask for sources to cross-check)")


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
