"""The DecisionBrief — the structured, bounded output of the copilot.

A brief is advisory decision support, never a command and never the sole authority. Its shape
makes the guardrails legible:
  - `factors` / `recommendations` each carry `grounded_in`: the engine tool(s) or
    `playbook:<variant>` they rest on. `validate()` drops anything that cites nothing — so a
    recommendation that wasn't grounded in an engine fact or the playbook cannot survive.
  - `confidence` is first-class on the brief and on each item (the fuzzy-adherence principle).
  - `disclaimer` always rides along.

This module also builds the **deterministic** brief from a gathered snapshot with NO LLM. That
is the engine-only baseline: it proves the whole decision structure works on physics alone, and
it is the fallback whenever the LLM is unreachable or its output fails validation. The LLM layer
(see `copilot.py`) re-prioritizes and explains on top of this — it can never exceed the facts.
"""
from . import tools

DISCLAIMER = (
    "Advisory decision support, not a command and not the sole authority — verify against the "
    "instruments and the crew's own judgment before acting. The engine computes the numbers; "
    "this is interpretation of them and of the pre-race playbook."
)


def _num(v, nd=1):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def new_brief() -> dict:
    return {
        "situation": "",
        "factors": [],
        "recommendations": [],
        "caveats": [],
        "confidence": "low",
        "sources_used": [],
        "disclaimer": DISCLAIMER,
        "engine_only": False,
    }


def _factor(text, detail, grounded_in, confidence="med"):
    return {"factor": text, "detail": detail, "grounded_in": list(grounded_in),
            "confidence": confidence}


def _rec(action, rationale, grounded_in, urgency="monitor", confidence="med"):
    return {"action": action, "rationale": rationale, "grounded_in": list(grounded_in),
            "urgency": urgency, "confidence": confidence}


def structural_caveats(snapshot: dict, used_sources, playbook=None) -> list[str]:
    """The caveats the ENGINE knows to be true (staleness, forecast-is-a-model, playbook status).

    The LLM is not trusted to author factual caveats — it once wrote "the forecast indicates…"
    without ever fetching a forecast. So both the deterministic and the LLM paths use these
    grounded caveats instead of free-text the model invents."""
    used = set(used_sources or [])
    out = []
    cond = snapshot.get("get_conditions") or {}
    if cond.get("available") and cond.get("stale"):
        out.append(f"Instrument data is stale ({_num(cond.get('data_age_seconds'))} s old) — "
                   "treat readings with caution.")
    if "get_forecast" in used:
        out.append("Forecast is a model — treat as a trend, not a guarantee; weigh against the "
                   "live instruments.")
    if playbook is not None and getattr(playbook, "loaded", False):
        out.append(f"Playbook '{playbook.race_id}' is loaded — match recommendations against its "
                   "pre-authored variants.")
    else:
        out.append("No playbook loaded — interpreting live engine facts only; no pre-race "
                   "strategy is aboard.")
    return out


# ---------------------------------------------------------------------------------------------
# Grounding validator — the structural guardrail.
# ---------------------------------------------------------------------------------------------
def validate(brief: dict, allowed_sources: set[str], playbook_ids: list[str] | None = None,
             play_ids: list[str] | None = None) -> dict:
    """Enforce that every factor/recommendation is grounded in a real engine source or a real
    playbook variant. Ungrounded items are removed and recorded in `caveats`. Confidence is
    capped by how much survived. Returns a cleaned copy; never raises."""
    playbook_ids = playbook_ids or []
    play_ids = play_ids or []
    valid_refs = (set(allowed_sources) | {f"playbook:{v}" for v in playbook_ids} | {"playbook"}
                  | {f"play:{p}" for p in play_ids})

    out = new_brief()
    out["disclaimer"] = DISCLAIMER
    out["situation"] = str(brief.get("situation", "")).strip()
    out["engine_only"] = bool(brief.get("engine_only", False))

    dropped = 0

    def _clean_items(items, key):
        nonlocal dropped
        kept = []
        for it in items or []:
            if not isinstance(it, dict):
                dropped += 1
                continue
            refs = [r for r in (it.get("grounded_in") or []) if r in valid_refs]
            if not refs:
                dropped += 1
                continue
            it = dict(it)
            it["grounded_in"] = refs
            kept.append(it)
        return kept

    out["factors"] = _clean_items(brief.get("factors"), "factor")
    out["recommendations"] = _clean_items(brief.get("recommendations"), "action")

    used = set()
    for it in out["factors"] + out["recommendations"]:
        used.update(r for r in it["grounded_in"] if r in allowed_sources)
    out["sources_used"] = sorted(used)

    caveats = [str(c) for c in (brief.get("caveats") or []) if str(c).strip()]
    if dropped:
        caveats.append(f"{dropped} ungrounded item(s) were dropped (not backed by an engine fact "
                       "or a playbook variant).")
    out["caveats"] = caveats

    # Confidence is bounded by what's left + the model's own claim, never above it.
    claimed = str(brief.get("confidence", "low")).lower()
    claimed = claimed if claimed in ("high", "med", "medium", "low") else "low"
    claimed = "med" if claimed == "medium" else claimed
    if not out["recommendations"] and not out["factors"]:
        out["confidence"] = "low"
    elif dropped:
        out["confidence"] = "low" if claimed == "high" else claimed
    else:
        out["confidence"] = claimed
    return out


# ---------------------------------------------------------------------------------------------
# Deterministic brief — engine-only baseline + fallback.
# ---------------------------------------------------------------------------------------------
def deterministic_brief(snapshot: dict, playbook=None) -> dict:
    """Assemble a brief from gathered engine facts using simple rules — no LLM. `snapshot` is
    keyed by tool name (get_conditions, get_navigator, ...) as produced by copilot.gather()."""
    b = new_brief()
    b["engine_only"] = True

    cond = snapshot.get("get_conditions") or {}
    nav = snapshot.get("get_navigator") or {}
    tac = snapshot.get("get_tactics") or {}
    sail = snapshot.get("get_sail_advice") or {}
    fat = snapshot.get("get_fatigue") or {}
    fc = snapshot.get("get_forecast") or {}

    # --- situation ---
    bits = []
    if cond.get("available"):
        tws, twa, stw = _num(cond.get("tws")), _num(cond.get("twa")), _num(cond.get("stw"))
        if tws is not None:
            bits.append(f"TWS {tws} kts")
        if twa is not None:
            bits.append(f"TWA {twa}°")
        if stw is not None:
            bits.append(f"STW {stw} kts")
    if nav.get("available") and nav.get("next_mark"):
        nm = nav["next_mark"]
        d = _num(nm.get("distance_nm"))
        bits.append(f"next mark {nm.get('name','?')} {d} nm" if d is not None else f"next mark {nm.get('name','?')}")
    b["situation"] = ("Now: " + ", ".join(bits) + ".") if bits else "Insufficient live data for a situation read."

    # --- factors + recommendations ---
    if cond.get("available"):
        b["factors"].append(_factor(
            "Wind & boatspeed",
            f"TWS {_num(cond.get('tws'))} kts, TWA {_num(cond.get('twa'))}°, "
            f"STW {_num(cond.get('stw'))} kts, heel {_num(cond.get('heel'))}°.",
            ["get_conditions"], "high" if not cond.get("stale") else "low"))

    if nav.get("available"):
        leg = (nav.get("leg") or {}).get("type")
        nm = nav.get("next_mark") or {}
        b["factors"].append(_factor(
            "Course position",
            f"Next mark {nm.get('name','?')} at {_num(nm.get('distance_nm'))} nm, "
            f"bearing {_num(nm.get('bearing_deg'))}°, {leg or 'leg'}. "
            f"{nav.get('layline_call','')}".strip(),
            ["get_navigator"], "high"))

    if tac.get("available"):
        side = tac.get("favored_side")
        rec = tac.get("recommendation")
        phase = tac.get("phase") or ("persistent shift" if tac.get("persistent")
                                     else "oscillating" if tac.get("oscillation_deg") else None)
        detail = "; ".join(str(x) for x in [
            phase,
            (f"favored side: {side}" + (f" ({tac['favored_reason']})" if tac.get("favored_reason") else "")) if side else None,
            f"leverage {tac['leverage']}" if tac.get("leverage") is not None else None,
        ] if x)
        b["factors"].append(_factor("Tactical read", detail or "tactics available",
                                    ["get_tactics"], "med"))
        if rec:
            b["recommendations"].append(_rec(
                str(rec), "From the engine's wind-shift analysis on this leg.",
                ["get_tactics"], "soon", "med"))

    if sail.get("available"):
        optimal = sail.get("optimal_sail")
        hoisted = sail.get("hoisted_sail")
        if optimal:
            f_detail = f"Optimal sail: {optimal}" + (f"; hoisted: {hoisted}" if hoisted
                                                     else "; crew hasn't reported what's hoisted")
            b["factors"].append(_factor("Sail selection", f_detail, ["get_sail_advice"], "med"))
        # Flag a mismatch only when the engine itself says so (it knows the crossover bands).
        if sail.get("wrong_sail") and optimal:
            b["recommendations"].append(_rec(
                f"Consider changing to {optimal}",
                f"Engine sail-crossover says {optimal} is optimal for the current TWS/TWA"
                + (f"; {hoisted} is up." if hoisted else "."),
                ["get_sail_advice"], "soon", "med"))
        nx = sail.get("next_crossover")
        if isinstance(nx, dict) and nx.get("sail"):
            b["recommendations"].append(_rec(
                f"Next crossover: {nx.get('sail')}",
                f"Engine flags {nx.get('sail')} as the next sail change"
                + (f" at TWA {_num(nx.get('twa'))}°" if nx.get("twa") is not None else "") + ".",
                ["get_sail_advice"], "monitor", "low"))

    if fat.get("available"):
        idx, level = _num(fat.get("index")), fat.get("level")
        rec = fat.get("recommendation")
        b["factors"].append(_factor("Helm fatigue",
                                    f"index {idx}, level {level}" + (f" — {rec}" if rec else ""),
                                    ["get_fatigue"], "med"))
        if level in ("rotate_soon", "rotate_now"):
            b["recommendations"].append(_rec(
                "Plan a helm rotation" if level == "rotate_soon" else "Rotate the helm now",
                f"Fatigue index {idx} ({level}) — the current driver is degrading vs their own baseline.",
                ["get_fatigue"], "now" if level == "rotate_now" else "soon",
                "high" if level == "rotate_now" else "med"))

    if fc.get("available"):
        b["factors"].append(_factor("Forecast ahead",
                                    "Common public wind forecast loaded for the course.",
                                    ["get_forecast"], "med"))

    # sources_used + grounded structural caveats
    used = sorted({r for it in b["factors"] + b["recommendations"] for r in it["grounded_in"]
                   if r in tools.TOOL_NAMES})
    b["sources_used"] = used
    b["caveats"] = structural_caveats(snapshot, used, playbook)
    b["confidence"] = "high" if (cond.get("available") and not cond.get("stale") and nav.get("available")) \
        else ("med" if used else "low")
    return b
