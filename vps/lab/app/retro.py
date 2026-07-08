"""Fleet retro study — ingest past races + run the optimizer for every boat (docs/RETRO_STUDY.md).

R1 (this module, ingest): YB RaceSetup (entries, start, TCFs, divisions) + the AllPositions3
full-fleet binary (decoder in `track.py`) + GetPositions (the ABSOLUTE-time anchor per team — the
binary carries only relative seconds) + the leaderboard (YB's own corrected results per division,
stored verbatim rather than recomputed) → the persistent `retrostore` archive.

R2 (also here): match every entry to its ORC cert (sail# → yacht-name, `fleetimport._orc_dump`) and
convert the cert's Allowances into an optimizer-shaped polar (`orcpolar.cert_polar`) — stored per
(race, team). Unmatched boats are reported, never silently dropped.

Block→team matching: AllPositions3 blocks come in teams[] order but DNS/sparse blocks may be
skipped, so identity is by DISTANCE — a team's GetPositions latest fix must sit within 1 nm of a
block's newest fix (the same self-validating link `fetch_yb_track` uses), greedily nearest-first.
"""
import json

from shared import race_def

from . import jobs
from . import fleetimport as fi
from . import orcpolar
from . import retrostore as rs
from . import track as track_mod

_HOST = "https://cf.yb.tl"


def _yb(path, raw=False):
    return track_mod._yb_get(f"{_HOST}/{path}", raw=raw)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ingest_race(race_id: str) -> dict:
    """Pull one YB race into the archive: race + entries + anchored tracks + results."""
    setup = _yb(f"JSON/{race_id}/RaceSetup")
    teams = setup.get("teams") or []
    if not teams:
        return {"ok": False, "note": "race has no teams (unpublished/dormant feed)"}
    tags = {t.get("id"): t for t in setup.get("tags") or []}
    rs.upsert_race(race_id, setup, setup.get("start"))

    for t in teams:
        division = json.dumps([tags[i]["name"] for i in (t.get("tags") or []) if i in tags])
        rs.upsert_entry(race_id, {**t, "division": division, "tcf": _f(t.get("tcf1"))})

    # --- tracks: decode all blocks, anchor + match by GetPositions latest fix -------------------
    binb = _yb(f"BIN/{race_id}/AllPositions3", raw=True)
    blocks = track_mod._decode_allpositions3(binb, len(teams))
    anchors = {}
    try:
        gp = _yb(f"API3/Race/{race_id}/GetPositions?t=0")
        for tm in gp.get("teams") or []:
            ps = tm.get("positions") or []
            if ps:
                p = max(ps, key=lambda x: x.get("gpsAtMillis") or 0)
                if p.get("gpsAtMillis"):
                    anchors[(tm.get("name") or "").lower()] = (
                        p["latitude"], p["longitude"], p["gpsAtMillis"] / 1000.0)
    except Exception:
        pass

    used, n_tracks, unanchored = set(), 0, []
    for t in teams:
        a = anchors.get((t.get("name") or "").lower())
        if not a:
            unanchored.append(t.get("name"))
            continue
        best, bestd = None, 1e9
        for bi, fx in enumerate(blocks):
            if bi in used or not fx:
                continue
            d = track_mod._hav_nm((fx[-1]["lat"], fx[-1]["lon"]), (a[0], a[1]))
            if d < bestd:
                best, bestd = bi, d
        if best is None or bestd > 1.0:
            unanchored.append(t.get("name"))
            continue
        used.add(best)
        fx = blocks[best]
        tmax = fx[-1]["t"]
        fixes = [{"lat": f["lat"], "lon": f["lon"], "t": a[2] - (tmax - f["t"]),
                  "sog": None, "cog": None} for f in fx]
        track_mod._derive_sog_cog(fixes)
        rs.save_track(race_id, t.get("id"), fixes)
        n_tracks += 1

    # --- results: the leaderboard verbatim (YB's own corrected order per division) --------------
    n_results, n_divisions = 0, 0
    try:
        lb = _yb(f"JSON/{race_id}/leaderboard")
        lb_tags = lb.get("tags") or []
        # tag identity: by id when present, else zip against the setup's lb-enabled tags in order
        ordered = [t for t in sorted((setup.get("tags") or []), key=lambda x: x.get("sort") or 0)
                   if t.get("lb")]
        for i, lt in enumerate(lb_tags):
            tag = tags.get(lt.get("id")) or (ordered[i] if i < len(ordered) else {})
            name = tag.get("name") or f"tag{lt.get('id') or i}"
            rows = lt.get("teams") or []
            if rows:
                n_divisions += 1
            for r in rows:
                rs.save_result(race_id, r.get("id"), name, _f(r.get("elapsed")),
                               _f(r.get("cElapsed")), _f(r.get("tcf")), r.get("rankR"),
                               bool(r.get("finished")), str(r.get("status") or ""))
                n_results += 1
    except Exception as exc:
        return {"ok": True, "race_id": race_id, "teams": len(teams), "blocks": len(blocks),
                "tracks": n_tracks, "results": 0, "divisions": 0,
                "note": f"leaderboard unavailable ({type(exc).__name__}) — results skipped",
                "unmatched_tracks": unanchored[:20]}

    return {"ok": True, "race_id": race_id, "teams": len(teams), "blocks": len(blocks),
            "tracks": n_tracks, "results": n_results, "divisions": n_divisions,
            "unmatched_tracks": unanchored[:20]}


_RESULT_HEAVY = ("isochrones", "laylines", "candidate_paths", "wind_grid", "current_grid",
                 "wave_grid", "obstacles", "log")

# ---- background fleet-run job (a full fleet is ~1-2h; the nginx gateway caps a request at 300s,
# so the batch runs via the shared jobs.py machinery and the UI/CLI polls status) ----------------

def start_fleet_job(race_id, def_race_id, course_id, teams, limit, resolution) -> dict:
    return jobs.start(
        "retro_fleet",
        lambda progress: run_fleet(race_id, def_race_id, course_id, teams, limit, resolution,
                                   on_progress=progress),
        meta={"race_id": race_id})


def fleet_job_status() -> dict:
    return jobs.status("retro_fleet")


def run_fleet(race_id: str, def_race_id: str = "bayview-mackinac-2026",
              course_id: str = "cove_island", teams=None, limit=None,
              resolution: str = "auto", on_progress=None) -> dict:
    """R4: optimize the course for EVERY archived boat on ITS ORC polar through the forecast that
    was knowable at ITS division's gun, then score its actual track against its own optimal route.

    One wind field is built per distinct division-start hour (staggered starts pick different HRRR
    cycles; the pinned GRIBs are shared). Results are slimmed of visualization-only arrays but keep
    path/legs — the archive is for analysis, not repaint. `teams`/`limit` scope a pilot subset."""
    from . import optimizer, store
    from .wind import build_windfield
    from .wind.archive import gun_sources

    say = on_progress or (lambda *_: None)
    d = store.get_race(def_race_id)
    if not d:
        return {"ok": False, "note": f"unknown lab race definition {def_race_id!r}"}
    race = rs.get_race(race_id)
    if not race:
        return {"ok": False, "note": "race not ingested yet"}
    marks, _skipped, _cid = race_def.course_to_marks(d, course_id)

    tracked = rs.teams_with_tracks(race_id)
    have_polar = {p["team_id"] for p in rs.get_polars(race_id)}
    want = {t.lower() for t in teams} if teams else None
    targets = [e for e in rs.get_entries(race_id)
               if e["team_id"] in have_polar and e["team_id"] in tracked
               and (want is None or (e["boat"] or "").lower() in want)]
    if limit:
        targets = targets[:int(limit)]
    if not targets:
        return {"ok": False, "note": "no boats with both a track and a matched polar in scope"}

    bbox = optimizer.course_bbox(d, course_id)
    fields = {}          # start-hour bucket -> WindField
    done, failed = [], []
    for e in targets:
        start = float(e.get("start_epoch") or race["start_epoch"])
        bucket = int(start // 3600)
        wf = fields.get(bucket)
        if wf is None:
            say(f"wind field for start {bucket * 3600} …")
            wf = build_windfield(bbox, start, start + 60 * 3600,
                                 models=gun_sources(start, context=f"retro:{race_id}"),
                                 on_progress=say)
            fields[bucket] = wf
        if not wf.loaded:
            failed.append({"boat": e["boat"], "note": "gun wind field failed to load"})
            continue

        P = rs.get_polar(race_id, e["team_id"])
        say(f"optimize {e['boat']} ({len(P)} polar pts, start +{round((start - race['start_epoch'])/60)}min)")
        try:
            result = optimizer.optimize_course(d, course_id, start, wf, polar=P, avoid=True,
                                               resolution=resolution, emit_exploration=False,
                                               per_model=False)
        except Exception as exc:
            failed.append({"boat": e["boat"], "note": f"optimize error: {exc}"})
            continue
        if not result.get("available", True) or not result.get("path"):
            failed.append({"boat": e["boat"], "note": result.get("note", "no route")})
            continue
        # COMPLETENESS GUARD: a budget-truncated isochrone can return a partial route (the pilot
        # caught a 124nm "optimal" on a 276nm course → 36nm XTE nonsense). A route that doesn't
        # reach the finish is stored for debugging but NEVER scored — scoring a real track against
        # half a route poisons the analysis.
        last = result["path"][-1]
        gap_nm = track_mod._hav_nm((last["lat"], last["lon"]), (marks[-1][2], marks[-1][3]))
        if gap_nm > 3.0:
            slim = {k: v for k, v in result.items() if k not in _RESULT_HEAVY}
            rs.save_run(race_id, e["team_id"], "gun_forecast_partial",
                        {"def_race_id": def_race_id, "course_id": course_id,
                         "start_epoch": start, "resolution": resolution,
                         "finish_gap_nm": round(gap_nm, 1)}, {}, slim)
            failed.append({"boat": e["boat"],
                           "note": f"route incomplete — ends {round(gap_nm)}nm short of the finish"})
            continue
        slim = {k: v for k, v in result.items() if k not in _RESULT_HEAVY}
        run_id = rs.save_run(race_id, e["team_id"], "gun_forecast",
                             {"def_race_id": def_race_id, "course_id": course_id,
                              "start_epoch": start, "resolution": resolution,
                              "polar_pts": len(P)},
                             {"models": [m for m in (result.get("wind_field") or {}).get("models", [])]},
                             slim)
        fixes = rs.get_track(race_id, e["team_id"])
        try:
            metrics = track_mod.score_track({"fixes": fixes}, result, marks, start, wf=wf, polars=P)
        except Exception as exc:
            metrics = {"available": False, "note": f"scoring error: {exc}"}
        rs.save_score(race_id, e["team_id"], run_id, metrics)
        done.append({"boat": e["boat"], "run_id": run_id,
                     "opt_hours": result.get("total_hours"),
                     "scored": bool(metrics.get("available"))})
    return {"ok": True, "race_id": race_id, "ran": len(done), "failed": failed[:20],
            "wind_fields": len(fields), "boats": done}


def _spearman(xs, ys):
    """Spearman rank correlation (tie-aware, pure stdlib). None when n<4 or degenerate."""
    n = len(xs)
    if n < 4:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(num / (dx * dy), 3) if dx and dy else None


def report(race_id: str) -> dict:
    """R5: adherence-vs-finish analysis. For every scored boat, how its distance from its OWN
    gun-forecast optimal route relates to its corrected rank — per division (≥5 scored finishers;
    rating-luck confound is why correlation runs WITHIN divisions) plus a pooled read on rank
    percentile. rho > 0 for behind_min/xte/extra-distance means sailing closer to the optimizer
    line went with finishing better (rank 1 = best); rho < 0 for polar_pct means faster-than-cert
    execution went with finishing better. Correlation, not causation — stated in the UI."""
    latest = {}
    for s in rs.get_scores(race_id):
        m = json.loads(s["metrics_json"])
        if m.get("available") and s["run_id"] >= latest.get(s["team_id"], (0, None))[0]:
            latest[s["team_id"]] = (s["run_id"], m)
    scores = {tid: m for tid, (_rid, m) in latest.items()}
    if not scores:
        return {"ok": False, "note": "no scored runs yet — run the fleet batch first"}
    entries = {e["team_id"]: e for e in rs.get_entries(race_id)}

    divs = {}
    for r in rs.get_results(race_id):
        m = scores.get(r["team_id"])
        if m and r.get("finished") and r.get("rank_division"):
            divs.setdefault(r["division"], []).append((r, m))

    out, pooled_x, pooled_rank = [], {"behind": [], "xte": [], "polar": []}, []
    for name, rows in sorted(divs.items()):
        if len(rows) < 5:
            continue
        rows.sort(key=lambda t: t[0]["rank_division"])
        rank = [r["rank_division"] for r, _ in rows]
        col = lambda key: [m.get(key) if m.get(key) is not None else 0 for _, m in rows]
        third = max(1, len(rows) // 3)
        sides = [m.get("side_worked") for _, m in rows[:third]]
        out.append({
            "division": name, "n": len(rows),
            "rho_behind_min": _spearman(col("time_behind_optimal_min"), rank),
            "rho_xte": _spearman(col("xte_mean_nm"), rank),
            "rho_extra_distance": _spearman(col("extra_distance_pct"), rank),
            "rho_polar_pct": _spearman(col("polar_pct"), rank),
            "top_third_sides": {s: sides.count(s) for s in set(sides) if s},
            "boats": [{"boat": (entries.get(r["team_id"]) or {}).get("boat"),
                       "rank": r["rank_division"],
                       "behind_min": m.get("time_behind_optimal_min"),
                       "xte_nm": m.get("xte_mean_nm"),
                       "extra_pct": m.get("extra_distance_pct"),
                       "polar_pct": m.get("polar_pct"),
                       "side": m.get("side_worked")} for r, m in rows]})
        n = len(rows)
        for (r, m) in rows:
            pooled_rank.append((r["rank_division"] - 1) / max(1, n - 1))
            pooled_x["behind"].append(m.get("time_behind_optimal_min") or 0)
            pooled_x["xte"].append(m.get("xte_mean_nm") or 0)
            pooled_x["polar"].append(m.get("polar_pct") or 0)

    pooled = {"n": len(pooled_rank),
              "rho_behind_min": _spearman(pooled_x["behind"], pooled_rank),
              "rho_xte": _spearman(pooled_x["xte"], pooled_rank),
              "rho_polar_pct": _spearman(pooled_x["polar"], pooled_rank)} if pooled_rank else None
    return {"ok": True, "race_id": race_id, "scored_boats": len(scores),
            "divisions": out, "pooled": pooled,
            "caveats": ["Correlation, not causation — a good crew both routes well and sails fast.",
                        "Corrected-time rank carries rating luck; correlations run within divisions.",
                        "The optimal is each boat's OWN gun-forecast route (GFS+HRRR archive blend)."]}


def venue_stats():
    """Fleet-normal adherence stats + side history across the archived races (locked Phase-B
    input #3/#7 — the bundle freezes these so onboard phrasing is percentile-framed against the
    venue's empirical distribution). One team counted once per race (newest run). None when the
    archive is empty."""
    races = rs.list_races()
    xte, behind, side_hist, n_boats = [], [], [], 0
    for r in races:
        latest = {}
        for s in rs.get_scores(r["race_id"]):
            m = json.loads(s["metrics_json"])
            if m.get("available") and s["run_id"] >= latest.get(s["team_id"], (0, None))[0]:
                latest[s["team_id"]] = (s["run_id"], m)
        ms = [m for _rid, m in latest.values()]
        n_boats += len(ms)
        xte += [m["xte_mean_nm"] for m in ms if m.get("xte_mean_nm") is not None]
        behind += [m["time_behind_optimal_min"] for m in ms
                   if m.get("time_behind_optimal_min") is not None]
        rep = report(r["race_id"])
        if rep.get("ok"):
            div1 = next((d for d in rep["divisions"] if "overall" in d["division"].lower()), None)
            if div1:
                side_hist.append({"race_id": r["race_id"],
                                  "top_third_sides": div1["top_third_sides"]})
    if not xte and not behind:
        return None

    def pct(v, p):
        if not v:
            return None
        v = sorted(v)
        return round(v[min(len(v) - 1, int(p * len(v)))], 1)

    return {"races": [r["race_id"] for r in races], "n_boats": n_boats,
            "xte_median_nm": pct(xte, 0.5), "xte_p90_nm": pct(xte, 0.9),
            "behind_median_min": pct(behind, 0.5), "behind_p90_min": pct(behind, 0.9),
            "side_history": side_hist,
            "note": ("fleet-normal adherence at this venue (RETRO_STUDY.md §6) — a boat near the "
                     "median is sailing NORMALLY; p90 marks a genuine departure. Side history is a "
                     "labeled historical prior, never a forecast.")}


def match_polars(race_id: str, country: str = "USA") -> dict:
    """R2: ORC cert + converted polar for every entry that matches the public dump."""
    entries = rs.get_entries(race_id)
    if not entries:
        return {"ok": False, "note": "race not ingested yet — run ingest first"}
    idx = fi._orc_dump(country)
    matched, misses = 0, []
    for e in entries:
        rec, by, conf = None, None, 0.0
        s = fi._norm_sail(e.get("sail"))
        if s and s in idx["by_sail"]:
            rec, by, conf = idx["by_sail"][s], "sail", 0.9
        else:
            n = fi._norm(e.get("boat"))
            if n and n in idx["by_name"]:
                rec, by, conf = idx["by_name"][n], "name", 0.6
        if not rec:
            misses.append(e.get("boat"))
            continue
        polar = orcpolar.cert_polar(rec)
        if not polar:
            misses.append(f"{e.get('boat')} (cert has no Allowances)")
            continue
        refno = next((rec.get(k) for k in ("RefNo", "CertNo", "FileId", "BIN") if rec.get(k)),
                     f"{rec.get('SailNo')}|{rec.get('YachtName')}")
        cert_id = rs.save_cert(country, refno, rec.get("YachtName"), rec.get("SailNo"), rec)
        rs.save_polar(race_id, e["team_id"], cert_id, polar, by, conf)
        matched += 1
    return {"ok": True, "race_id": race_id, "entries": len(entries), "matched": matched,
            "certs_in_dump": idx.get("n"), "unmatched": misses[:30],
            "unmatched_n": len(misses)}
