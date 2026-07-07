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
the gun — the strategy library the onboard copilot matches conditions against in-race (the LLM never
originates strategy; only the deterministic engine may flag an off-book departure — descope 2026-07-06).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time

from shared import race_def
from . import forecast_ref
from . import llm as lab_llm
from . import playbook as pb
from . import sailplan

SCHEMA = "c4.playbook/v1"
SCHEMA_V2 = "c4.playbook/v2"          # v1 superset: + plays[]/nominal/corridor/venue_stats
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
    """The observable wind-direction trigger for a side, expressed against the first-beat rhumb.
    Racer-native: 'shifted right/left' + the degree threshold (never veer/back); a right shift heads
    port tack, a left shift heads starboard."""
    if rhumb is None:
        if side == "left":
            return "the breeze shifts left early (heads starboard) — if it shifts right instead, the right side pays"
        if side == "right":
            return "the breeze shifts right early (heads port) — if it shifts left instead, the left side pays"
        return "the breeze stays steady near the rhumb — a persistent shift either way favors that side"
    left_b = int((rhumb - 10) % 360)
    right_b = int((rhumb + 10) % 360)
    if side == "left":
        return (f"the breeze sits left of the rhumb ({int(rhumb)}°) / shifts left in the first hour — but if "
                f"it shifts right past ~{right_b}° and holds, switch to the right variant")
    if side == "right":
        return (f"the breeze sits right of the rhumb ({int(rhumb)}°) / shifts right in the first hour — but if "
                f"it shifts left past ~{left_b}° and holds, switch to the left variant")
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
        "WIND LANGUAGE: never say 'veer' or 'back' — say the wind shifted RIGHT or LEFT. Whenever you "
        "state a wind-direction change, give the baseline and the new bearing in degrees (e.g. 'shifts "
        "right past ~265°' / 'from the rhumb 250° to 265°'), never a bare delta. A right shift heads "
        "port tack, a left shift heads starboard.\n"
        "Then pick `recommended` (the default to start on — normally the highest-agreement variant) "
        "and build a `decision_tree`: an ordered list the crew follows from the gun — the start "
        "default, then observe→switch branches. Be specific, concise, no preamble.\n"
        f"Return STRICT JSON only, no markdown, with keys: headline (str), recommended (one of "
        f"{ids}), variants (object keyed by the variant id, each {{summary,rationale,tradeoffs,"
        "what_flips_it}}), decision_tree (list of {observe,action,variant}). variant ids in "
        f"`recommended`/`decision_tree[].variant` MUST be from {ids}.")
    try:
        txt, used_model = lab_llm.complete(
            system, "Variants:\n" + json.dumps(facts, indent=2), max_tokens=4000)
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
            "recommended": rec, "variants": syn, "decision_tree": tree, "synth_model": used_model}


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
        # the boat's own inventory (incl. J2/J3) wins over the cert's single-headsail list
        "sail_inventory": b.get("sail_inventory") or sm.get("inventory", []),
        "sail_names": sm.get("sail_names", {}),
        # per-TWS×TWA zones with the upwind jib specialised to J1/J2/J3 by this boat's change-downs
        "crossovers": sailplan.crossovers_specialized(
            b.get("jib_crossovers") or [],
            {"code0": b.get("code0") or {}, "main_reefs": b.get("main_reefs") or {}}),
        # per-TWS toss-up bands (two sails within ~2% of target — the near-ties the zones can't show)
        "overlaps": sailplan.overlaps_specialized(b.get("jib_crossovers") or []),
        "jib_crossovers": b.get("jib_crossovers", []),  # J1/J2/J3 by TWS (crew/sailmaker, not cert)
        # Code 0 band + main reef points — crew config the cert can't express (label overlays);
        # frozen so the copilot's sail-configuration calls are grounded in the reviewed model
        "code0": b.get("code0") or {},
        "main_reefs": b.get("main_reefs") or {},
    }


def _buoy_stations(definition, course_id, pad_deg=0.6):
    """Freeze the race's BUOY station list into the bundle (the homework pattern): every NDBC
    station in the Lab's curated venue list within the course bbox (+ a pad). Onboard, buoys.py
    reads this block for the up-course leading indicator. Best-effort — omitted when none."""
    try:
        from . import optimizer as _opt
        from . import venue as _venue
        bbox = _opt.course_bbox(definition, course_id)
        if not bbox:
            return None
        n, s_, w, e = bbox
        out = [{"id": st["id"], "lat": st["lat"], "lon": st["lon"], "name": st.get("name")}
               for st in _venue.STATIONS if st.get("kind") == "ndbc"
               and s_ - pad_deg <= st["lat"] <= n + pad_deg
               and w - pad_deg <= st["lon"] <= e + pad_deg]
        return out or None
    except Exception:
        return None


def _fingerprint(variants, recommended):
    """Freeze the common forecast the playbook was built on (sampled along the recommended variant's
    route) so the onboard executor can measure FORECAST-DRIFT. Best-effort — None if the route is
    empty or Open-Meteo is unreachable (the bundle simply omits it)."""
    try:
        v = next((x for x in variants if x.get("id") == recommended), None) or \
            (variants[0] if variants else None)
        path = ((v or {}).get("route") or {}).get("path")
        return forecast_ref.build_fingerprint(path)
    except Exception:
        return None


# ---- background synthesis job: the v2 scenario fan makes a full synthesize ~10 min — far past
# the gateway's 300 s — so the UI starts a job and polls (same pattern as the retro fleet batch).
_JOB = {"state": "idle"}
_JOB_LOCK = threading.Lock()


def start_job(definition, course_id, start_epoch, models, ensemble_members=0, **kw) -> dict:
    with _JOB_LOCK:
        if _JOB.get("state") == "running":
            return {"ok": False, "note": "a synthesis is already in progress"}
        _JOB.clear()
        _JOB.update({"state": "running", "race_id": definition.get("race_id"),
                     "started_at": time.time()})

    def _work():
        try:
            out = synthesize(definition, course_id, start_epoch, models,
                             ensemble_members=ensemble_members, **kw)
            _JOB.update({"state": "done", "bundle": out})
        except Exception as exc:      # noqa: BLE001 — the job must record any failure
            _JOB.update({"state": "error", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "state": "running"}


def job_status() -> dict:
    return dict(_JOB)


def _venue_stats():
    """Fleet-normal stats + side history from the retro archive (locked Phase-B input #3/#7) —
    frozen into the bundle so the onboard matcher phrases against the venue's empirical
    distribution instead of guesses. Best-effort: no archive → omitted."""
    try:
        from . import retro
        return retro.venue_stats()
    except Exception:
        return None


def _mean_tws(consensus):
    ws = [(l.get("wind") or {}).get("tws") for l in (consensus or {}).get("legs") or []]
    ws = [w for w in ws if isinstance(w, (int, float))]
    return round(sum(ws) / len(ws), 1) if ws else None


def _downsample(path, max_pts=200):
    if not path or len(path) <= max_pts:
        return path
    step = max(1, len(path) // max_pts)
    out = path[::step]
    if out[-1] is not path[-1]:
        out.append(path[-1])
    return out


def _play_synthesis(plays_in, corridor, profile, race_name):
    """Fable writes each play's crew-facing text (summary/rationale/tradeoffs/what_flips_it +
    the condition NARRATIVE the onboard matcher pattern-matches). Deterministic fallback = the
    registry seeds, so a play always ships with valid conditions."""
    if not plays_in or not API_KEY:
        return {}, None
    facts = [{"id": p["id"], "name": p["name"], "scenario_params": p["params"],
              "divergence_min": p["divergence"]["delta_eta_min"],
              "divergence_nm": p["divergence"]["xte_mean_nm"],
              "favored_side": p.get("favored_side"), "seed": p["narrative_seed"]} for p in plays_in]
    system = (
        "You are the pre-race strategist for a yacht race, writing the PLAYS of a playbook the "
        "crew carries frozen from the gun. Each play is a pre-computed alternate routing for a "
        "SCENARIO (the wind departing from the forecast in a specific way). For each play write, "
        "for the crew: `summary` (one line); `narrative` — the CONDITION in a tactician's words: "
        "what you would actually OBSERVE on the water when this scenario is real (grounded in the "
        "scenario params — a 20° right rotation, +25% pressure, the system running 3h early), so "
        "an onboard assistant can MATCH the live situation against it; `rationale` (why the "
        "alternate route pays then); `tradeoffs`; `what_flips_it` (the observable that says the "
        "scenario has passed / reversed — hand back to the nominal). WIND LANGUAGE: never "
        "'veer'/'back' — say RIGHT/LEFT with degrees (baseline → new). Do NOT invent numbers "
        "beyond the provided params/divergences.\n"
        f"Course context: corridor verdict = {corridor.get('verdict')} ({corridor.get('note')}); "
        f"point-of-sail profile = {profile}.\n"
        "Return STRICT JSON only: an object keyed by play id, each value "
        "{summary, narrative, rationale, tradeoffs, what_flips_it}."
    )
    last = None
    for attempt in range(2):        # the variant-synthesis call runs just before this one — a
        try:                        # transient rate-limit here must not silently gut the plays
            txt, used_model = lab_llm.complete(system, f"Race: {race_name}\nPlays:\n" +
                                               json.dumps(facts, indent=1), max_tokens=12000)
            if txt.startswith("```"):
                txt = txt.split("```", 2)[1].lstrip("json").strip() if "```" in txt[3:] else txt.strip("`")
            data = json.loads(txt)
            if isinstance(data, dict):
                return data, None
            last = "non-object JSON"
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(4)
    return {}, f"play narratives fell back to registry seeds ({last})"


def _sail_speed(SP, sail, tws, twa):
    """Nearest per-sail-polar speed for `sail` at (tws, twa), or None (jib change-downs share the
    J1 curve, so J2/J3 read as J1)."""
    pts = SP.get(sail) or SP.get({"J2": "J1", "J3": "J1", "C0": "J1"}.get(sail, "")) or []
    best, bd = None, 1e9
    for t, a, v in pts:
        d = (t - tws) ** 2 + ((a - twa) / 10.0) ** 2
        if d < bd:
            best, bd = v, d
    return best


# legs carry beat/reach/run (optimizer._point_of_sail); the older upwind/reaching/downwind
# aliases are kept so nothing regresses if a caller uses the other vocabulary
_POS_TWA = {"beat": 45.0, "upwind": 45.0, "reach": 100.0, "reaching": 100.0,
            "run": 150.0, "downwind": 150.0}


def _sail_guidance_plays(consensus, jib_crossovers, sail_config=None):
    """Phase-C SAIL GUIDANCE plays (no route — a pre-authored call): for each nominal leg's sail,
    where is the crossover if the breeze runs above/below forecast, and what does the change buy?
    Grounded in the frozen boat model (crossovers + per-sail polars) — never invented."""
    from . import polars as POLm
    from . import sailplan
    SP = POLm.sail_polars()
    seen, out = set(), []
    for i, leg in enumerate((consensus or {}).get("legs") or []):
        sail = leg.get("sail")
        w = ((leg.get("wind") or {}).get("tws"))
        if not sail or not isinstance(w, (int, float)):
            continue
        twa = _POS_TWA.get(leg.get("point_of_sail") or "reaching", 100.0)
        for step, direction in ((1, "over"), (-1, "under")):
            s2, thr = None, None
            for dw in range(1, 8):          # scan for the crossover boundary from the leg's TWS
                w2 = w + step * dw
                if w2 < 3:
                    break
                cand = sailplan.optimal_sail(w2, twa, jib_crossovers, sail_config)
                if cand and cand != sail:
                    s2, thr = cand, round(w2, 1)
                    break
            if not s2:
                continue
            key = (sail, s2, direction)
            if key in seen:
                continue
            seen.add(key)
            v_new = _sail_speed(SP, s2, thr + 1, twa)
            v_old = _sail_speed(SP, sail, thr + 1, twa)
            rec = round(v_new - v_old, 2) if (v_new and v_old) else None
            gain = (f" — worth ~{rec} kn while it lasts" if rec and rec > 0.05 else "")
            verb = "builds" if direction == "over" else "drops"
            out.append({
                "id": f"sail_{direction}_{sail.lower()}_{s2.lower()}",
                "name": f"{sail} past its crossover → {s2}",
                "kind": "sail_guidance", "category": "internal",
                "params": {"hoisted": sail, "change_to": s2, "tws_threshold": thr,
                           "direction": direction, "leg": i},
                "narrative_seed": (f"Flying the {sail} while the breeze {verb} through ~{thr} kn — "
                                   f"past the frozen crossover, the {s2} is the faster sail"
                                   + (" and holding on means giving speed away" if direction == "over"
                                      else "")),
                "divergence": {"delta_eta_min": 0, "xte_mean_nm": None},
                "guidance": (f"Change to the {s2} once ~{thr} kn is sustained{gain}. "
                             "From the frozen boat model — confirm against the crew's own bands."),
                "favored_side": None,
            })
    return out


def _reef_guidance_plays(consensus, sail_config):
    """Phase-C-style GUIDANCE plays for the MAIN REEF points (crew thresholds, not in the cert):
    when a nominal leg's forecast TWS sits within scan range of a reef threshold, pre-author the
    call — 'tuck in reef 1 through ~N kn' (depower) and, running the A3, the lower-threshold
    'reef 1 to open the slot between the A3 and the main'. No route — the reef changes the CALL,
    never the rated speed."""
    from . import sailplan
    mr = (sail_config or {}).get("main_reefs") or {}
    if not mr:
        return []
    out, seen = [], set()
    for i, leg in enumerate((consensus or {}).get("legs") or []):
        w = (leg.get("wind") or {}).get("tws")
        sail = leg.get("sail")
        if not isinstance(w, (int, float)):
            continue
        for kind, thr, cond in (
                ("depower", mr.get("r1_tws_kn"), True),
                ("a3_slot", mr.get("r1_a3_slot_tws_kn"), sail == "A3")):
            if thr is None or not cond or kind in seen:
                continue
            if not (w - 2 <= float(thr) <= w + 8):     # threshold within reach of this leg's breeze
                continue
            seen.add(kind)
            if kind == "depower":
                gid, name = "reef_r1_depower", "Reef 1 — depower in the breeze"
                narrative = (f"The breeze builds through ~{thr:g} kn and the boat is overpowered — "
                             "heel climbing, helm loading up, main traveller buried.")
                guidance = (f"Tuck in reef 1 once ~{thr:g} kn is sustained — depower before the "
                            "helm does it for you. Shake it out when the breeze drops back below. "
                            "From the boat's own reef points — confirm against the crew's feel.")
            else:
                gid, name = "reef_r1_a3_slot", "Reef 1 with the A3 — open the slot"
                narrative = (f"Running the A3 in ~{thr:g}+ kn — the main is blanketing the kite "
                             "and the slot between them has closed.")
                guidance = (f"With the A3 up and ~{thr:g}+ kn, tuck in reef 1 to OPEN the slot "
                            "between the kite and the main — the A3 breathes and the boat "
                            "stands up. From the boat's own reef points.")
            out.append({
                "id": gid, "name": name, "kind": "sail_guidance", "category": "internal",
                "params": {"reef": "R1", "tws_threshold": float(thr), "direction": "over",
                           "context": kind, "leg": i,
                           **({"hoisted": "A3"} if kind == "a3_slot" else {})},
                "narrative_seed": narrative,
                "divergence": {"delta_eta_min": 0, "xte_mean_nm": None},
                "guidance": guidance, "favored_side": None,
            })
    return out


def _build_plays(playbook, definition, course_id, venue_stats=None, jib_crossovers=None,
                 sail_config=None):
    """The v2 plays: the external scenario fan + the internal fan (pace/gear-loss routes) + the
    sail GUIDANCE plays; registry predicates resolved against the route context + the venue's
    fleet-normal stats (percentile-framed, locked input #3); frontier narratives with the
    registry seeds as fallback."""
    from . import scenarios as scen
    v2 = playbook.get("v2") or {}
    entries = list(v2.get("scenario_routes") or [])
    entries += _sail_guidance_plays(playbook.get("consensus"), jib_crossovers, sail_config)
    entries += _reef_guidance_plays(playbook.get("consensus"), sail_config)
    if not entries:
        return [], v2, None
    ctx = {"mean_tws": _mean_tws(playbook.get("consensus")), "venue_stats": venue_stats}
    reg = {s["id"]: s for s in scen.EXTERNAL}
    texts, synth_note = _play_synthesis(entries, v2.get("corridor") or {},
                                        v2.get("pos_profile") or {}, definition.get("name", ""))
    plays = []
    for p in entries:
        t = texts.get(p["id"]) or {}
        stakes = abs((p.get("divergence") or {}).get("delta_eta_min") or 0)
        category = p.get("category") or "external"
        if category == "external":
            s = reg.get(p["id"]) or {}
            preds = (s.get("detect")(next(iter(p["params"].values())), ctx)
                     if s.get("detect") else [])
        else:
            fn = scen.INTERNAL_DETECT.get(p.get("kind"))
            preds = fn(p.get("params") or {}, ctx) if fn else []
        route = dict(p.get("route") or {}) or None
        if route:
            route["path"] = _downsample(route.get("path"))
        guidance = p.get("guidance")
        applic = ({"legs": [p["params"]["mark"]], "phase": "any"} if p.get("kind") == "pace"
                  else {"legs": [p["params"]["leg"]], "phase": "any"}
                  if p.get("kind") == "sail_guidance" and "leg" in (p.get("params") or {})
                  else {"legs": "all", "phase": "any"})
        plays.append({
            "id": p["id"], "name": p["name"], "category": category,
            "scenario": {"kind": p["kind"], "params": p["params"],
                         "source": "synthetic" if category == "external" else "internal"},
            "conditions": {
                "predicates": preds,
                "narrative": (t.get("narrative") or p.get("narrative_seed") or "").strip(),
            },
            "applicability": applic,
            "response": {"type": "route" if route else "guidance", "route": route,
                         "guidance": guidance,
                         "sail_plan": (route or {}).get("sail_plan")},
            "summary": (t.get("summary")
                        or (guidance if guidance else f"{p['name']} — the pre-routed answer.")).strip(),
            "rationale": (t.get("rationale") or "").strip(),
            "tradeoffs": (t.get("tradeoffs") or "").strip(),
            "what_flips_it": (t.get("what_flips_it")
                              or "the departure reverses / settles back to the frozen forecast").strip(),
            "stakes_min": stakes,
            "favored_side": p.get("favored_side"),
            **({"table": p["table"]} if p.get("table") else {}),   # rejoin-vs-continue rows
        })
    plays.sort(key=lambda x: (0 if x["category"] == "internal" else 1, -(x["stakes_min"] or 0)))
    return plays, v2, synth_note


def synthesize(definition, course_id, start_epoch, models, ensemble_members=0, time_budget_s=200,
               jib_crossovers=None, sail_config=None, helm_factor=1.0, use_waves=True,
               polar_adjustments=None, wave_coeffs=None):
    """Lab-2a fan-out → Lab-2b synthesized bundle (UNSIGNED draft). Freeze (`sign_bundle`) before
    it's relied on / deployed onboard. Passes through the not-available case from 2a unchanged."""
    playbook = pb.build_playbook(definition, course_id, start_epoch, models,
                                 ensemble_members=ensemble_members, time_budget_s=time_budget_s,
                                 jib_crossovers=jib_crossovers, sail_config=sail_config,
                                 helm_factor=helm_factor, use_waves=use_waves,
                                 polar_adjustments=polar_adjustments, wave_coeffs=wave_coeffs)
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

    # ---- Playbook v2: the play library from the scenario fan (docs/PLAYBOOK_V2.md) --------------
    venue_stats = _venue_stats()
    plays, v2meta, play_note = _build_plays(playbook, definition, course_id,
                                            venue_stats=venue_stats, jib_crossovers=jib_crossovers,
                                            sail_config=sail_config)
    corridor = (v2meta.get("corridor") or {}) if v2meta else {}
    headline = syn.get("headline", "")
    if corridor.get("note"):
        headline = (headline + " — " if headline else "") + corridor["note"].capitalize() + "."

    bundle = {
        "schema": SCHEMA_V2 if plays or v2meta else SCHEMA,
        "race_id": definition.get("race_id"),
        "race_name": definition.get("name"),
        "course_id": playbook.get("course_id") or course_id,
        "start_epoch": playbook.get("start_epoch"),
        "generated_at": round(time.time()),
        "headline": headline,
        "nominal": {
            "favored_side": (playbook.get("consensus") or {}).get("favored_side"),
            "total_hours": (playbook.get("consensus") or {}).get("total_hours"),
            "robustness": (v2meta or {}).get("robustness") or [],
        },
        "plays": plays,
        "corridor": corridor or None,
        "pos_profile": (v2meta or {}).get("pos_profile"),
        "venue_stats": venue_stats,
        "buoys": _buoy_stations(definition, course_id),   # up-course leading-indicator stations
        "recommended": syn.get("recommended"),
        "agreement": playbook.get("agreement"),
        "decision_spread_min": playbook.get("decision_spread_min"),
        "first_beat_rhumb_deg": first_beat_rhumb(definition, course_id),
        "consensus": playbook.get("consensus"),
        "boat_model": _boat_model(),      # polars/sail crossovers + draft frozen into the homework
        # the common forecast the plan was built on → the onboard forecast-drift branch trigger
        "forecast_fingerprint": _fingerprint(variants, syn.get("recommended")),
        # compact race obstacles (island disks + zones) → the onboard re-optimizer avoids land
        "obstacles": race_def.course_obstacles(definition, playbook.get("course_id") or course_id),
        "variants": variants,
        "decision_tree": syn.get("decision_tree", []),
        "provenance": {
            "models": [m["model"] for m in playbook.get("windfield", {}).get("models", [])],
            "n_scenarios": playbook.get("n_scenarios"),
            "n_variants": playbook.get("n_variants"),
            "synth_model": syn.get("synth_model"),
            "play_synthesis_note": play_note,     # non-None = narratives fell back to seeds
            "scenarios": playbook.get("scenarios"),
        },
        "windfield": playbook.get("windfield"),
        "skipped_marks": playbook.get("skipped_marks", []),
        "signature": None,            # unsigned draft until frozen
    }
    return bundle
