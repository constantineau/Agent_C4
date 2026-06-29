"""Proactive crew callouts — the copilot speaks up.

The brief / dashboard / detail surfaces are all PULL: the crew asks, or the dashboard polls for
commentary. This module is PUSH — a deterministic callout engine watches the gathered engine
facts + the frozen playbook and surfaces the few things worth SAYING right now: a CLOSING-TRAFFIC
collision warning (safety — top priority, always legal in-race), a mark rounding coming up (timed
~15 / 10 / 5-min prep, escalating), a playbook branch trigger firing, an upcoming sail change-down,
a helm rotation, stale instruments.

Every callout is GROUNDED in an engine fact and/or a playbook variant exactly like a brief item
— the engine does the math, the callout reports it. The LLM only PHRASES the top callouts into a
calm spoken line, and the deterministic callout text is the always-on fallback. Nothing here
originates strategy: it SELECTS/INTERPRETS the pre-authored homework + the engine's own numbers,
which is the in-race-legal posture (RRS 41 — see the copilot README).

State: a tiny in-process dedup store (per route) gives "raise slow, clear fast" + speak-once. A
callout that just (and persistently) appeared is `new` — worth voicing; once voiced it stays in
`active` but isn't re-voiced until it clears and returns. Single-boat, single-process service, so
holding module state is fine (the same shape as the cloud alerting loop).
"""
import os

from . import brief as brief_mod

_num = brief_mod._num

# Collision-watch guard (mirrors the dashboard AIS tile): a CLOSING contact inside ACT → voice now
# ("collision risk"), inside the looser WATCH → voice soon ("traffic closing"). env-tunable.
AIS_ACT_CPA_NM = float(os.environ.get("COPILOT_AIS_ACT_CPA_NM", "0.5"))
AIS_ACT_TCPA_MIN = float(os.environ.get("COPILOT_AIS_ACT_TCPA_MIN", "12"))
AIS_WATCH_CPA_NM = float(os.environ.get("COPILOT_AIS_WATCH_CPA_NM", "1.5"))
AIS_WATCH_TCPA_MIN = float(os.environ.get("COPILOT_AIS_WATCH_TCPA_MIN", "30"))

# ETA thresholds (minutes-to-mark) for the staged rounding prep. Tightest matching stage wins, so
# as the mark approaches the callout id changes (…:15 → …:10 → …:5) and each stage voices once.
ROUNDING_STAGES = [(15, "heads-up"), (10, "stage"), (5, "final")]

URGENCY_RANK = {"now": 0, "soon": 1, "monitor": 2}
# Lower = more important; the spoken line leads with the top of this order.
CATEGORY_PRIORITY = {"safety": 0, "fatigue": 1, "rounding": 2, "sail": 3,
                     "playbook": 4, "shift": 5, "layline": 6, "data": 7}
# How many consecutive evaluations a callout must persist before it's "confirmed" and voiced —
# the fuzzy-adherence hysteresis. Time-critical things fire at once; noisier reads wait one poll
# so a single-sample blip never barks. (The engine already debounces tactical persistence.)
CONFIRM_ROUNDS = {"safety": 1, "fatigue": 1, "rounding": 1, "sail": 1,
                  "playbook": 2, "shift": 2, "layline": 1, "data": 2}


def _callout(cid, category, urgency, headline, detail, grounded_in, confidence="med"):
    return {"id": cid, "category": category, "urgency": urgency, "headline": headline,
            "detail": detail, "grounded_in": list(grounded_in), "confidence": confidence}


def _safety_callout(ais):
    """Collision watch — the ONE thing the copilot interrupts for. The nearest CLOSING contact inside
    the guard becomes a top-priority safety callout, grounded in the boat's own AIS receiver + own
    CPA/TCPA math (always legal in-race, never RRS-41 'outside help'). The level (act/watch) is in the
    id so an escalation watch→act re-voices, exactly like the staged rounding prep."""
    if not ais.get("own_fix"):
        return None                       # no own fix → CPA/TCPA meaningless; don't bark
    closing = [t for t in (ais.get("targets") or []) if t.get("closing")]
    if not closing:
        return None
    t = closing[0]                        # already threat-sorted: closing, smallest CPA first
    cpa, tcpa = _num(t.get("cpa_nm")), _num(t.get("tcpa_min"))
    if cpa is None or tcpa is None or tcpa < 0:
        return None
    act = cpa <= AIS_ACT_CPA_NM and tcpa <= AIS_ACT_TCPA_MIN
    watch = cpa <= AIS_WATCH_CPA_NM and tcpa <= AIS_WATCH_TCPA_MIN
    if not (act or watch):
        return None                       # closing but still comfortably clear — nothing to say
    name = t.get("name") or f"MMSI {t.get('mmsi', '?')}"
    brg, rng = _num(t.get("bearing")), _num(t.get("range_nm"))
    detail = (f"CPA {cpa} nm in {tcpa} min"
              + (f", bearing {brg}°" if brg is not None else "")
              + (f", range {rng} nm" if rng is not None else ""))
    level = "act" if act else "watch"
    return _callout(f"ais:{t.get('mmsi') or name}:{level}", "safety", "now" if act else "soon",
                    f"Collision risk: {name}" if act else f"Traffic closing: {name}",
                    detail, ["get_ais"], "high" if act else "med")


def _rounding_callout(nav, snapshot, engine):
    """Timed next-mark prep: the staged 15/10/5-min rounding heads-up + the leg-after homework."""
    nm = nav.get("next_mark") or {}
    eta = _num(nm.get("eta_min"))
    if eta is None:
        return None
    stage = next(((m, lbl) for m, lbl in ROUNDING_STAGES if eta <= m), None)
    if stage is None:
        return None                      # mark is still far off — nothing to say yet
    mins, _label = stage
    mark = nm.get("name", "the mark")
    urgency = "now" if mins <= 5 else "soon" if mins <= 10 else "monitor"
    leg_type = (nav.get("leg") or {}).get("type")
    headline = f"{mark} in ~{round(eta)} min"

    bits, grounded = [], ["get_navigator"]
    if leg_type:
        bits.append(f"on a {leg_type}")
    call = nav.get("layline_call")
    if call and mins <= 10:
        bits.append(call.rstrip("."))

    nr = nav.get("next_rounding")
    if nr:
        exit_twa = _num(nr.get("exit_twa_deg"))
        man = nr.get("maneuver", "round")
        exit_type = nr.get("exit_leg_type", "")
        bits.append(f"after rounding: {man} to ~{exit_twa}° TWA ({exit_type} leg to "
                    f"{nr.get('exit_mark', 'next mark')})")
        # The sail for the leg AFTER the mark — the engine decides, we relay (only when close
        # enough that staging it matters, and only if we can ask the engine for a TWS-resolved sail).
        if mins <= 10 and engine is not None and exit_twa is not None:
            cond = snapshot.get("get_conditions") or {}
            tws = _num(cond.get("tws"))
            adv = engine.sail(tws=tws, twa=exit_twa) if tws is not None else {}
            if isinstance(adv, dict) and adv.get("available", True) is not False:
                exit_sail = adv.get("optimal_sail")
                hoisted = snapshot.get("_hoisted") or (snapshot.get("get_sail_advice") or {}).get("hoisted_sail")
                if exit_sail:
                    grounded.append("get_sail_advice")
                    if hoisted and hoisted != exit_sail:
                        bits.append(f"stage the {exit_sail} (you have the {hoisted} up)")
                    else:
                        bits.append(f"{exit_sail} on the next leg")

    return _callout(f"rounding:{mark}:{mins}", "rounding", urgency, headline,
                    "; ".join(bits) if bits else "prepare to round",
                    grounded, "high" if mins <= 10 else "med")


def _layline_callout(nav):
    call = nav.get("layline_call") or ""
    if not call.startswith("On the"):
        return None
    nm = (nav.get("next_mark") or {}).get("name", "the mark")
    return _callout(f"layline:{nm}", "layline", "soon", "On the layline", call.rstrip("."),
                    ["get_navigator"], "high")


def _sail_callouts(sail):
    out = []
    optimal = sail.get("optimal_sail")
    if sail.get("wrong_sail") and optimal:
        hoisted = sail.get("hoisted_sail")
        out.append(_callout(f"sail_change:{optimal}", "sail", "soon",
                            f"Sail change: {optimal}",
                            f"engine crossover says {optimal} is optimal now"
                            + (f" — {hoisted} is up" if hoisted else ""),
                            ["get_sail_advice"], "med"))
    nx = sail.get("next_crossover")
    if isinstance(nx, dict) and nx.get("sail"):
        twa = _num(nx.get("twa"))
        out.append(_callout(f"sail_next:{nx['sail']}", "sail", "monitor",
                            f"Next change-down: {nx['sail']}",
                            f"approaching the {nx['sail']} crossover"
                            + (f" near TWA {twa}°" if twa is not None else ""),
                            ["get_sail_advice"], "low"))
    return out


def _variant_for_side(playbook, side):
    """Find a playbook variant whose flip-trigger / id / summary points at `side` (left/right).
    The match keeps the copilot SELECTING a pre-authored variant, never inventing one."""
    if side not in ("left", "right") or playbook is None or not getattr(playbook, "loaded", False):
        return None
    for v in playbook.variants:
        hay = " ".join(str(v.get(k, "")) for k in ("id", "name", "summary", "what_flips_it",
                                                    "favored_side")).lower()
        if side in hay:
            return v
    return None


def _tactics_callouts(tac, playbook):
    """A persistent shift is the on-the-water trigger the playbook branches on. If a variant
    matches the favored side, voice that branch (grounded in BOTH the tactic and the variant);
    otherwise voice the engine's tactical read alone (grounded only in get_tactics)."""
    wind = tac.get("wind") or {}
    if not wind.get("persistent"):
        return []
    side = tac.get("favored_side")
    osc = _num(wind.get("oscillation_deg"))
    base = f"persistent shift{f', {osc}° swing' if osc else ''}"
    side_txt = f" — favored side {side}" if side else ""
    v = _variant_for_side(playbook, side)
    if v:
        vid = str(v.get("id") or v.get("name") or "?")
        summary = v.get("summary") or v.get("rationale") or ""
        flips = v.get("what_flips_it") or ""
        detail = f"{base}{side_txt}. Playbook variant {vid}: {summary}".strip()
        if flips:
            detail += f" (flips when: {flips})"
        return [_callout(f"playbook:{vid}", "playbook", "soon",
                        f"Consider playbook variant {vid}", detail,
                        ["get_tactics", f"playbook:{vid}"], "med")]
    return [_callout(f"shift:{side or 'persistent'}", "shift", "soon",
                    "Persistent shift", f"{base}{side_txt}"
                    + (tac.get("recommendation") and f" — {tac['recommendation']}" or ""),
                    ["get_tactics"], "med")]


def _fatigue_callout(fat):
    level = fat.get("level")
    if level not in ("rotate_soon", "rotate_now"):
        return None
    idx = _num(fat.get("index"))
    now = level == "rotate_now"
    return _callout(f"fatigue:{level}", "fatigue", "now" if now else "soon",
                    "Rotate the helm now" if now else "Plan a helm rotation",
                    f"fatigue index {idx} ({level}) — the driver is degrading vs their own baseline",
                    ["get_fatigue"], "high" if now else "med")


def _data_callout(cond):
    if not cond.get("stale"):
        return None
    age = _num(cond.get("data_age_seconds"))
    return _callout("data_stale", "data", "monitor", "Instruments stale",
                    f"live data is ~{age} s old — treat readings with caution",
                    ["get_conditions"], "high")


def evaluate(snapshot, playbook=None, engine=None):
    """The deterministic callout set for the current situation (unsorted, no dedup). `snapshot` is
    a `copilot.gather()` dict keyed by tool name; `engine` (optional) is only used for the targeted
    exit-leg sail lookup. Every returned callout is grounded in a real engine source / playbook
    variant — `_validate` downstream re-checks that, same guarantee as a brief."""
    nav = snapshot.get("get_navigator") or {}
    tac = snapshot.get("get_tactics") or {}
    sail = snapshot.get("get_sail_advice") or {}
    fat = snapshot.get("get_fatigue") or {}
    cond = snapshot.get("get_conditions") or {}
    ais = snapshot.get("get_ais") or {}

    out = []
    if ais.get("available", True) is not False:    # SAFETY first — collision watch (always legal)
        c = _safety_callout(ais)
        if c:
            out.append(c)
    if nav.get("available"):
        for c in (_rounding_callout(nav, snapshot, engine), _layline_callout(nav)):
            if c:
                out.append(c)
    if sail.get("available"):
        out += _sail_callouts(sail)
    if tac.get("available"):
        out += _tactics_callouts(tac, playbook)
    if fat.get("available"):
        c = _fatigue_callout(fat)
        if c:
            out.append(c)
    if cond.get("available"):
        c = _data_callout(cond)
        if c:
            out.append(c)
    # Drop anything that lost all grounding (defensive — every builder grounds, but keep the
    # invariant explicit so a future builder can't sneak an ungrounded callout through).
    return [c for c in out if c.get("grounded_in")]


def _sort_key(c):
    return (URGENCY_RANK.get(c["urgency"], 9), CATEGORY_PRIORITY.get(c["category"], 9))


# Per-route dedup state: {route: {callout_id: consecutive_seen_count}}. Module-level — one boat,
# one process. Reset between races by restarting the service (the playbook is frozen per race).
_STATE: dict[str, dict[str, int]] = {}


def reset(route=None):
    """Clear dedup state (a race/course change). No arg → clear all."""
    if route is None:
        _STATE.clear()
    else:
        _STATE.pop(route, None)


def step(route, snapshot, playbook=None, engine=None):
    """Evaluate + apply raise-slow / clear-fast dedup. Returns {active, new}:
      - active: every confirmed callout right now, priority-sorted (what the banner shows);
      - new:    the callouts confirmed THIS step (first reached their persistence threshold) —
                what's worth voicing. Re-appearing-after-clear counts as new again.
    """
    route = route or "default"
    found = {c["id"]: c for c in evaluate(snapshot, playbook, engine)}
    prev = _STATE.get(route, {})
    counts, active, new = {}, [], []
    for cid, c in found.items():
        n = prev.get(cid, 0) + 1
        counts[cid] = n
        need = CONFIRM_ROUNDS.get(c["category"], 1)
        if n >= need:
            active.append(c)
            if n == need:               # crossed the threshold this very step → voice once
                new.append(c)
    _STATE[route] = counts              # clear-fast: ids not in `found` simply drop out
    active.sort(key=_sort_key)
    new.sort(key=_sort_key)
    return {"active": active, "new": new}


# ---------------------------------------------------------------------------------------------
# Narration — the LLM phrases the callouts; the deterministic text is the fallback.
# ---------------------------------------------------------------------------------------------
_NARR_SYSTEM = (
    "You are the onboard tactical copilot for the SR33 racing yacht 'C4', speaking to the crew. "
    "You are given one or more CALLOUTS the engine has already computed and grounded. Restate the "
    "most important one or two as a single short spoken radio call — calm and practical like a good "
    "navigator, most urgent first, at most two sentences, plain prose (no JSON, no lists). Use ONLY "
    "the facts in the callouts; invent no numbers, marks, or advice not present. If nothing is "
    "worth saying, reply with an empty line."
)


def _deterministic_spoken(callouts):
    """The grounded fallback line: the top callouts' own text, no model needed."""
    parts = []
    for c in callouts[:2]:
        parts.append(c["headline"] + (f" — {c['detail']}" if c.get("detail") else ""))
    return ". ".join(parts)


def narrate(callouts, llm=None):
    """Phrase the (already-sorted) callouts into a spoken crew line. Returns (text, mode). With no
    callouts → ("", "none"). The LLM only rephrases grounded text; any failure falls back to the
    deterministic line so a call is always available."""
    if not callouts:
        return "", "none"
    fallback = _deterministic_spoken(callouts)
    if llm is None:
        return fallback, "deterministic"
    payload = [{"headline": c["headline"], "detail": c.get("detail", ""),
                "urgency": c["urgency"]} for c in callouts[:3]]
    import json
    from .llm import LLMUnavailable
    try:
        msg = llm.chat([{"role": "system", "content": _NARR_SYSTEM},
                        {"role": "user", "content": "Callouts:\n" + json.dumps(payload, ensure_ascii=False)}])
        text = (msg.get("content") or "").strip()
    except LLMUnavailable:
        return fallback, "deterministic"
    return (text, "llm") if text else (fallback, "deterministic")
