"""Fleet retro study — ingest past races + run the optimizer for every boat (docs/RETRO_STUDY.md).

R1 (this module, ingest): the race's tracker provider (`trackers`: YB, or an uploaded GPX/CSV
fleet file). For YB: RaceSetup (entries, start, TCFs, divisions) + the AllPositions3
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
from . import trackers

# No race is built in (#310): the fleet is routed on the course of a Lab race definition the user
# picks, and the tracks come from that race's tracker provider (`trackers`: YB, or a fleet file).
NO_RACE = "Pick a race to route on."
NO_COURSE = "This race has no course with marks."


def setup_for(definition: dict) -> dict:
    """What the retro card needs from a Lab race definition: its tracker provider and the race
    id it names (or a plain note why there is none), the course retro routes on, and the ORC
    countries to search. Nothing here guesses a race."""
    d = definition or {}
    tr = d.get("tracker") or {}
    prov = trackers.provider_name(d)
    p = trackers.get(prov)
    yb_id, note = None, None
    if p is trackers.YB and p.ref(tr):
        yb_id = p.ref(tr)
    elif p is trackers.File:
        note = trackers.FILE_HINT
    elif prov and p is None and prov != "sample":
        note = trackers.unknown_note(prov) + " Or type a YB race id if it had one."
    else:
        note = "No YB race id in this race. Type one, or upload a fleet file."
    courses = [{"id": c.get("id"), "name": c.get("name") or c.get("id")}
               for c in d.get("courses") or [] if c.get("id")]
    return {"race_id": d.get("race_id"), "provider": p.name if p else (prov or None),
            "upload": p is trackers.File, "yb_id": yb_id, "note": note,
            "course_id": default_course(d), "courses": courses,
            "countries": race_def.rating_countries(d)}


def default_course(definition: dict):
    """Our division's course, else the race's first course; None when it has none."""
    d = definition or {}
    own = race_def.course_of_division(d, (d.get("own") or {}).get("division"))
    ids = [c.get("id") for c in d.get("courses") or [] if c.get("id")]
    return own if own in ids else (ids[0] if ids else None)


def ingest_race(race_id: str, provider: str = "yb") -> dict:
    """Pull one race off a fetching tracker (YB) into the archive: race + entries + anchored
    tracks + results."""
    p = trackers.get(provider)
    if p is None or not p.fetches:
        return {"ok": False, "note": trackers.unknown_note(provider)}
    return store_fleet(race_id, p.fleet(race_id))


RESCORE = "Old scores cleared. Score the fleet again."
BUSY = "Scoring is running. Upload after it ends."
STALE_POLARS = "The fleet was imported again while matching. Nothing more saved. Find ORC polars again."
STALE = "The fleet was imported again while scoring. Nothing more saved. Score the fleet again."


def ingest_file(race_id: str, files, definition=None) -> dict:
    """Store an uploaded fleet file (GPX/CSV, `trackers.parse_fleet_files`) under `race_id`.
    A re-upload REPLACES the race (`rs.replace_fleet`): boats, tracks and ranks the new file
    leaves out are gone, the race's runs and scores are cleared, and a boat keeps its id (and its
    polar, when unchanged) whatever order the file lists it in (`stable_ids`)."""
    if not race_id:
        return {"ok": False, "note": NO_RACE}
    job = fleet_job_status()
    if job.get("state") == "running" and job.get("race_id") == race_id:
        return {"ok": False, "busy": True, "note": BUSY}
    fleet = trackers.parse_fleet_files(files, definition)
    if not fleet.get("ok"):
        return fleet
    stable_ids(fleet, rs.get_entries(race_id))
    cleared = rs.replace_fleet(race_id, fleet.get("setup") or {}, fleet.get("start"), "file",
                               fleet["teams"], fleet["tracks"], fleet["results"])
    out = _summary(race_id, fleet)
    out["cleared"] = cleared
    out["no_start"] = fleet.get("no_start") or []
    notes = [fleet.get("results_note"), RESCORE if cleared["runs"] else None]
    if any(notes):
        out["note"] = " ".join(n for n in notes if n)
    return out


def _sail_key(sail):
    return fi._norm_sail(sail) or None


def _name_key(name):
    return fi._norm(name) or None


def stable_ids(fleet: dict, existing: list) -> None:
    """Give each uploaded boat the id it already has in the archive, matched by sail number
    (all of those first), then by name; a new boat gets the next unused id. Rewrites the fleet's teams, tracks and
    results in place. Without this the ids follow the file's order, and a re-upload in another
    order would hand each boat another boat's polar and scores."""
    by_sail = {_sail_key(e.get("sail")): e["team_id"] for e in existing if _sail_key(e.get("sail"))}
    by_name = {_name_key(e.get("boat")): e["team_id"] for e in existing if _name_key(e.get("boat"))}
    nxt = max([e["team_id"] for e in existing] + [0]) + 1
    used, remap = set(), {}
    # every sail-number match is reserved before any name match, so the file's row order can't
    # let a boat matched by name take an id another boat holds by its sail number
    for key_of, table in ((lambda t: _sail_key(t.get("sail")), by_sail),
                          (lambda t: _name_key(t.get("name")), by_name)):
        for t in fleet["teams"]:
            key = key_of(t)
            if t["id"] in remap or not key or table.get(key) is None or table[key] in used:
                continue
            remap[t["id"]] = table[key]
            used.add(table[key])
    for t in fleet["teams"]:
        if t["id"] not in remap:
            remap[t["id"]], nxt = nxt, nxt + 1
    for t in fleet["teams"]:
        t["id"] = remap[t["id"]]
    fleet["tracks"] = {remap[k]: v for k, v in fleet["tracks"].items()}
    for r in fleet["results"]:
        r["team_id"] = remap[r["team_id"]]


def _summary(race_id, fleet):
    results = fleet.get("results") or []
    return {"ok": True, "race_id": race_id, "teams": len(fleet.get("teams") or []),
            "blocks": fleet.get("blocks"), "tracks": len(fleet.get("tracks") or {}),
            "results": len(results), "divisions": len({r["division"] for r in results}),
            "unmatched_tracks": (fleet.get("unmatched") or [])[:20]}


def store_fleet(race_id: str, fleet: dict) -> dict:
    """Write a fetched provider's normalized fleet (YB) into the retro archive. Upserts, as YB
    always did: YB team ids are the feed's own, so they stay put across re-fetches."""
    if not fleet.get("ok"):
        return fleet
    rs.upsert_fleet(race_id, fleet.get("setup") or {}, fleet.get("start"),
                    fleet.get("source") or "yb", fleet.get("teams") or [],
                    fleet.get("tracks") or {}, fleet.get("results") or [])
    out = _summary(race_id, fleet)
    if fleet.get("results_note"):
        out["note"] = fleet["results_note"]
    return out


_RESULT_HEAVY = ("isochrones", "laylines", "candidate_paths", "wind_grid", "current_grid",
                 "wave_grid", "obstacles", "log")

# ---- background fleet-run job (a full fleet is ~1-2h; the nginx gateway caps a request at 300s,
# so the batch runs via the shared jobs.py machinery and the UI/CLI polls status) ----------------

def start_fleet_job(race_id, def_race_id, course_id, teams, limit, resolution) -> dict:
    """Refuses up front (no job) when no race definition is picked or it isn't in the library."""
    if not def_race_id:
        return {"ok": False, "note": NO_RACE}
    from . import store
    if not store.get_race(def_race_id):
        return {"ok": False, "note": f"Race {def_race_id!r} isn't in the library."}
    return jobs.start(
        "retro_fleet",
        lambda progress: run_fleet(race_id, def_race_id, course_id, teams, limit, resolution,
                                   on_progress=progress),
        meta={"race_id": race_id})


def fleet_job_status() -> dict:
    return jobs.status("retro_fleet")


def run_fleet(race_id: str, def_race_id: str = None, course_id: str = None, teams=None,
              limit=None, resolution: str = "auto", on_progress=None) -> dict:
    """R4: optimize the course for EVERY archived boat on ITS ORC polar through the forecast that
    was knowable at ITS division's gun, then score its actual track against its own optimal route.

    One wind field is built per distinct division-start hour (staggered starts pick different HRRR
    cycles; the pinned GRIBs are shared). Results are slimmed of visualization-only arrays but keep
    path/legs — the archive is for analysis, not repaint. `teams`/`limit` scope a pilot subset.

    `def_race_id` is required: the Lab race definition whose course the fleet is routed on. No
    course given → our division's course, else the race's first (`default_course`)."""
    from . import optimizer, store
    from .wind import build_windfield
    from .wind.archive import gun_sources

    say = on_progress or (lambda *_: None)
    if not def_race_id:
        return {"ok": False, "note": NO_RACE}
    d = store.get_race(def_race_id)
    if not d:
        return {"ok": False, "note": f"Race {def_race_id!r} isn't in the library."}
    race = rs.get_race(race_id)
    if not race:
        return {"ok": False, "note": "race not ingested yet"}
    marks, _skipped, course_id = race_def.course_to_marks(d, course_id or default_course(d))
    if len(marks) < 2:
        return {"ok": False, "note": NO_COURSE}

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
    # The import this run scores (#345): a re-import mid-run (a fleet file uploaded again) changes
    # the race's stamp, and every write below is refused once it has, so old routes never land
    # against new tracks or ranks.
    stamp = race.get("ingested_at")
    for e in targets:
        # A fleet file's boat starts on its own division's gun or isn't scored: it is never given
        # another division's gun or its first fix (#345). A YB team falls back to the race start,
        # as it always did.
        start = e.get("start_epoch") or (race["start_epoch"] if race.get("source") != "file" else None)
        if start is None:
            failed.append({"boat": e["boat"], "note": trackers.no_start_note([e["boat"]])})
            continue
        start = float(start)
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
        say(f"optimize {e['boat']} ({len(P)} polar pts, start +{round((start - (race['start_epoch'] or start))/60)}min)")
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
        # half a route poisons the analysis. The router's own `incomplete` flag comes first (#31): on
        # a course that finishes at its start, a route stalled on leg 1 ends within 3 nm of the finish.
        last = result["path"][-1]
        gap_nm = track_mod._hav_nm((last["lat"], last["lon"]), (marks[-1][2], marks[-1][3]))
        why = optimizer.incomplete_reason(result, (marks[-1][2], marks[-1][3]))
        if why:
            slim = {k: v for k, v in result.items() if k not in _RESULT_HEAVY}
            if rs.save_run(race_id, e["team_id"], "gun_forecast_partial",
                           {"def_race_id": def_race_id, "course_id": course_id,
                            "start_epoch": start, "resolution": resolution,
                            "finish_gap_nm": round(gap_nm, 1)}, {}, slim, stamp=stamp) is None:
                return _stale(race_id, done, failed, fields)
            failed.append({"boat": e["boat"], "note": f"route incomplete — {why}"})
            continue
        slim = {k: v for k, v in result.items() if k not in _RESULT_HEAVY}
        run_id = rs.save_run(race_id, e["team_id"], "gun_forecast",
                             {"def_race_id": def_race_id, "course_id": course_id,
                              "start_epoch": start, "resolution": resolution,
                              "polar_pts": len(P)},
                             {"models": [m for m in (result.get("wind_field") or {}).get("models", [])]},
                             slim, stamp=stamp)
        if run_id is None:
            return _stale(race_id, done, failed, fields)
        fixes = rs.get_track(race_id, e["team_id"])
        try:
            metrics = track_mod.score_track({"fixes": fixes}, result, marks, start, wf=wf, polars=P)
        except Exception as exc:
            metrics = {"available": False, "note": f"scoring error: {exc}"}
        if not rs.save_score(race_id, e["team_id"], run_id, metrics, stamp=stamp):
            return _stale(race_id, done, failed, fields)
        done.append({"boat": e["boat"], "run_id": run_id,
                     "opt_hours": result.get("total_hours"),
                     "scored": bool(metrics.get("available"))})
    return {"ok": True, "race_id": race_id, "ran": len(done), "failed": failed[:20],
            "wind_fields": len(fields), "boats": done}


def _stale(race_id, done, failed, fields):
    return {"ok": False, "stale": True, "note": STALE, "race_id": race_id, "ran": len(done),
            "failed": failed[:20], "wind_fields": len(fields), "boats": done}


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


def match_polars(race_id: str, country=None, def_race_id: str = None) -> dict:
    """R2: ORC cert + converted polar for every entry that matches the public dump.

    Countries: the ones typed (`country`, 'USA,CAN' or a list), else the picked race definition's
    (its scoring list, else venue country + entrants' sail prefixes), else the entrants' sail
    prefixes alone. A sail-number match in any country's dump beats a name match; country order
    only breaks ties within the same kind of match."""
    # the import these entries belong to (#345): a re-import while the ORC dumps load must not
    # get the old entries' polars written onto its boat ids
    stamp = (rs.get_race(race_id) or {}).get("ingested_at")
    entries = rs.get_entries(race_id)
    if not entries:
        return {"ok": False, "note": "race not ingested yet — run ingest first"}
    definition = None
    if def_race_id:
        from . import store
        definition = store.get_race(def_race_id)
    countries = fi._countries(country, definition, entries)
    if not countries:
        return {"ok": False, "note": fi.COUNTRY_UNKNOWN}
    idxs = []
    for cc in countries:
        try:
            idxs.append((cc, fi._orc_dump(cc)))
        except Exception as exc:
            print(f"[retro] ORC dump {cc} failed ({type(exc).__name__}) — skipping", flush=True)
    if not idxs:
        return {"ok": False, "note": f"ORC database fetch failed ({','.join(countries)})"}
    matched, misses = 0, []
    for e in entries:
        rec, by, conf, country = None, None, 0.0, None
        s = fi._norm_sail(e.get("sail"))
        n = fi._norm(e.get("boat"))
        # a sail-number match in ANY country beats a name match; country order only breaks ties
        for key, table, kind, c in ((s, "by_sail", "sail", 0.9), (n, "by_name", "name", 0.6)):
            hit = next(((cc, idx[table][key]) for cc, idx in idxs if key and key in idx[table]),
                       None)
            if hit:
                country, rec = hit
                by, conf = kind, c
                break
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
        if not rs.save_polar(race_id, e["team_id"], cert_id, polar, by, conf, stamp=stamp):
            return {"ok": False, "stale": True, "note": STALE_POLARS, "race_id": race_id,
                    "matched": matched}
        matched += 1
    return {"ok": True, "race_id": race_id, "entries": len(entries), "matched": matched,
            "countries": [cc for cc, _ in idxs],
            "certs_in_dump": sum(idx.get("n") or 0 for _, idx in idxs), "unmatched": misses[:30],
            "unmatched_n": len(misses)}
