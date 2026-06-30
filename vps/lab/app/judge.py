"""Debrief — the Lab-4 post-race judge loop.

After a race the optimizer becomes an ORACLE: re-route the course on the wind that actually blew
(best-available analysis / latest GRIB over the race window) → the hindsight-optimal route. Compare
it to the frozen PLAYBOOK we carried (the plan made on the pre-race forecast) to measure REGRET — the
cost of the plan vs perfect foresight — and which side of the first beat actually paid vs what we
recommended. Opus then critiques the plan and proposes write-back: a Learnings bullet for the next
regatta + an onboard-brain adjustment + a boat-model note. The write-back is human-reviewed before it
lands (apply promotes the Learnings note onto the race).

Scoring the boat's ACTUAL track (helm execution vs the optimal) is the next enrichment — a slot is
reserved in the report (`actual_track`). Today the judge runs the oracle-vs-plan comparison + critique,
which is the heart of the loop and fully runnable from the frozen playbook + live GRIB.
"""
import json
import math
import os
import time

from shared import race_def

from . import store, pbstore, optimizer, track, learning, boats

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")

_R_NM = 3440.065


def _dist_nm(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _R_NM * math.asin(min(1.0, math.sqrt(h)))


def _bearing(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _xtrack_nm(start, mark, p):
    """Signed cross-track distance (nm) of p from the start→mark line. +right of rhumb / -left."""
    d13 = _dist_nm(start, p) / _R_NM
    b13 = math.radians(_bearing(start, p))
    b12 = math.radians(_bearing(start, mark))
    return math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(b13 - b12)))) * _R_NM


def _first_beat_side(path, start, first_mark, band_nm=0.4):
    """Which side of the first-beat rhumb the route worked: left | middle | right."""
    if not path or not first_mark:
        return "middle"
    beat = []
    for p in path:
        pp = (p["lat"], p["lon"])
        beat.append(pp)
        if _dist_nm(pp, first_mark) < 0.5:
            break
    if len(beat) < 2:
        return "middle"
    xs = [_xtrack_nm(start, first_mark, pp) for pp in beat]
    ext = max(xs, key=abs)
    if abs(ext) < band_nm:
        return "middle"
    return "right" if ext > 0 else "left"


def _playbook_predicted_hours(pb):
    """The hours the carried plan expected — the recommended variant's, else the consensus/min."""
    variants = pb.get("variants") or []
    rec = pb.get("recommended")
    for v in variants:
        if v.get("id") == rec or v.get("side") == rec:
            if v.get("total_hours"):
                return v["total_hours"]
    cons = (pb.get("consensus") or {}).get("total_hours")
    if cons:
        return cons
    hrs = [v["total_hours"] for v in variants if v.get("total_hours")]
    return min(hrs) if hrs else None


def run_judge(race_id, playbook_id=None, models=None, on_progress=None):
    """Oracle re-route on the actual/latest wind vs the frozen playbook → regret + Opus critique."""
    log = on_progress or (lambda *_: None)
    d = store.get_race(race_id)
    if not d:
        return {"available": False, "note": "unknown race"}
    pbs = [b for b in pbstore.list_bundles() if b.get("race_id") == race_id]
    pid = playbook_id or (pbs[0]["id"] if pbs else None)
    pb = pbstore.get(pid) if pid else None
    if not pb:
        return {"available": False, "note": "no frozen playbook to judge — freeze one in Gameplan first",
                "playbooks": pbs}
    course_id = pb.get("course_id")
    start = float(pb.get("start_epoch") or 0)
    if not start:
        return {"available": False, "note": "playbook has no start_epoch"}

    from .wind import build_windfield
    bbox = optimizer.course_bbox(d, course_id)
    if not bbox:
        return {"available": False, "note": "course has no geocoded marks"}
    hours = optimizer.estimate_hours(d, course_id)
    t_end = start + hours * 3600
    log("building the actual-wind field (oracle)…")
    pv_models = (pb.get("provenance") or {}).get("models") or None
    wf = build_windfield(bbox, start, t_end, models=(models or pv_models or None), on_progress=log) \
        if (models or pv_models) else build_windfield(bbox, start, t_end, on_progress=log)
    if not wf.loaded:
        return {"available": False, "note": "no wind data for the race window (analysis GRIB unavailable "
                "for a past race, or no egress) — the oracle needs the wind that actually blew",
                "windfield": wf.status(), "log": []}
    log("routing the hindsight-optimal (oracle) course…")
    oracle = optimizer.optimize_course(d, course_id, start, wf, avoid=True)
    if not oracle.get("available", True) and oracle.get("note"):
        return {"available": False, "note": "oracle route failed: " + oracle["note"]}

    marks, _, _ = race_def.course_to_marks(d, course_id)
    start_pt = (marks[0][2], marks[0][3]) if marks else None
    first_mark = (marks[1][2], marks[1][3]) if len(marks) > 1 else None
    oracle_path = oracle.get("path") or []
    side_paid = _first_beat_side(oracle_path, start_pt, first_mark)

    predicted = _playbook_predicted_hours(pb)
    oracle_hours = oracle.get("total_hours")
    regret_h = (predicted - oracle_hours) if (predicted and oracle_hours) else None
    rec_side = pb.get("recommended")
    variants = [{"side": v.get("id") or v.get("side"), "total_hours": v.get("total_hours"),
                 "share": v.get("share")} for v in (pb.get("variants") or [])]
    winning = next((v for v in variants if v["side"] == side_paid), None)

    report = {
        "available": True, "race_id": race_id, "race_name": d.get("name"),
        "playbook_id": pid, "course_id": course_id, "start_epoch": start,
        "oracle": {"total_hours": oracle_hours, "favored_side": side_paid,
                   "route_confidence": oracle.get("route_confidence"), "path": oracle_path,
                   "tacks": oracle.get("tacks")},
        "playbook": {"recommended": rec_side, "predicted_hours": predicted, "variants": variants,
                     "headline": pb.get("headline"), "agreement": pb.get("agreement")},
        "regret": {"hours": regret_h, "minutes": (round(regret_h * 60) if regret_h is not None else None),
                   "side_paid": side_paid, "recommended_side": rec_side,
                   "side_matched": (rec_side == side_paid),
                   "winning_variant": winning},
        "windfield": wf.status(),
        "actual_track": _score_actual_track(race_id, oracle, marks, start, wf),
        "caveat": "Oracle wind is the best-available GRIB over the race window; a true post-race judge "
                  "uses reanalysis/analysis fields. For a future/near race this is forecast-grade, so "
                  "regret reflects forecast drift, not full hindsight.",
    }
    log("writing the critique…")
    report["critique"] = _critique(report) or _deterministic_critique(report)
    try:                                   # archive to the ongoing learning DB (best-effort)
        bid = (boats.active_boat() or {}).get("boat_id")
        report["archived_id"] = learning.archive_debrief(report, bid)
    except Exception:
        pass
    return report


def _score_actual_track(race_id, oracle, marks, start_epoch, wf):
    """Score the boat's stored ACTUAL track vs the oracle line (helm execution), if one is uploaded/
    fetched. Returns the actual_track block for the report; a no-track default keeps the slot honest."""
    t = track.load_track(race_id)
    if not t or not t.get("fixes"):
        return {"available": False,
                "note": "no boat track for this race — upload a GPX or fetch our YB track below"}
    try:
        from . import polars as POL
        return track.score_track(t, oracle, marks, start_epoch, wf=wf, polars=POL.polars_stw())
    except Exception as e:
        return {"available": False, "note": f"track scoring failed: {type(e).__name__}"}


def _deterministic_critique(r):
    reg = r["regret"]
    matched = reg["side_matched"]
    side_paid, rec = reg["side_paid"], reg["recommended_side"]
    mins = reg["minutes"]
    if matched:
        assess = (f"The plan's recommended side ({rec}) is the side the wind ended up favoring. "
                  "The pre-race read held up.")
        lesson = f"Recommended {rec} and {rec} paid — the high-agreement call was right; keep that read."
    else:
        assess = (f"The plan recommended {rec} but the wind favored {side_paid}. The pre-race forecast "
                  "pointed the other way" + (f" — about {abs(mins)} min of regret vs perfect foresight." if mins is not None else "."))
        lesson = (f"{side_paid} paid, not the recommended {rec}. Watch the first-beat trigger more "
                  "aggressively next time; the branch to {side_paid} should have fired.")
    at = r.get("actual_track") or {}
    note = ""
    if at.get("available"):
        bits = []
        if at.get("time_behind_optimal_min") is not None:
            bits.append(f"{at['time_behind_optimal_min']} min behind the oracle line")
        if at.get("extra_distance_pct") is not None:
            bits.append(f"{at['extra_distance_pct']}% oversail")
        if at.get("polar_pct") is not None:
            bits.append(f"{at['polar_pct']}% of polar")
        if at.get("side_worked"):
            sw = at["side_worked"]
            bits.append(f"worked the {sw} side" + (" (= the side that paid)" if sw == side_paid else f", paid was {side_paid}"))
        if bits:
            note = " Boat track: " + ", ".join(bits) + "."
        assess += note
    return {"assessment": assess, "key_lesson": lesson,
            "proposed_learnings": f"[{r['race_name']}] First beat: {side_paid} paid"
                                  + (f" (recommended {rec})" if not matched else "")
                                  + (f"; regret ~{mins} min vs optimal." if mins is not None else ".")
                                  + (f" Sailed {at['extra_distance_pct']}% over optimal at {at.get('polar_pct','?')}% of polar."
                                     if at.get("available") and at.get("extra_distance_pct") is not None else ""),
            "brain_edit": ("Tighten the first-beat side trigger — bias toward switching when early "
                           f"pressure shows on the {side_paid} side.") if not matched else
                          "Keep the current first-beat read; it matched the outcome.",
            "boat_model_note": ("Helm achieved only %d%% of polar — consider lowering the boat's "
                                "helm_factor toward that, and review trim/steering in the debrief."
                                % at["polar_pct"]) if (at.get("available") and at.get("polar_pct") and at["polar_pct"] < 92) else "",
            "model": "deterministic"}


def _critique(r):
    if not API_KEY:
        return None
    at = r.get("actual_track") or {}
    facts = {
        "race": r["race_name"], "playbook": r["playbook"], "oracle": {
            "favored_side": r["oracle"]["favored_side"], "total_hours": r["oracle"]["total_hours"]},
        "regret": r["regret"], "caveat": r["caveat"],
    }
    if at.get("available"):
        facts["actual_track"] = {k: at.get(k) for k in (
            "source", "elapsed_hours", "time_behind_optimal_min", "sailed_nm", "optimal_nm",
            "extra_distance_pct", "xte_mean_nm", "xte_p90_nm", "xte_max_nm", "side_worked",
            "polar_pct", "polar_samples")}
    system = (
        "You are an expert yacht-racing coach running a POST-RACE DEBRIEF (the judge loop). You are "
        "given the frozen pre-race PLAYBOOK we carried (recommended first-beat side + variants) and the "
        "ORACLE result — the hindsight-optimal route on the wind that actually blew (which side paid, "
        "the optimal time) — plus the REGRET (plan vs perfect foresight). When ACTUAL_TRACK is present "
        "you also have the boat's REAL sailed track scored vs the oracle line: time_behind_optimal_min, "
        "extra_distance_pct (oversail), XTE off the optimal route, the first-beat side the boat actually "
        "WORKED (vs the side that paid / we recommended), and polar_pct (% of the flat-water polar the "
        "helm achieved). SEPARATE three causes: tactical (wrong side/strategy), helm execution (low "
        "polar_pct, high XTE, oversail = steering/trim/sail-handling), and conditions/luck. The boat "
        "NEVER sails the optimal line exactly — these are coaching deltas, not pass/fail; judge PROCESS "
        "not just outcome (a good +EV call that didn't pay is still good). Be concrete, concise, no "
        "preamble. Return STRICT JSON only with keys: assessment (2-3 sentences on what the plan + helm "
        "got right/wrong), key_lesson (one sentence), proposed_learnings (one bullet to add to the "
        "boat's Learnings for the next regatta), brain_edit (a concrete adjustment to the onboard "
        "decision brain — how to weight the first-beat side trigger next time), boat_model_note (any "
        "polar/crossover/helm_factor refinement the track suggests, else empty string).")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=API_KEY)
        resp = client.messages.create(model=MODEL, max_tokens=1200, system=system,
                                       messages=[{"role": "user", "content": json.dumps(facts, indent=2)}])
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            txt = txt[4:].strip() if txt.lower().startswith("json") else txt
        out = json.loads(txt)
        out["model"] = MODEL
        return out
    except Exception:
        return None


def apply_writeback(race_id, learnings_text):
    """Human-reviewed write-back v1: promote the debrief's Learnings bullet onto the race's
    learnings_notes (carried into the next prep). Deeper write-back (onboard_brain / polars) is staged
    in the report for review."""
    d = store.get_race(race_id)
    if not d:
        return {"saved": False, "note": "unknown race"}
    note = (learnings_text or "").strip()
    if not note:
        return {"saved": False, "note": "nothing to write"}
    existing = (d.get("learnings_notes") or "").strip()
    d["learnings_notes"] = (existing + "\n" + note).strip() if existing else note
    import re
    rid = re.sub(r"[^a-z0-9_-]", "", str(d.get("race_id", "")).lower())
    ingested = os.environ.get("INGESTED_DIR", "/srv/ingested")
    os.makedirs(ingested, exist_ok=True)
    with open(os.path.join(ingested, f"{rid}.json"), "w") as f:
        json.dump(d, f, indent=2)
    return {"saved": True, "learnings_notes": d["learnings_notes"]}
