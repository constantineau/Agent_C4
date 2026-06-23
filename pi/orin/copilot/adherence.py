"""Playbook adherence — are we sailing the frozen homework, and has a branch trigger fired?

Powers the **PLAYBOOK-ADHERENCE** dashboard tile (docs/COPILOT_DASHBOARD.md — the one remaining
"later tile", unblocked by Lab-2 + the playbook loader). Lab-2 froze a *branching* playbook aboard:
N strategic VARIANTS, each favoring a side of the first beat (`id` = left|middle|right), a
`recommended` start default, and per-variant `what_flips_it` — the OBSERVABLE on-the-water trigger
(a persistent wind shift relative to the first-beat rhumb) that says "abandon this variant, the
other side now pays". This module answers, deterministically and at a glance:

  - which variant we're on (the recommended start default),
  - whether we're ON PLAN (oscillating, or a persistent shift confirms the recommended side), and
  - whether a BRANCH TRIGGER FIRED (a persistent shift now favors a DIFFERENT variant → switch).

It SELECTS/INTERPRETS the pre-authored variants against the engine's tactical read — it never
originates strategy (the RRS-41 posture, same as the rest of the copilot). The engine does the math
(persistent vs oscillating, favored side); this maps that onto the frozen homework. **No LLM** — the
tile's truth is deterministic and always available; the dashboard's LLM commentary layer may still
reference the tile through grounding-as-routing.

The return shape IS a dashboard tile object (status/value/sub/why/consider/clears/based/rows/conf),
so the front-end passes it straight through, plus a few extras (headline/recommended/agreement_pct)
for the tap-to-detail view.
"""
from . import brief as brief_mod

_num = brief_mod._num


def _variant_by_id(playbook, vid):
    if vid is None:
        return None
    for v in playbook.variants:
        if str(v.get("id") or v.get("name")) == str(vid):
            return v
    return None


def _variant_for_side(playbook, side):
    """The variant for a first-beat side. Variant ids ARE the side (left|middle|right; from the Lab
    synthesis), so this is an exact id match — no fragile text matching (every `what_flips_it`
    mentions both sides)."""
    if side not in ("left", "right", "middle"):
        return None
    for v in playbook.variants:
        if str(v.get("id") or "").lower() == side:
            return v
    return None


def _label(v, fallback="?"):
    if not v:
        return fallback
    return v.get("name") or str(v.get("id") or fallback)


def _na(note):
    return {"available": False, "status": "na", "value": "—", "sub": note,
            "why": note, "consider": "—", "clears": "—", "based": [], "conf": "engine"}


def evaluate(playbook, snapshot):
    """Deterministic adherence tile payload. `playbook` is a copilot `Playbook`; `snapshot` carries
    at least `get_tactics` (a `copilot.gather`-style dict keyed by tool name)."""
    if playbook is None or not getattr(playbook, "loaded", False):
        return _na("no playbook aboard")
    variants = playbook.variants
    if not variants:
        return _na("playbook has no variants")

    data = playbook.data
    rec_v = _variant_by_id(playbook, data.get("recommended")) or variants[0]
    rec_id = str(rec_v.get("id") or data.get("recommended") or "")
    rec_label = _label(rec_v, "the start plan")
    headline = data.get("headline") or ""
    agreement = data.get("agreement")
    spread = data.get("decision_spread_min")
    agree_pct = round(agreement * 100) if isinstance(agreement, (int, float)) else None

    tac = snapshot.get("get_tactics") or {}
    has_tac = tac.get("available") is True
    wind = (tac.get("wind") or {}) if has_tac else {}
    persistent = bool(wind.get("persistent"))
    favored = tac.get("favored_side") if has_tac else None     # left | right | either
    osc = _num(wind.get("oscillation_deg"))
    shift = wind.get("shift_deg")
    trend = wind.get("trend")
    trend_txt = (trend + " ") if trend and trend != "steady" else ""

    based = [f"playbook:{rec_id}"] + ([f"agreement {agree_pct}%"] if agree_pct is not None else [])

    def _rows():
        out = [{"hdr": True, "cols": ["agree", ""]}]
        for v in variants[:4]:
            vid = str(v.get("id") or "")
            sh = v.get("share")
            shp = f"{round(sh * 100)}%" if isinstance(sh, (int, float)) else "—"
            tags = []
            if vid == rec_id:
                tags.append("start")
            if favored and favored != "either" and vid == favored:
                tags.append("now")
            out.append({"label": ("★ " if vid == rec_id else "") + _label(v, vid),
                        "emph": bool(favored and favored != "either" and vid == favored),
                        "cols": [shp, " · ".join(tags)]})
        return out

    base = {"available": True, "based": based, "rows": _rows(), "conf": "engine",
            "headline": headline, "recommended": rec_label, "agreement_pct": agree_pct,
            "decision_spread_min": spread, "what_flips_it": rec_v.get("what_flips_it") or ""}

    # No live tactical read yet (pre-start / building the wind baseline) → hold the start plan.
    if not has_tac:
        return {**base, "status": "ok", "value": rec_label, "sub": "start plan · holding",
                "why": (headline or f"Playbook recommends starting {rec_label}.")
                       + " No live tactical read yet — holding the recommended start.",
                "consider": f"Set up for the {rec_label} start per the gameplan.", "clears": "—"}

    on_rec_side = favored == rec_id
    flip_v = _variant_for_side(playbook, favored) if favored in ("left", "right") else None

    # A persistent shift favouring a DIFFERENT side: the playbook's branch trigger has fired → ACT.
    if persistent and favored in ("left", "right") and not on_rec_side:
        if flip_v is not None:
            flip_label = _label(flip_v, favored)
            trig = flip_v.get("what_flips_it") or f"switch to {flip_label}."
            return {**base, "status": "act", "value": f"Switch → {flip_label}",
                    "sub": f"branch: persistent {trend_txt}shift favors {favored}",
                    "why": (f"A persistent {trend_txt}shift now favors the {favored} side — against "
                            f"the recommended '{rec_label}'. This is the playbook's branch trigger: "
                            + trig),
                    "consider": f"Execute the branch — commit {favored} per variant '{flip_label}'.",
                    "clears": "the shift reverses / settles back toward the rhumb",
                    "based": based + ["get_tactics", f"playbook:{flip_v.get('id')}"],
                    "what_flips_it": flip_v.get("what_flips_it") or ""}
        # Persistent divergence but no variant aboard for that side — honest watch, can't name a branch.
        return {**base, "status": "watch", "value": f"Off plan: {rec_label}",
                "sub": f"persistent {trend_txt}shift favors {favored}",
                "why": (f"A persistent {trend_txt}shift favors the {favored} side, off the recommended "
                        f"'{rec_label}' — but there's no pre-authored variant for that side aboard."),
                "consider": f"The breeze has gone {favored} against the plan — reassess with the crew.",
                "clears": "the shift reverses", "based": based + ["get_tactics"]}

    # A persistent shift CONFIRMS the recommended side → ON PLAN.
    if persistent and on_rec_side:
        return {**base, "status": "ok", "value": f"On plan: {rec_label}",
                "sub": f"persistent {trend_txt}shift confirms {favored}",
                "why": (f"A persistent {trend_txt}shift favors the {favored} side — exactly what the "
                        f"recommended variant '{rec_label}' plays. Stay committed."),
                "consider": "Commit to the gameplan side — the shift backs it.",
                "clears": "—", "based": based + ["get_tactics"]}

    # Oscillating: is the instantaneous lean drifting toward a non-recommended side? → early-warning WATCH.
    lean = None
    if shift is not None and osc:
        thr = max(4.0, osc * 0.25)
        lean = "right" if shift > thr else "left" if shift < -thr else None
    lean_v = _variant_for_side(playbook, lean) if lean in ("left", "right") else None
    if lean and lean_v is not None and str(lean_v.get("id")) != rec_id:
        lean_label = _label(lean_v, lean)
        return {**base, "status": "watch", "value": f"On plan: {rec_label}",
                "sub": f"watch a {lean} lean → {lean_label}",
                "why": (f"Still oscillating (±{round(osc / 2)}°) so the recommended "
                        f"'{rec_label}' start holds, but the breeze is leaning {lean}. If it turns "
                        f"persistent, the playbook branches to '{lean_label}'."),
                "consider": f"Hold the gameplan but watch the {lean} side — a persistent shift flips it.",
                "clears": "the breeze settles / the recommended side reasserts",
                "based": based + ["get_tactics"], "what_flips_it": lean_v.get("what_flips_it") or ""}

    # Oscillating, no meaningful lean → ON PLAN (the start default stands).
    return {**base, "status": "ok", "value": f"On plan: {rec_label}",
            "sub": (f"oscillating ±{round(osc / 2)}°" if osc else "holding the start plan"),
            "why": ("Wind is oscillating — no persistent shift. The recommended variant "
                    f"'{rec_label}' stands; play the shifts within the band."
                    + (f" {headline}" if headline else "")),
            "consider": "Hold the gameplan — tack on the headers, no branch yet.",
            "clears": "—", "based": based + ["get_tactics"]}
