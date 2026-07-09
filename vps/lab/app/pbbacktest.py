"""Known-answer playbook backtest (docs/PLAYBOOK_V2.md §8) — the lab-container half.

The question: synthesize the playbook EXACTLY as it would have existed at the 2025 gun
(pinned archive GRIBs, nothing the boat couldn't have known), then replay the realized race
through the onboard selector/matcher — does the pipeline end up pointing at the side we KNOW
paid (2025: right, 18:2 in the Div-I top third)?

Two stages live here (they need cfgrib + the retro store); the decision replay itself runs on
the host against the real vps/agent selector/matcher code:

  1. `synth_asof()`   — as-of-gun v2 bundle. Same seam the retro study uses
     (`archive.gun_sources` instances pass straight through `build_windfield`); the ONE
     backtest-specific correction is the forecast fingerprint — `synthesis._fingerprint`
     samples LIVE Open-Meteo, which cannot serve 2025, so it is rebuilt from the same frozen
     gun blend and the bundle re-signed.
  2. `sample_replay()` — realized wind along real YB tracks: an hourly HRRR f00/analysis
     chain (the judge-loop's oracle definition) sampled at each boat's interpolated position
     per step, plus the frozen fingerprint interpolated to the same moment → one JSON the
     host-side replay consumes.

Run via docker cp + exec (the lab image bakes its source):
  docker cp vps/lab/app/pbbacktest.py sr33-dev-lab-1:/srv/app/pbbacktest.py
  docker exec sr33-dev-lab-1 python3 -c "from app import pbbacktest; pbbacktest.run()"
"""
import datetime as dt
import json
import math
import os

from . import retrostore, store, synthesis
from .wind import archive, grib
from .wind.windfield import WindField, build_windfield
from . import optimizer

OUT_DIR = os.path.join(os.path.dirname(retrostore.RETRO_DB), "backtest")

DEF_RACE = "bayview-mackinac-2026"        # course geometry (same course sailed in 2025)
RETRO_RACE = "bayviewmack2025"
COURSE = "cove_island"
GUN = 1752338400.0                        # C4's division gun (Div I Class E, 2025-07-12)


def _out(name):
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, name)


# ------------------------------------------------------------------ Phase A: as-of synthesis
def _archive_fingerprint(bundle, definition, course_id, gun, models):
    """The drift reference the 2025 boat would have frozen: the SAME gun blend sampled along
    the recommended variant's route (source-labeled so nobody mistakes it for Open-Meteo)."""
    rec = bundle.get("recommended")
    v = next((x for x in (bundle.get("variants") or []) if x.get("id") == rec), None)
    path = ((v or {}).get("route") or {}).get("path") or []
    pts = [p for p in path if p.get("lat") is not None and p.get("t") is not None]
    if len(pts) < 2:
        return None
    bbox = optimizer.course_bbox(definition, course_id)
    hours = optimizer.estimate_hours(definition, course_id)
    wf = build_windfield(bbox, gun, gun + hours * 3600, models=models)   # GRIBs cached → fast
    step = max(1, len(pts) // 12)
    out = []
    for i in range(0, len(pts), step):
        p = pts[min(i, len(pts) - 1)]
        w = wf.wind_at(p["lat"], p["lon"], p["t"])
        if w:
            out.append({"lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
                        "t": round(p["t"]), "tws": round(w[0], 1), "twd": round(w[1])})
    if len(out) < 2:
        return None
    return {"source": "archive-gun-blend", "built_at": round(gun), "points": out}


def synth_asof(def_race_id=DEF_RACE, retro_race_id=RETRO_RACE, course_id=COURSE,
               gun=GUN, fan_depth="standard", use_waves=False):
    """The playbook as-of the 2025 gun. Returns the signed bundle (also written to disk).
    race_id is suffixed -backtest so nothing here can ever land in a real race's deploy list."""
    d = dict(store.get_race(def_race_id) or {})
    if not d:
        return {"available": False, "note": f"unknown definition {def_race_id!r}"}
    d["race_id"] = f"{retro_race_id}-backtest"
    d["name"] = f"{d.get('name', def_race_id)} (2025 backtest)"
    models = archive.gun_sources(gun, context=f"retro:{retro_race_id}")
    bundle = synthesis.synthesize(d, course_id, gun, models,
                                  ensemble_members=0, use_waves=use_waves, fan_depth=fan_depth)
    if not bundle.get("available", True):
        return bundle
    fp = None
    try:
        fp = _archive_fingerprint(bundle, d, course_id, gun, models)
    except Exception as e:
        print(f"[backtest] fingerprint rebuild failed: {e}", flush=True)
    if fp:
        bundle["forecast_fingerprint"] = fp
    else:
        bundle["forecast_fingerprint"] = None     # never ship a live-2026 fingerprint here
    synthesis.sign_bundle(bundle, signed_by="pbbacktest")
    with open(_out("bundle.json"), "w") as f:
        json.dump(bundle, f)
    print(f"[backtest] bundle: recommended={bundle.get('recommended')!r} "
          f"agreement={bundle.get('agreement')} plays={len(bundle.get('plays') or [])} "
          f"fingerprint={'archive' if fp else 'NONE'}", flush=True)
    return bundle


# ------------------------------------------------------------- realized wind (oracle chain)
def build_oracle_field(bbox, t0, t1, retro_race_id=RETRO_RACE):
    """The wind that actually blew: hourly HRRR f00 (analysis) frames chained over the race
    window — the judge-loop's oracle definition, via the same pinned-archive fetch path."""
    src = archive.ArchiveHRRR()
    src.asof = t1 + 7200                      # cycle picks are explicit below; asof just must not veto
    src.pin_context = f"retro:{retro_race_id}-oracle"
    parser = grib.IsolatedGribParser() if getattr(grib, "ISOLATE", False) else None
    frames = []
    try:
        h = math.floor(t0 / 3600.0) * 3600.0
        while h <= t1 + 3600.0:
            cycle = dt.datetime.fromtimestamp(h, tz=dt.timezone.utc)
            try:
                path = src.fetch(cycle, 0, "det", bbox)
                if path:
                    frames.append(grib.GribFrame.from_file(
                        path, "hrrr", "det", h, parser=parser).crop(bbox))
            except Exception as e:
                print(f"[backtest] oracle frame {cycle:%Y-%m-%d %HZ} skipped: {e}", flush=True)
            h += 3600.0
    finally:
        if parser is not None:
            parser.close()
    frames.sort(key=lambda fr: fr.valid_time)
    series = {("hrrr", "det"): frames} if frames else {}
    meta = [{"model": "hrrr", "cycle": "analysis-chain", "members": 1,
             "frames": len(frames), "expected_frames": int((t1 - t0) / 3600) + 2,
             "cycle_fallbacks": 0, "priority": 1, "kind": "deterministic"}]
    print(f"[backtest] oracle field: {len(frames)} hourly analysis frames", flush=True)
    return WindField(series, meta, bbox, t0, t1)


# ------------------------------------------------------------- Phase B stage 1: sampling
def _interp_track(fixes, t):
    """Linear position interp on a time-ascending fix list; None outside its span."""
    if not fixes or t < fixes[0]["t"] or t > fixes[-1]["t"]:
        return None
    lo, hi = 0, len(fixes) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fixes[mid]["t"] <= t:
            lo = mid
        else:
            hi = mid
    a, b = fixes[lo], fixes[hi]
    span = (b["t"] - a["t"]) or 1.0
    f = (t - a["t"]) / span
    return {"lat": a["lat"] + f * (b["lat"] - a["lat"]),
            "lon": a["lon"] + f * (b["lon"] - a["lon"]),
            "cog": b.get("cog"), "sog": b.get("sog")}


def _fp_at(fp_points, t):
    """The frozen fingerprint interpolated to time t (points are route-ordered with ETAs)."""
    pts = fp_points or []
    if not pts:
        return None
    if t <= pts[0]["t"]:
        return pts[0]
    if t >= pts[-1]["t"]:
        return pts[-1]
    for a, b in zip(pts, pts[1:]):
        if a["t"] <= t <= b["t"]:
            span = (b["t"] - a["t"]) or 1.0
            f = (t - a["t"]) / span
            # interpolate direction the short way round
            dd = ((b["twd"] - a["twd"] + 540) % 360) - 180
            return {"tws": a["tws"] + f * (b["tws"] - a["tws"]),
                    "twd": (a["twd"] + f * dd) % 360}
    return None


def sample_replay(bundle=None, team_ids=(3168, 1676), step_s=900, retro_race_id=RETRO_RACE,
                  def_race_id=DEF_RACE, course_id=COURSE, hours_cap=48.0):
    """Stage-1 sampling → backtest/replay_input.json for the host-side selector replay.
    Per boat per step: interpolated position + realized (oracle) wind there + the frozen
    fingerprint's expectation for that moment."""
    if bundle is None:
        with open(_out("bundle.json")) as f:
            bundle = json.load(f)
    d = dict(store.get_race(def_race_id) or {})
    bbox = optimizer.course_bbox(d, course_id)
    entries = {e["team_id"]: e for e in retrostore.get_entries(retro_race_id)}
    results = {r["team_id"]: r for r in retrostore.get_results(retro_race_id)
               if r.get("division") == "Division I Overall"}
    fp_points = (bundle.get("forecast_fingerprint") or {}).get("points") or []

    guns = [entries[t]["start_epoch"] for t in team_ids if t in entries]
    t0 = min(guns) if guns else GUN
    t1 = t0 + hours_cap * 3600
    wf = build_oracle_field(bbox, t0 - 3600, t1, retro_race_id)
    if not wf.loaded:
        return {"ok": False, "note": "oracle field empty — no analysis GRIBs reachable"}

    boats = {}
    for tid in team_ids:
        e = entries.get(tid)
        fixes = retrostore.get_track(retro_race_id, tid)
        if not e or not fixes:
            print(f"[backtest] team {tid}: no entry/track — skipped", flush=True)
            continue
        gun = e["start_epoch"] or t0
        steps = []
        t = gun
        end = min(fixes[-1]["t"], e.get("finished_at") or 9e18, gun + hours_cap * 3600)
        while t <= end:
            pos = _interp_track(fixes, t)
            if pos:
                w = wf.wind_at(pos["lat"], pos["lon"], t)
                fpx = _fp_at(fp_points, t)
                if w:
                    steps.append({"t": round(t), "lat": round(pos["lat"], 5),
                                  "lon": round(pos["lon"], 5), "cog": pos.get("cog"),
                                  "sog": pos.get("sog"),
                                  "tws": round(w[0], 1), "twd": round(w[1], 1),
                                  "fp_tws": round(fpx["tws"], 1) if fpx else None,
                                  "fp_twd": round(fpx["twd"], 1) if fpx else None})
            t += step_s
        boats[str(tid)] = {"boat": e["boat"], "division_gun": gun,
                           "rank_division": (results.get(tid) or {}).get("rank_division"),
                           "steps": steps}
        print(f"[backtest] {e['boat']}: {len(steps)} steps sampled", flush=True)

    rec = bundle.get("recommended")
    rec_v = next((v for v in (bundle.get("variants") or []) if v.get("id") == rec), None)
    out = {
        "ok": True,
        "race_id": retro_race_id,
        "gun": GUN,
        "bundle": {
            "race_id": bundle.get("race_id"),
            "recommended": rec,
            "agreement": bundle.get("agreement"),
            "headline": bundle.get("headline"),
            "decision_spread_min": bundle.get("decision_spread_min"),
            "corridor": bundle.get("corridor"),
            "venue_stats": bundle.get("venue_stats"),
            "variants": [{"id": v.get("id"), "name": v.get("name"),
                          "what_flips_it": v.get("what_flips_it")}
                         for v in (bundle.get("variants") or [])],
            "plays": bundle.get("plays") or [],
            "recommended_path": [{"lat": p["lat"], "lon": p["lon"], "t": p.get("t")}
                                 for p in ((rec_v or {}).get("route") or {}).get("path") or []],
        },
        "boats": boats,
    }
    with open(_out("replay_input.json"), "w") as f:
        json.dump(out, f)
    print(f"[backtest] replay_input.json written ({len(boats)} boats)", flush=True)
    return {"ok": True, "boats": {k: len(v['steps']) for k, v in boats.items()}}


def run(fan_depth="standard", team_ids=(3168, 1676)):
    """Phase A + stage-1 sampling in one go (the ~10-min path is the synthesis fan)."""
    bundle = synth_asof(fan_depth=fan_depth)
    if not bundle.get("available", True):
        return bundle
    return sample_replay(bundle, team_ids=team_ids)
