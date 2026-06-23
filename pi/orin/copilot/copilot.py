"""The copilot orchestration — bounded tool-calling + guardrails + deterministic fallback.

Flow of a brief:
  1. `gather()` deterministically pulls the core engine facts (the engine does the math).
  2. If the LLM is enabled and reachable, `llm_brief()` runs a BOUNDED tool-calling loop: the
     model is seeded with those facts, may call the read-only engine tools for more, and must
     conclude with a DecisionBrief JSON. Its output is then **validated** — every factor/rec
     must be grounded in a fact it actually fetched (or a playbook variant), or it's dropped.
  3. If the LLM is off, unreachable, errors, or its output fails validation, we return the
     **deterministic** brief built from the same facts. So a brief is always produced, always
     grounded, and never depends on the model behaving.

The model interprets and prioritizes; it can never exceed or fabricate beyond the engine facts.
"""
import json
import re

from . import adherence as adherence_mod
from . import brief as brief_mod
from . import config, narrate as narrate_mod, playbook as playbook_mod, tools
from .engine_client import EngineClient
from .llm import LLMClient, LLMUnavailable

# The facts pulled up front for every brief. (Forecast/route are left for on-demand tool calls
# — they hit the network / are CPU-heavy, so we don't pay for them unless the model asks.)
_DEFAULT_GATHER = ["get_conditions", "get_navigator", "get_tactics", "get_sail_advice", "get_fatigue"]


def gather(engine: EngineClient, route=None, hoisted=None) -> dict:
    """Pull the core engine facts, keyed by tool name (so grounding maps cleanly)."""
    snap = {"_route": route, "_hoisted": hoisted}
    snap["get_conditions"] = engine.conditions()
    snap["get_navigator"] = engine.navigator(route)
    snap["get_tactics"] = engine.tactics(route)
    snap["get_sail_advice"] = engine.sail(hoisted=hoisted)
    snap["get_fatigue"] = engine.fatigue()
    return snap


def _facts_digest(snapshot: dict) -> str:
    """A compact, grounding-tagged fact list for the LLM seed message. Each line is prefixed
    with the tool name to cite in `grounded_in`."""
    lines = []
    for name in _DEFAULT_GATHER:
        d = snapshot.get(name)
        if not isinstance(d, dict) or not d.get("available", True) or d.get("available") is False:
            continue
        # Trim each fact blob so the 7B's context isn't blown out.
        compact = {k: v for k, v in d.items() if k not in ("available",) and v is not None}
        s = json.dumps(compact, separators=(",", ":"))
        if len(s) > 600:
            s = s[:600] + "…"
        lines.append(f"[{name}] {s}")
    return "\n".join(lines) if lines else "(no live facts available)"


def _system_prompt(pb: playbook_mod.Playbook) -> str:
    return (
        "You are the SR33's ONBOARD sailing copilot — bounded decision support during a race.\n"
        "HARD RULES (these are not optional):\n"
        "1. You NEVER do arithmetic or estimate numbers yourself. If you need a number, call a "
        "tool to get it from the engine. The engine is the only source of numbers.\n"
        "2. You NEVER invent tactics or a strategy that isn't grounded in (a) an engine fact you "
        "fetched, (b) a pre-authored playbook variant, or (c) common public data the engine "
        "returned. You SELECT and INTERPRET; you do not originate strategy.\n"
        "3. You are advisory, never the sole authority. Frame recommendations as options with "
        "confidence, not commands.\n"
        "4. Only the provided tools exist. Do not claim to have done anything else.\n\n"
        + pb.digest() + "\n\n"
        "When you have enough facts, STOP calling tools and reply with ONLY a JSON object, no "
        "prose, with exactly this shape:\n"
        '{"situation": "<one or two factual sentences>",\n'
        ' "factors": [{"factor":"<short>","detail":"<from the facts>",'
        '"grounded_in":["<tool name(s) you used>"],"confidence":"high|med|low"}],\n'
        ' "recommendations": [{"action":"<what to consider>","rationale":"<why, from facts>",'
        '"grounded_in":["<tool name(s)>"],"urgency":"now|soon|monitor","confidence":"high|med|low"}],\n'
        ' "caveats": ["<uncertainty/staleness/forecast notes>"],\n'
        ' "confidence": "high|med|low"}\n'
        "Every factor and recommendation MUST cite real tool names in grounded_in (e.g. "
        "get_conditions, get_navigator, get_tactics, get_sail_advice, get_fatigue, get_forecast, "
        "get_route). Items citing nothing will be discarded."
    )


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    # Strip code fences if the model wrapped the JSON.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Grab the largest balanced {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _norm_tool_call(tc: dict) -> tuple[str, dict, str]:
    fn = tc.get("function", {}) or {}
    name = fn.get("name", "")
    raw_args = fn.get("arguments", {})
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return name, args, tc.get("id", "")


def llm_brief(engine: EngineClient, llm: LLMClient, snapshot: dict, question: str,
              pb: playbook_mod.Playbook):
    """Run the bounded tool loop and return a VALIDATED brief, or raise to trigger fallback."""
    used_sources = set(s for s in _DEFAULT_GATHER
                       if isinstance(snapshot.get(s), dict) and snapshot[s].get("available", True) is not False)
    messages = [
        {"role": "system", "content": _system_prompt(pb)},
        {"role": "user", "content":
            (question or "Give me a current decision-support read for the crew.") + "\n\n"
            "FACTS ALREADY GATHERED (cite these tool names in grounded_in; call more tools only "
            "if you need them):\n" + _facts_digest(snapshot) + "\n\n"
            "Cite ONLY these tool names in grounded_in: " + ", ".join(sorted(used_sources)) +
            " — plus any other tool you actually call. Do not cite a tool whose facts you do not "
            "have; build the brief from the facts above."},
    ]

    final_text = None
    for _round in range(config.MAX_TOOL_ROUNDS):
        msg = llm.chat(messages, tools=tools.TOOL_SPECS, tool_choice="auto")
        messages.append(msg)
        tcs = msg.get("tool_calls") or []
        if not tcs:
            final_text = msg.get("content") or ""
            break
        for tc in tcs:
            name, args, call_id = _norm_tool_call(tc)
            result, tool_name = tools.dispatch(engine, name, args)
            if isinstance(result, dict) and result.get("available", True) is not False:
                used_sources.add(tool_name)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result)[:2000]})
    else:
        # Hit the round cap still wanting tools — force a JSON conclusion with no tools.
        msg = llm.chat(messages + [{"role": "user",
                       "content": "Stop gathering. Reply now with ONLY the JSON brief."}],
                       json_object=True)
        final_text = msg.get("content") or ""

    parsed = _extract_json(final_text or "")
    if not parsed:
        raise LLMUnavailable("LLM did not return parseable JSON")

    cleaned = brief_mod.validate(parsed, used_sources, pb.variant_ids())
    cleaned["engine_only"] = False
    # The LLM is not trusted to author factual caveats — replace its free text with the
    # engine's grounded caveats, keeping only the validator's own "dropped item" note.
    dropped = [c for c in cleaned["caveats"] if "ungrounded item" in c]
    cleaned["caveats"] = brief_mod.structural_caveats(snapshot, used_sources, pb) + dropped
    # If validation gutted everything, the LLM gave us nothing usable → fall back.
    if not cleaned["factors"] and not cleaned["recommendations"]:
        raise LLMUnavailable("LLM brief had no grounded content after validation")
    return cleaned


def make_brief(question: str = "", route=None, hoisted=None, use_llm: bool | None = None) -> dict:
    """Top-level entry: produce a DecisionBrief for the current situation (+ optional crew
    question). Always returns a brief; falls back to the deterministic one on any LLM trouble."""
    route = route or config.DEFAULT_ROUTE
    engine = EngineClient()
    pb = playbook_mod.load()
    snapshot = gather(engine, route, hoisted)

    want_llm = config.USE_LLM if use_llm is None else use_llm
    meta = {"route": route, "engine": engine.base_url, "llm_used": False,
            "playbook_loaded": pb.loaded, "model": config.LLM_MODEL if want_llm else None}

    if want_llm:
        try:
            llm = LLMClient()
            out = llm_brief(engine, llm, snapshot, question, pb)
            meta["llm_used"] = True
            out["_meta"] = meta
            return out
        except LLMUnavailable as e:
            meta["llm_error"] = str(e)

    out = brief_mod.deterministic_brief(snapshot, pb)
    # Re-run grounding validation so the deterministic path obeys the same guardrail.
    used = set(s for s in snapshot if s in tools.TOOL_NAMES
               and isinstance(snapshot[s], dict) and snapshot[s].get("available", True) is not False)
    validated = brief_mod.validate(out, used, pb.variant_ids())
    validated["engine_only"] = True
    validated["_meta"] = meta
    return validated


def make_narration(route=None, hoisted=None, use_llm: bool | None = None) -> dict:
    """Proactive crew callouts for the current situation + a spoken line for what's newly worth
    saying. This is the PUSH counterpart to make_brief's PULL: a deterministic engine watches the
    facts + the frozen playbook and surfaces the few things worth SAYING right now (a rounding
    coming up, a playbook branch firing, a sail change-down, a helm rotation, stale instruments).

    Stateful per route (`narrate.step` holds raise-slow/clear-fast dedup): repeated polls only
    'voice' callouts that just crossed their confirmation threshold (speak-once), exactly like the
    cloud alerting loop — so the iPad can poll this every ~15 s. `active` is the full confirmed set
    for the banner; `spoken` is the LLM-phrased top of the NEW callouts, with the deterministic
    callout text as the always-available fallback."""
    route = route or config.DEFAULT_ROUTE
    engine = EngineClient()
    pb = playbook_mod.load()
    snapshot = gather(engine, route, hoisted)

    # `engine` is threaded in for the targeted exit-leg sail lookup the rounding callout makes.
    stepped = narrate_mod.step(route, snapshot, pb, engine)

    want_llm = config.USE_LLM if use_llm is None else use_llm
    spoken, mode = narrate_mod.narrate(stepped["new"], LLMClient() if want_llm else None)

    return {
        "active": stepped["active"],
        "new": stepped["new"],
        "spoken": spoken,
        "narration_mode": mode,
        "_meta": {"route": route, "engine": engine.base_url, "playbook_loaded": pb.loaded,
                  "model": config.LLM_MODEL if want_llm else None, "llm_used": mode == "llm"},
    }


def make_adherence(route=None) -> dict:
    """Deterministic playbook-adherence read for the PLAYBOOK-ADHERENCE dashboard tile: are we on
    the frozen gameplan, and has a branch trigger fired? No LLM — the engine does the math (persistent
    vs oscillating, favored side), `adherence.evaluate` maps it onto the pre-authored variants. Always
    returns a tile payload (na when no playbook is aboard). Pulls only the engine's tactical read (the
    one fact the tile needs), so it's cheap enough for the dashboard to poll on a short cadence."""
    route = route or config.DEFAULT_ROUTE
    engine = EngineClient()
    pb = playbook_mod.load()
    snapshot = {"get_tactics": engine.tactics(route)}
    out = adherence_mod.evaluate(pb, snapshot)
    out["_meta"] = {"route": route, "engine": engine.base_url, "playbook_loaded": pb.loaded}
    return out


def reset_narration(route=None) -> dict:
    """Clear the per-route speak-once dedup state (a race / course change → re-voice from scratch)."""
    narrate_mod.reset(route)
    return {"reset": route or "all"}
