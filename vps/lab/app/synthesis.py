"""Lab-2b — Opus synthesis → the signed, onboard-loadable PLAYBOOK BUNDLE.

Lab-2a (`playbook.build_playbook`) clusters the forecast fan-out into a small set of strategic
VARIANTS (which side of the first beat each model favors, with the agreement distribution and the
time stakes). That's the raw material. Lab-2b turns it into the artifact the crew actually carries:

  - per-variant crew-facing **summary / rationale / tradeoffs**, and crucially the **what-flips-it**
    — the OBSERVABLE on-the-water trigger (a wind shift past a bearing, a pressure line) that says
    "abandon this variant, the other side is now favored". That trigger is what makes the playbook
    *branching* rather than a single frozen route.
  - a **decision tree** the crew follows from the gun: the default variant at the start, then the
    observe→switch branches.
  - a **signature** (sha256 over the canonical content) so the bundle is tamper-evident — "frozen
    at the gun". The onboard copilot verifies it on load.

The bundle schema is `c4.playbook/v1` and is deliberately a superset of what the onboard copilot's
`playbook.Playbook` reads (`race_id`, `variants[].id/summary/what_flips_it`) — so freezing a bundle
and dropping it at the copilot's `PLAYBOOK_PATH` is the whole onboard wiring. The frontier model
(Opus) writes the narrative; a deterministic fallback always produces a valid bundle with no key, so
the Lab never depends on the model being reachable. RRS 41: all pre-race cloud homework, frozen at
the gun — the copilot SELECTS/INTERPRETS these variants in-race, it never originates new strategy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time

from shared import race_def
from . import playbook as pb
from . import sailplan

SCHEMA = "c4.playbook/v1"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SIGNED_BY = os.environ.get("PLAYBOOK_SIGNED_BY", "C4 Performance Lab")


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def first_beat_rhumb(definition, course_id):
    """Bearing from the start to the first mark — the reference the favored-side / what-flips-it
    triggers are expressed against (left/right OF the rhumb). None if the course lacks geometry."""
    try:
        marks, _s, _c = race_def.course_to_marks(definition, course_id)
    except Exception:
        return None
    if len(marks) < 2:
        return None
    return round(_bearing(marks[0][2], marks[0][3], marks[1][2], marks[1][3]), 0)


# ---------------------------------------------------------------------------- canonical signing

def canonical_bytes(bundle: dict) -> bytes:
    """The exact bytes the signature covers: the bundle minus its `signature` field, serialized
    deterministically (sorted keys, no spaces). Lab signs over this; the copilot verifies over the
    identical function, so a bundle that loads byte-for-byte verifies."""
    content = {k: v for k, v in bundle.items() if k != "signature"}
    return json.dumps(content, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_bundle(bundle: dict, signed_by: str = SIGNED_BY) -> dict:
    """Attach a sha256 signature over the canonical content. Frozen-at-the-gun integrity."""
    digest = hashlib.sha256(canonical_bytes(bundle)).hexdigest()
    bundle["signature"] = {"alg": "sha256", "value": digest,
                           "signed_at": round(time.time()), "signed_by": signed_by}
    return bundle


def verify_bundle(bundle: dict) -> bool:
    sig = bundle.get("signature") or {}
    if sig.get("alg") != "sha256" or not sig.get("value"):
        return False
    return hashlib.sha256(canonical_bytes(bundle)).hexdigest() == sig["value"]


# ---------------------------------------------------------------------------- deterministic synthesis

_OPP = {"left": "right", "right": "left", "middle": "either side"}


def _trigger_text(side, rhumb):
    """The observable wind-direction trigger for a side, expressed against the first-beat rhumb."""
    if rhumb is None:
        if side == "left":
            return "the breeze backs (heads port tack) early — if it veers right instead, the right side pays"
        if side == "right":
            return "the breeze veers (heads starboard tack) early — if it backs left instead, the left side pays"
        return "the breeze stays steady near the rhumb — a persistent shift either way favors that side"
    left_b = int((rhumb - 10) % 360)
    right_b = int((rhumb + 10) % 360)
    if side == "left":
        return (f"the breeze sits left of the rhumb ({int(rhumb)}°) / backs in the first hour — but if "
                f"it veers right of ~{right_b}° and holds, switch to the right variant")
    if side == "right":
        return (f"the breeze sits right of the rhumb ({int(rhumb)}°) / veers in the first hour — but if "
                f"it backs left of ~{left_b}° and holds, switch to the left variant")
    return (f"the breeze holds within ~10° of the rhumb ({int(rhumb)}°) — if a persistent shift sets "
            "in either way, follow it to that side's variant")


def _deterministic_synthesis(playbook: dict, definition, course_id):
    """Build summary/rationale/tradeoffs/what_flips_it + headline + decision_tree from the 2a numbers
    alone. Always available (no API key), and the floor the Opus path improves on."""
    variants = playbook["variants"]
    rhumb = first_beat_rhumb(definition, course_id)
    spread = playbook.get("decision_spread_min") or 0.0
    best_h = min((v["total_hours"] for v in variants if v.get("total_hours") is not None),
                 default=None)
    syn = {}
    for v in variants:
        side, label = v["side"], v["label"]
        models = ", ".join(m.upper() for m in v.get("supported_by", [])) or "the blended field"
        share = int(round((v.get("share") or 0) * 100))
        hrs = v.get("total_hours")
        rng = v.get("hours_range")
        cost = None
        if best_h is not None and hrs is not None:
            cost = round((hrs - best_h) * 60)
        syn[side] = {
            "summary": f"{label}: {models} back it ({share}% of scenarios), ~{hrs} h.",
            "rationale": (f"{models} favor committing {side} off the line. Representative route "
                          f"~{hrs} h" + (f" (scenario spread {rng[0]}–{rng[1]} h)" if rng else "")
                          + f", route confidence {v.get('route_confidence')}."),
            "tradeoffs": (f"If you commit {side} and the wind ends up favoring the {_OPP[side]}, you "
                          f"give up roughly the {round(spread)} min decision spread across the side "
                          "options." if spread else
                          f"Low stakes here — the side options finish within a few minutes; commit "
                          f"{side} for the cleaner lane."),
            "what_flips_it": _trigger_text(side, rhumb),
        }
    # recommended = highest-share variant (variants already sorted by -share in 2a)
    rec = variants[0]["side"] if variants else None
    rec_label = variants[0]["label"] if variants else "—"
    agree = int(round((playbook.get("agreement") or 0) * 100))
    headline = (f"Gameplan: start on the {rec_label.lower()} ({agree}% of forecasts agree); "
                f"the decision is worth ~{round(spread)} min, so watch the first-beat breeze and be "
                "ready to switch." if rec else "No clear gameplan — insufficient route geometry.")
    # decision tree: default at the gun, then one observe→switch branch per other variant
    tree = []
    if rec:
        tree.append({"node": "start", "observe": "at the gun, before the first shift commits",
                     "action": f"start on the {rec_label.lower()} — the consensus default",
                     "variant": rec})
        for v in variants:
            if v["side"] == rec:
                continue
            tree.append({"node": "branch", "observe": syn[v["side"]]["what_flips_it"],
                         "action": f"switch to {v['label']}", "variant": v["side"]})
    return {"headline": headline, "recommended": rec, "variants": syn,
            "decision_tree": tree, "synth_model": "deterministic"}


# ---------------------------------------------------------------------------- Opus synthesis

def _opus_synthesis(playbook: dict, definition, course_id, race_name):
    """Have Opus write the crew-facing narrative + decision tree over the 2a variants. The routes
    are ALREADY computed — Opus narrates and structures, it never invents a route. Returns the same
    shape as `_deterministic_synthesis` or None on any failure (caller falls back)."""
    if not API_KEY:
        return None
    rhumb = first_beat_rhumb(definition, course_id)
    facts = {
        "race": race_name, "first_beat_rhumb_deg": rhumb,
        "agreement": playbook.get("agreement"),
        "decision_spread_min": playbook.get("decision_spread_min"),
        "n_scenarios": playbook.get("n_scenarios"),
        "consensus": {k: playbook["consensus"].get(k) for k in
                      ("favored_side", "total_hours", "route_confidence")},
        "variants": [{"id": v["side"], "label": v["label"], "supported_by": v.get("supported_by"),
                      "share": v.get("share"), "total_hours": v.get("total_hours"),
                      "hours_range": v.get("hours_range"), "first_heading": v.get("first_heading"),
                      "route_confidence": v.get("route_confidence")}
                     for v in playbook["variants"]],
    }
    ids = [v["side"] for v in playbook["variants"]]
    system = (
        "You are an expert yacht-racing navigator writing the PRE-RACE branching PLAYBOOK the crew "
        "will carry aboard. You are given strategic route VARIANTS that an optimizer ALREADY computed "
        "by fanning the forecast across weather models and clustering by which side of the first beat "
        "each favors — with the model agreement and the time stakes. DO NOT invent routes or numbers; "
        "narrate and structure what's given.\n"
        "For each variant write, for the crew: a one-line `summary`; a `rationale` (which models back "
        "it and why that side); the `tradeoffs` (what you give up / the time cost if you commit there "
        "and the wind favors the other side); and — most important — `what_flips_it`: the concrete, "
        "OBSERVABLE on-the-water trigger (a wind shift past a specific bearing relative to the rhumb, "
        "a pressure line, a persistent vs oscillating shift) that tells the crew to abandon this "
        "variant for another. Express bearings against the first-beat rhumb when given.\n"
        "Then pick `recommended` (the default to start on — normally the highest-agreement variant) "
        "and build a `decision_tree`: an ordered list the crew follows from the gun — the start "
        "default, then observe→switch branches. Be specific, concise, no preamble.\n"
        f"Return STRICT JSON only, no markdown, with keys: headline (str), recommended (one of "
        f"{ids}), variants (object keyed by the variant id, each {{summary,rationale,tradeoffs,"
        "what_flips_it}}), decision_tree (list of {observe,action,variant}). variant ids in "
        f"`recommended`/`decision_tree[].variant` MUST be from {ids}.")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=API_KEY)
        resp = client.messages.create(
            model=MODEL, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": "Variants:\n" + json.dumps(facts, indent=2)}],
        )
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if txt.startswith("```"):
            txt = txt.split("```", 2)[1].lstrip("json").strip() if "```" in txt[3:] else txt.strip("`")
        data = json.loads(txt)
    except Exception:
        return None
    # validate / coerce against the variant ids — drop anything ungrounded
    vars_in = data.get("variants") or {}
    syn = {}
    for vid in ids:
        d = vars_in.get(vid) or {}
        syn[vid] = {k: str(d.get(k) or "").strip() for k in
                    ("summary", "rationale", "tradeoffs", "what_flips_it")}
        if not syn[vid]["what_flips_it"]:           # never ship a variant with no trigger
            syn[vid]["what_flips_it"] = _trigger_text(vid, rhumb)
    rec = data.get("recommended")
    if rec not in ids:
        rec = ids[0] if ids else None
    tree = [n for n in (data.get("decision_tree") or [])
            if isinstance(n, dict) and n.get("variant") in ids]
    if not tree:                                    # fall back to a deterministic tree
        tree = _deterministic_synthesis(playbook, definition, course_id)["decision_tree"]
    return {"headline": str(data.get("headline") or "").strip(),
            "recommended": rec, "variants": syn, "decision_tree": tree, "synth_model": MODEL}


# ---------------------------------------------------------------------------- bundle assembly

def _boat_model():
    """The reviewed boat sail/draft model frozen into the bundle — so the polars + sail crossovers
    the homework assumes travel onboard (the copilot/engine can surface the per-leg sail plan and the
    crew can review what was loaded). Boat-scoped via the Lab's active BoatProfile."""
    try:
        from . import boats
        b = boats.active_boat() or {}
    except Exception:
        b = {}
    sm = sailplan.model()
    draft_m = b.get("draft_m")
    return {
        "boat_id": b.get("boat_id") or sm.get("boat_id"),
        "name": b.get("name"),
        "draft_m": draft_m,
        "draft_ft": round(draft_m / 0.3048, 1) if draft_m is not None else None,
        "polars_source": sm.get("source"),
        "sail_inventory": sm.get("inventory", []),
        "sail_names": sm.get("sail_names", {}),
        "crossovers": sm.get("crossovers", {}),     # per-TWS sail zones — the reviewable model
    }


def synthesize(definition, course_id, start_epoch, models, ensemble_members=0, time_budget_s=200):
    """Lab-2a fan-out → Lab-2b synthesized bundle (UNSIGNED draft). Freeze (`sign_bundle`) before
    it's relied on / deployed onboard. Passes through the not-available case from 2a unchanged."""
    playbook = pb.build_playbook(definition, course_id, start_epoch, models,
                                 ensemble_members=ensemble_members, time_budget_s=time_budget_s)
    if not playbook.get("available"):
        return playbook

    syn = _opus_synthesis(playbook, definition, course_id, definition.get("name", "")) \
        or _deterministic_synthesis(playbook, definition, course_id)

    # merge the narrative onto each 2a variant, in copilot-loadable shape
    variants = []
    for v in playbook["variants"]:
        s = syn["variants"].get(v["side"], {})
        variants.append({
            "id": v["side"], "name": v["label"],
            "summary": s.get("summary", ""), "rationale": s.get("rationale", ""),
            "tradeoffs": s.get("tradeoffs", ""), "what_flips_it": s.get("what_flips_it", ""),
            "supported_by": v.get("supported_by"), "share": v.get("share"),
            "total_hours": v.get("total_hours"), "hours_range": v.get("hours_range"),
            "first_heading": v.get("first_heading"), "route_confidence": v.get("route_confidence"),
            "sail_plan": (v.get("route") or {}).get("sail_plan"),
            "route": v.get("route"),
        })

    bundle = {
        "schema": SCHEMA,
        "race_id": definition.get("race_id"),
        "race_name": definition.get("name"),
        "course_id": playbook.get("course_id") or course_id,
        "start_epoch": playbook.get("start_epoch"),
        "generated_at": round(time.time()),
        "headline": syn.get("headline", ""),
        "recommended": syn.get("recommended"),
        "agreement": playbook.get("agreement"),
        "decision_spread_min": playbook.get("decision_spread_min"),
        "first_beat_rhumb_deg": first_beat_rhumb(definition, course_id),
        "consensus": playbook.get("consensus"),
        "boat_model": _boat_model(),      # polars/sail crossovers + draft frozen into the homework
        "variants": variants,
        "decision_tree": syn.get("decision_tree", []),
        "provenance": {
            "models": [m["model"] for m in playbook.get("windfield", {}).get("models", [])],
            "n_scenarios": playbook.get("n_scenarios"),
            "n_variants": playbook.get("n_variants"),
            "synth_model": syn.get("synth_model"),
            "scenarios": playbook.get("scenarios"),
        },
        "windfield": playbook.get("windfield"),
        "skipped_marks": playbook.get("skipped_marks", []),
        "signature": None,            # unsigned draft until frozen
    }
    return bundle
