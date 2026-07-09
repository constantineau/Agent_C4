"""CREW BRIEFS — the copilot's scheduled/on-demand SYNTHESIS surfaces (the coach window's meat).

Four brief kinds, each a different "read the whole boat at once" moment the tiles can't give:

  handover  — the watch-change brief (T-15): the race the incoming watch is inheriting — wind
              now + trend, the sail up and the next window, the leg, the strategy stack, what's
              armed/close, the divergence reads, who's on/next. The single highest-leverage
              synthesis: a watch change is exactly when context is lost.
  recap     — "the last hour": wind from→to, performance vs polar, position vs plan, the top
              rival, sail changes logged, plays that armed. Situational awareness + morale.
  mark      — the mark-approach pre-brief (~T-20): the rounding in one card — distance/ETA, the
              leg after, the sail change waiting, who takes the rounding, plays for the next leg.
  watchlist — "what flips the plan": the quiet plays measurably CLOSE to arming (the matcher's
              distance-to-trigger), each with the live number against its threshold, plus any
              divergence read already in its consider band.

Discipline (same as strategy_brief): the DETERMINISTIC composer builds every section from
engine numbers first — that text is always available and always correct. The LLM, when up, only
REPHRASES the deterministic brief into tighter crew language (a schema-forced rewrite; it is
told every number is fixed); any LLM trouble → the deterministic text stands. DATA HONESTY rides
on every brief: stale instruments / missing playbook / engine fallback are stated up front, so a
degraded read can never masquerade as a confident one. Advisory, never a command. No actions.
"""
from __future__ import annotations

import json
import time

from . import config
from . import playbook as playbook_mod
from .engine_client import EngineClient
from .llm import LLMClient, LLMUnavailable

KINDS = ("handover", "recap", "mark", "watchlist")

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "body": {"type": "string"}},
    "required": ["headline", "body"],
}

_KIND_STYLE = {
    "handover": ("WATCH-HANDOVER brief the off-going helm reads aloud to the incoming watch",
                 "4-6 short sentences, most important first"),
    "recap": ("LAST-HOUR RECAP for the whole crew", "2-4 short sentences"),
    "mark": ("MARK-APPROACH pre-brief for the upcoming rounding", "3-5 short sentences"),
    "watchlist": ("WHAT-FLIPS-THE-PLAN watchlist summary", "1-2 sentences over the list"),
}


def _clock(epoch):
    try:
        return time.strftime("%H:%M", time.localtime(epoch))
    except (TypeError, ValueError, OverflowError):
        return "?"


def _sec(title, lines, grounded):
    lines = [l for l in lines if l]
    return {"title": title, "lines": lines, "grounded_in": grounded} if lines else None


# ------------------------------------------------------------------ deterministic section builders

def _data_notes(snap, engine):
    """DATA HONESTY — degraded inputs stated up front, never buried. Deterministic."""
    notes = []
    cond = snap.get("conditions") or {}
    if not cond.get("available", True) and cond.get("error"):
        notes.append("Engine unreachable for instruments — everything below may be stale.")
    elif cond.get("stale"):
        notes.append("Instrument feed is STALE — treat wind/speed reads below as suspect.")
    fb = (getattr(config, "ENGINE_URL_FALLBACK", "") or "").rstrip("/")
    if fb and engine.base_url == fb:
        notes.append("Engine reached over the FALLBACK link (not the race cable).")
    # plan-relative honesty keys off the ENGINE's read — the engine holds the frozen bundle;
    # the local playbook file only feeds sail thresholds
    pb = snap.get("_pb")
    engine_has_plan = ((snap.get("plays") or {}).get("available")
                       or (snap.get("deviation") or {}).get("available"))
    if (pb is None or not pb.loaded) and not engine_has_plan:
        notes.append("No playbook aboard — plan-relative reads (plays, deviation, promise) are off.")
    tr = snap.get("trend") or {}
    if not tr.get("available") and "thin" in (tr.get("note") or ""):
        notes.append("Wind archive still thin — trend reads not available yet.")
    return notes


def _wind_lines(snap):
    cond = snap.get("conditions") or {}
    tr = snap.get("trend") or {}
    lines = []
    if cond.get("tws") is not None:
        twa = f", TWA {round(cond['twa'])}°" if cond.get("twa") is not None else ""
        stw = (f", {round(cond['stw'], 1):g} kts through the water"
               if cond.get("stw") is not None else "")
        lines.append(f"Now: {round(cond['tws'], 1):g} kts{twa}{stw}.")
    if tr.get("available") and tr.get("read"):
        lines.append(tr["read"][0].upper() + tr["read"][1:] + ".")
    return _sec("Wind", lines, ["get_conditions", "get_trend"])


def _sail_window_lines(snap, pb):
    """The SAIL-WINDOW clock: current sail + fit, and — from the trend rate against the frozen
    boat-model thresholds — roughly when the next threshold arrives. Deterministic arithmetic
    on engine numbers (the LLM never computes this)."""
    sail = snap.get("sail") or {}
    plays = snap.get("plays") or {}
    st = plays.get("sail_state") or {}
    tr = snap.get("trend") or {}
    cond = snap.get("conditions") or {}
    lines = []
    flying = st.get("flying") or ([st["hoisted"]] if st.get("hoisted") else [])
    if flying:
        reef = f" + reef {st['reef'][1:]}" if st.get("reef") else ""
        lines.append("Flying: " + "+".join(flying) + reef + ".")
    if sail.get("available", True) and sail.get("wrong_sail") and sail.get("optimal_sail"):
        lines.append(f"Crossover says {sail['optimal_sail']} is the sail now.")
    nx = sail.get("next_xover") or sail.get("next_crossover")
    if isinstance(nx, dict) and (nx.get("to_sail") or nx.get("sail")):
        to = nx.get("to_sail") or nx.get("sail")
        at = nx.get("at_twa") or nx.get("twa")
        lines.append(f"Next change: {to}" + (f" at TWA {at:g}°" if at is not None else "") + ".")
    # the clock: TWS thresholds from the frozen boat model vs the 1 h build/fade rate
    rate = tr.get("tws_trend_kn_per_hr")
    tws = cond.get("tws")
    if rate is not None and tws is not None and abs(rate) >= 0.3:
        bm = pb.boat_model if pb.loaded else {}
        thresholds = []
        mr = bm.get("main_reefs") or {}
        if mr.get("r1_tws_kn") is not None:
            thresholds.append((f"the reef-1 threshold ({mr['r1_tws_kn']:g} kts)", mr["r1_tws_kn"]))
        if mr.get("r1_a3_slot_tws_kn") is not None and "A3" in flying:
            thresholds.append((f"the A3 slot-reef threshold ({mr['r1_a3_slot_tws_kn']:g} kts)",
                               mr["r1_a3_slot_tws_kn"]))
        c0 = bm.get("code0") or {}
        if c0.get("tws_max") is not None and "C0" in flying:
            thresholds.append((f"the C0 ceiling ({c0['tws_max']:g} kts)", c0["tws_max"]))
        for label, thr in thresholds:
            hrs = None
            if rate > 0 and thr > tws:
                hrs = (thr - tws) / rate
            elif rate < 0 and thr < tws:
                hrs = (tws - thr) / -rate
            if hrs is not None and hrs <= 8:
                eta = _clock(time.time() + hrs * 3600)
                lines.append(f"At this rate you cross {label} in ~{round(hrs, 1):g} h (~{eta}).")
    return _sec("Sails", lines, ["sail_state", "get_sail_advice", "get_trend", "get_conditions"])


def _leg_lines(snap):
    nav = snap.get("navigator") or {}
    if not nav.get("available"):
        return None
    nm = nav.get("next_mark") or {}
    lines = []
    if nm.get("name"):
        eta = nm.get("eta_min")
        eta_txt = (f", ETA ~{int(eta)} min (~{_clock(time.time() + eta * 60)})"
                   if isinstance(eta, (int, float)) else "")
        lines.append(f"Next mark: {nm['name']} — {nm.get('distance_nm', '?')} nm at "
                     f"{nm.get('bearing_deg', '?')}°{eta_txt}.")
    leg = nav.get("leg") or {}
    if leg.get("type"):
        lines.append(f"Leg: {leg['type']}.")
    return _sec("Leg", lines, ["get_navigator"])


def _strategy_lines(snap):
    stg = snap.get("strategy") or {}
    if not stg.get("available"):
        return None
    lines = []
    assessment = (stg.get("assessment") or "").strip()
    if assessment:
        lines.append(assessment.rstrip(".") + ".")
    rec = stg.get("recommendation") or {}
    action = (rec.get("action") or "").strip()
    if action and action.lower() not in assessment.lower():
        lines.append(f"The call: {action}.")
    return _sec("Strategy", lines, ["get_strategy"])


def _plays_lines(snap, include_watchlist=True):
    plays = snap.get("plays") or {}
    if not plays.get("available"):
        return None
    lines = []
    armed = [p for p in (plays.get("plays") or []) if p.get("status") == "armed"]
    arming = [p for p in (plays.get("plays") or []) if p.get("status") == "arming"]
    for p in armed[:3]:
        g = f" — {p['guidance']}" if p.get("guidance") else ""
        lines.append(f"ARMED: {p.get('name')}{g}")
    for p in arming[:2]:
        sp = f" ({p['sustain_pct']}% held)" if p.get("sustain_pct") is not None else ""
        lines.append(f"Arming: {p.get('name')}{sp}")
    if include_watchlist:
        lines += _watchlist_rows(plays)[:2]
    return _sec("Plays", lines, ["get_plays"])


def _watchlist_rows(plays):
    rows = []
    for w in (plays.get("watchlist") or []):
        gap = w.get("nearest_gap") or {}
        num = ""
        if gap.get("actual") is not None and gap.get("value") is not None:
            num = f" ({gap['signal']} {gap['actual']:g} vs {gap['op']} {gap['value']:g})"
        pct = f"{round((w.get('closeness') or 0) * 100)}%"
        rows.append(f"Close ({pct}): {w.get('name')}{num}")
    return rows


def _divergence_lines(snap):
    """The plan-vs-reality reads, one line each, only when in their consider/commit bands."""
    lines, grounded = [], []
    for key, tool in (("drift", "get_drift"), ("plangap", "get_plangap"),
                      ("deviation", "get_deviation")):
        d = snap.get(key) or {}
        if d.get("available") and d.get("status") in ("watch", "act"):
            lines.append(f"{d.get('value')}: {d.get('sub')}.")
            grounded.append(tool)
    return _sec("Plan vs reality", lines, grounded or ["get_drift"])


def _buoy_lines(snap):
    """The up-course leading indicator, corroborated against the drift read when both point the
    same way — the 'promised breeze arriving early' story, composed from engine numbers."""
    buo = (snap.get("buoys") or {})
    upc = (buo.get("upcourse") or {}) if buo.get("available") else {}
    if not upc:
        return None
    dtws, dtwd = upc.get("tws_delta_kn"), upc.get("twd_shift_deg")
    if not ((abs(dtws or 0) >= 3) or (abs(dtwd or 0) >= 15)):
        return None
    bits = []
    if abs(dtws or 0) >= 3:
        bits.append(f"{abs(dtws):g} kts {'MORE' if dtws > 0 else 'LESS'} pressure")
    if abs(dtwd or 0) >= 15:
        bits.append(f"breeze {abs(dtwd):g}° {'right' if dtwd > 0 else 'left'} of here")
    name = upc.get("name") or upc.get("station") or "up-course buoy"
    lines = [f"{name} ({upc.get('range_nm', '?')} nm ahead): " + ", ".join(bits)
             + f" ({upc.get('age_min', '?')} min old)."]
    dft = snap.get("drift") or {}
    if (dft.get("available") and dft.get("status") in ("watch", "act")
            and dft.get("drift_dir") in ("right", "left") and dtwd is not None
            and ((dtwd > 0) == (dft["drift_dir"] == "right"))):
        lines.append("That agrees with the forecast move — it may be arriving early.")
    return _sec("Up-course", lines, ["get_buoys", "get_drift"])


def _crew_lines(snap):
    """Watch + the fatigue-performance link: who's on/next, and the honest 'helm fading while
    we're under target' read when both signals say so."""
    w = snap.get("watch") or {}
    fat = snap.get("fatigue") or {}
    plays = snap.get("plays") or {}
    lines = []
    if w.get("plan_set") and w.get("active"):
        nxt = (f" — {w.get('next_on_label') or w.get('next_on')} on in "
               f"{int(w['mins_to_change'])} min" if w.get("mins_to_change") is not None
               and w.get("next_on") else "")
        lines.append(f"On watch: {w.get('on_label') or w.get('on')}{nxt}.")
    idx, level = fat.get("index"), fat.get("level")
    polar = (plays.get("signals") or {}).get("polar_pct")
    if idx is not None and level in ("watch", "rotate_soon", "rotate_now"):
        perf = (f" and the boat is at {polar:g}% of polar" if isinstance(polar, (int, float))
                and polar < 97 else "")
        lines.append(f"Helm fatigue {idx:g} ({level.replace('_', ' ')}){perf}.")
    elif isinstance(polar, (int, float)):
        lines.append(f"Boat at {polar:g}% of polar (10-min window).")
    return _sec("Crew", lines, ["get_watch", "get_fatigue", "get_plays"])


def _fleet_lines(snap):
    flt = snap.get("fleet") or {}
    rows = flt.get("fleet") or []
    if not (flt.get("available") and rows):
        return None
    top = rows[0]
    d = top.get("corrected_delta_s")
    if d is None:
        return None
    m, s = divmod(abs(int(d)), 60)
    who = "beating us" if d < 0 else "behind us"
    return _sec("Fleet", [f"{top.get('boat', 'Top rival')} projected {who} by {m}:{s:02d} "
                          "corrected."], ["get_fleet"])


def _recap_events(snap, window_s=3900):
    """Sail-log entries + newly armed plays inside the recap window (engine facts, timestamped)."""
    lines = []
    log = ((snap.get("sails_log") or {}).get("log")
           or (snap.get("sails_log") or {}).get("entries") or [])
    cutoff = time.time() - window_s
    for e in log:
        ts = e.get("ts") or e.get("time")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        flying = e.get("flying") or []
        reef = f" reef {e['reef'][1:]}" if e.get("reef") else ""
        lines.append(f"{_clock(ts)} — sails: " + ("+".join(flying) if flying else "—") + reef)
    return _sec("This hour", lines, ["sails_log"])


# ------------------------------------------------------------------------------- kind assemblies

def _gather(engine, kind, route):
    """Per-kind engine pulls — only what the brief needs (the Orin↔Pi link is fast, but the
    engine computes on every call; don't pay for facts a kind never uses)."""
    snap = {"conditions": engine.conditions()}
    if kind in ("handover", "recap"):
        snap["trend"] = engine.trend(route)
        snap["plays"] = engine.plays(route)
        snap["fatigue"] = engine.fatigue()
        snap["watch"] = engine.watch()
        snap["deviation"] = engine.deviation(route)
        snap["drift"] = engine.drift(route)
        snap["plangap"] = engine.plangap(route)
    if kind == "handover":
        snap["navigator"] = engine.navigator(route)
        snap["strategy"] = engine.strategy(route)
        snap["sail"] = engine.sail()
        snap["buoys"] = engine.buoys(route)
    if kind == "recap":
        snap["fleet"] = engine.fleet()
        snap["sails_log"] = engine._get("/sails/log")
    if kind == "mark":
        snap["navigator"] = engine.navigator(route)
        snap["sail"] = engine.sail()
        snap["watch"] = engine.watch()
        snap["plays"] = engine.plays(route)
    if kind == "watchlist":
        snap["plays"] = engine.plays(route)
        snap["drift"] = engine.drift(route)
        snap["plangap"] = engine.plangap(route)
        snap["deviation"] = engine.deviation(route)
    return snap


def _mark_sections(snap, pb):
    nav = snap.get("navigator") or {}
    if not nav.get("available") or not (nav.get("next_mark") or {}).get("name"):
        return None, "No next mark — nothing to pre-brief."
    nm = nav["next_mark"]
    secs = [_leg_lines(snap)]
    # the leg AFTER the rounding — the navigator's homework for it
    nr = nav.get("next_rounding") or {}
    after = []
    if nr.get("leg_type") or nr.get("type"):
        after.append(f"After the rounding: {nr.get('leg_type') or nr.get('type')} leg.")
    sail = snap.get("sail") or {}
    nx = sail.get("next_xover") or sail.get("next_crossover")
    if isinstance(nx, dict) and (nx.get("to_sail") or nx.get("sail")):
        after.append(f"Sail change waiting: {nx.get('to_sail') or nx.get('sail')} — "
                     "stage it before the mark.")
    secs.append(_sec("The rounding", after, ["get_navigator", "get_sail_advice"]))
    # who takes the rounding: watch countdown vs mark ETA
    w = snap.get("watch") or {}
    eta = nm.get("eta_min")
    if (w.get("plan_set") and w.get("active") and isinstance(eta, (int, float))
            and w.get("mins_to_change") is not None):
        who = (w.get("next_on_label") or w.get("next_on")) if w["mins_to_change"] < eta \
            else (w.get("on_label") or w.get("on"))
        if who:
            secs.append(_sec("Crew", [f"{who} take(s) this rounding."], ["get_watch"]))
    # plays authored for the NEXT leg (arriving-mark index + 1)
    plays = snap.get("plays") or {}
    nxt_leg = (nm.get("index") or 0) + 1
    coming = [p for p in (plays.get("plays") or [])
              if isinstance((p.get("applicability") or {}).get("legs"), list)
              and nxt_leg in p["applicability"]["legs"]]
    if coming:
        secs.append(_sec("Next leg's plays",
                         [f"{p.get('name')} ({p.get('status')})" for p in coming[:3]],
                         ["get_plays"]))
    headline = f"{nm['name']} in ~{int(eta)} min" if isinstance(eta, (int, float)) \
        else f"Approaching {nm['name']}"
    return [s for s in secs if s], headline


def _assemble(kind, snap, pb):
    """(sections, deterministic headline). Sections are always deterministic engine facts."""
    if kind == "handover":
        secs = [_wind_lines(snap), _sail_window_lines(snap, pb), _leg_lines(snap),
                _strategy_lines(snap), _plays_lines(snap), _divergence_lines(snap),
                _buoy_lines(snap), _crew_lines(snap)]
        w = snap.get("watch") or {}
        head = "Watch handover"
        if w.get("next_on") and w.get("mins_to_change") is not None:
            head = (f"Handover to {w.get('next_on_label') or w.get('next_on')} in "
                    f"{int(w['mins_to_change'])} min")
        return [s for s in secs if s], head
    if kind == "recap":
        secs = [_wind_lines(snap), _crew_lines(snap), _divergence_lines(snap),
                _fleet_lines(snap), _plays_lines(snap, include_watchlist=False),
                _recap_events(snap)]
        return [s for s in secs if s], "The last hour"
    if kind == "mark":
        return _mark_sections(snap, pb)
    if kind == "watchlist":
        plays = snap.get("plays") or {}
        rows = _watchlist_rows(plays)
        for p in (plays.get("plays") or []):
            if p.get("status") == "arming":
                sp = f" ({p['sustain_pct']}% held)" if p.get("sustain_pct") is not None else ""
                rows.insert(0, f"Arming: {p.get('name')}{sp}")
        secs = [_sec("Closest to flipping", rows, ["get_plays"]), _divergence_lines(snap)]
        n = len([r for r in rows])
        return [s for s in secs if s], (f"{n} trigger(s) worth watching" if n
                                        else "Nothing close to flipping the plan")
    return [], "?"


def _flatten(sections):
    out = []
    for s in sections:
        out.append(s["title"] + ": " + " ".join(s["lines"]))
    return "\n".join(out)


def _phrase(kind, headline, body_text, llm):
    """LLM rewrite of the deterministic brief — numbers fixed, tone crew-facing. Raises
    LLMUnavailable on any trouble; the caller keeps the deterministic text."""
    desc, style = _KIND_STYLE[kind]
    from .copilot import _WIND_VOCAB, _extract_json     # shared conventions (no import cycle)
    sys_p = (_WIND_VOCAB + "\n"
             "You are the SR33's onboard coach writing the " + desc + ". Below is the "
             "DETERMINISTIC brief — every number in it is an engine fact. Rewrite it as "
             + style + ", plain tactician's language, most important first. Reuse the numbers "
             "EXACTLY as given; invent nothing, add no numbers, no new tactics, no commands. "
             'Return ONLY JSON: {"headline": "...", "body": "..."}')
    msg = llm.chat([{"role": "system", "content": sys_p},
                    {"role": "user", "content": f"HEADLINE: {headline}\n{body_text}"}],
                   schema=_BRIEF_SCHEMA)
    parsed = _extract_json(msg.get("content") or "")
    if not parsed or not (parsed.get("body") or "").strip():
        raise LLMUnavailable("brief phrasing: no usable JSON")
    return (parsed.get("headline") or headline).strip()[:120], parsed["body"].strip()[:1200]


def make(kind, route=None, use_llm: bool | None = None) -> dict:
    """One crew brief, always produced: deterministic sections + optional LLM phrasing on top."""
    if kind not in KINDS:
        return {"available": False, "kind": kind, "note": f"unknown brief kind (have {KINDS})"}
    route = route or config.DEFAULT_ROUTE
    engine = EngineClient()
    pb = playbook_mod.load()
    snap = _gather(engine, kind, route)
    snap["_pb"] = pb

    sections, headline = _assemble(kind, snap, pb)
    notes = _data_notes(snap, engine)
    body = _flatten(sections or [])
    if not sections:
        body = headline if isinstance(headline, str) else "Nothing to brief."

    out = {"available": True, "kind": kind, "headline": headline, "body": body,
           "sections": sections or [], "data_notes": notes, "mode": "deterministic",
           "generated_at": round(time.time()),
           "grounded_in": sorted({g for s in (sections or []) for g in s["grounded_in"]}),
           "_meta": {"route": route, "engine": engine.base_url, "llm_used": False,
                     "playbook_loaded": pb.loaded, "model": None}}

    want_llm = config.USE_LLM if use_llm is None else use_llm
    if want_llm and sections:
        try:
            head2, body2 = _phrase(kind, headline, body, LLMClient())
            out.update({"headline": head2, "body": body2, "mode": "llm"})
            out["deterministic_body"] = body
            out["_meta"].update({"llm_used": True, "model": config.LLM_MODEL})
        except LLMUnavailable as e:
            out["_meta"]["llm_error"] = str(e)
    if notes:
        out["body"] = "⚠ " + " ".join(notes) + "\n" + out["body"]
    return out
