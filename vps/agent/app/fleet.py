"""Handicap-aware FLEET tactics — classify AIS targets against the pre-loaded race roster and turn
the matched competitors into tactical intelligence: course progress, leverage, and the ORC
**corrected-time delta** (who I actually need to beat, and by how much — NOT raw on-water position).

Compliance (RRS 41): this is CLEAN in-race. The targets arrive on the boat's OWN AIS receiver
(other-vessel Signal K contexts); the roster + ORC handicaps are PRE-LOADED public homework, frozen
at the gun; the geometry + handicap math run on the boat's OWN computer. The engine COMPUTES, the
LLM only INTERPRETS (pressure, cover/split). No mid-race cloud call.

Built source-agnostic on the Phase-9.0 seam (`datasource.active()` + the `ais` helpers), so the same
code runs in the cloud (`CloudSource`: `ais_targets`+`telemetry_raw`+`app_state`) and onboard the Pi
(`OnboardSource`: other-vessel SK contexts + the engine SQLite). It extends the collision-only AIS
layer to TACTICAL: FLEET (matched to the roster) vs TRAFFIC (the always-on collision guard).

FUZZY by construction (perflab item-5): AIS coverage is partial (not every boat transmits; Class B is
laggy), name/MMSI matching is imperfect, and corrected-time is a projection — so every competitor row
carries a confidence and the gaps are stated honestly. Treat as soft signals, never gospel.
"""
import math
import os
import re

from . import datasource, ais, tracker

# corrected-time tags
_RIVAL_BAND_S = 180.0       # within ±3 min corrected → "the boat you're racing"
_TOT_K = 600.0              # ToT ≈ K/GPH; K cancels in any ratio, so its exact value only scales the
                            # constant — we use the conventional 600 so a lone displayed coeff is sane.

# tracker fusion
_TRACKER_MATCH_NM = float(os.environ.get("TRACKER_MATCH_NM", "0.5"))  # AIS↔tracker-fix proximity match
_SRC_RANK = {"ais": 0, "tracker": 1}     # live own-receiver AIS outranks the delayed tracker for the face


# --- name / roster matching --------------------------------------------------
def _norm(s):
    """Normalize a vessel name for fuzzy matching: lowercase, drop non-alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match(target, roster):
    """Match a raw AIS target to a roster entry. MMSI first (exact → high confidence), then a
    normalized-name containment test (medium). Returns (entry, matched_by, confidence) or
    (None, None, 0.0)."""
    tm = str(target.get("mmsi") or "").strip()
    if tm:
        for e in roster:
            if str(e.get("mmsi") or "").strip() and str(e["mmsi"]).strip() == tm:
                return e, "mmsi", 0.95
    tn = _norm(target.get("name"))
    if tn:
        for e in roster:
            en = _norm(e.get("boat"))
            if en and (en == tn or en in tn or tn in en):
                return e, "name", 0.6 if en == tn else 0.45
    return None, None, 0.0


# --- ORC corrected-time model ------------------------------------------------
def _tot_coeff(entry, scoring_method):
    """ORC Time-on-Time coefficient for an entry: corrected = elapsed × ToT. Prefer the published
    single-number `rating` (already a ToT coeff); else derive from GPH (ToT ≈ K/GPH — a faster boat
    has a lower GPH and a higher coefficient). Returns None if neither is known."""
    r = entry.get("rating")
    if r:
        return float(r)
    g = entry.get("orc_gph") or entry.get("gph")
    return _TOT_K / float(g) if g else None


def _allowance_s_per_nm(entry):
    """ORC Time-on-Distance allowance (s/nm) = GPH. Returns None if unknown."""
    g = entry.get("orc_gph") or entry.get("gph")
    return float(g) if g else None


# --- course geometry (flat-plane nm; matches ais.py / navigator scale) -------
def _nm(lat0, lon0, lat, lon):
    """(east, north) of (lat,lon) relative to (lat0,lon0), in nm."""
    de = (lon - lon0) * 60.0 * math.cos(math.radians(lat0))
    dn = (lat - lat0) * 60.0
    return de, dn


def _course_progress(pe, pn, pts):
    """pts = [(e,n)] course marks in nm (start..finish). Project (pe,pn) onto the polyline.
    Returns (dtf_nm, leverage_nm, leg_index). dtf = remaining distance along the course to the
    finish; leverage = signed cross-track on the nearest leg (+ = right of the course heading)."""
    best = None
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        vx, vy = bx - ax, by - ay
        seg2 = vx * vx + vy * vy
        if seg2 < 1e-9:
            continue
        t = ((pe - ax) * vx + (pn - ay) * vy) / seg2          # projection fraction onto the leg
        tc = max(0.0, min(1.0, t))
        projx, projy = ax + tc * vx, ay + tc * vy
        perp = math.hypot(pe - projx, pn - projy)
        # signed cross-track: cross product sign of leg vector × (point - proj)
        sign = 1.0 if (vx * (pn - ay) - vy * (pe - ax)) < 0 else -1.0
        if best is None or perp < best[0]:
            # remaining distance: rest of this leg + all later legs
            rem = math.hypot(bx - projx, by - projy)
            for j in range(i + 1, len(pts) - 1):
                rem += math.hypot(pts[j + 1][0] - pts[j][0], pts[j + 1][1] - pts[j][1])
            best = (perp, rem, sign * perp, i)
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]


# --- main --------------------------------------------------------------------
def _active_route():
    try:
        from . import navigator
        return navigator._active or "race"
    except Exception:
        return "race"


def _fleet_row(entry, obj, matched_by, mconf, source, pts, origin, own, own_dtf,
               method, is_tod, own_tot, own_alw, age_s=None):
    """Build one matched-competitor row from a position-bearing object `obj` (lat/lon/sog/cog/mmsi).
    Shared by the live-AIS and the delayed-tracker paths so the geometry + corrected-time + confidence
    math is identical. `source` = 'ais' (own receiver, real-time) | 'tracker' (delayed, aged); a tracker
    fix carries `obj['_posconf']` (age decay) and `age_s`. Returns (row, dtf_nm)."""
    row = {"boat": entry.get("boat"), "division": entry.get("division", ""),
           "mmsi": obj.get("mmsi"), "matched_by": matched_by, "source": source,
           "sog": round(obj["sog"], 1) if obj.get("sog") is not None else None,
           "cog": round(obj["cog"]) if obj.get("cog") is not None else None}
    if age_s is not None:
        row["age_s"] = age_s
    if own.get("lat") is not None and obj.get("lat") is not None:
        de, dn = ais._enu_nm(own["lat"], own["lon"], obj["lat"], obj["lon"])
        row["range_nm"] = round(math.hypot(de, dn), 2)
        row["bearing"] = round((math.degrees(math.atan2(de, dn)) + 360) % 360)
    dtf = None
    if pts and obj.get("lat") is not None:
        te, tn = _nm(origin[0], origin[1], obj["lat"], obj["lon"])
        dtf, lev, _ = _course_progress(te, tn, pts)
        row["dtf_nm"] = round(dtf, 2) if dtf is not None else None
        row["leverage_nm"] = round(lev, 2) if lev is not None else None
        if own_dtf is not None and dtf is not None:
            row["on_water_lead_nm"] = round(own_dtf - dtf, 2)   # + = they're ahead on the water
    cdelta, basis, _ = _corrected_delta(entry, method, is_tod, dtf, own_dtf,
                                        obj.get("sog"), own.get("sog"), own_tot, own_alw)
    if cdelta is not None:
        row["corrected_delta_s"] = round(cdelta)
        row["corrected_basis"] = basis
    # confidence = match × handicap-known × course-known × position-freshness (age decay; 1.0 for live)
    hconf = 1.0 if _tot_coeff(entry, method) else 0.4
    cconf = 1.0 if dtf is not None else 0.5
    posconf = obj.get("_posconf", 1.0)
    row["confidence"] = round(mconf * hconf * cconf * posconf, 2)
    row["tag"] = _tag(row)
    return row, dtf


def get_fleet(max_range_nm: float = 40.0):
    """Fleet tactical view: matched competitors with course progress + corrected-time delta, plus
    the unmatched AIS traffic (the collision layer is unchanged). Folds in a permitted public race
    tracker as a second, DELAYED source — identity-resolving unmatched AIS targets by proximity and
    adding over-the-horizon roster boats not on our AIS at all (every tracker row aged +
    confidence-reduced). Sorted rivals-first (smallest absolute corrected delta; live AIS outranks the
    delayed tracker). Returns a dict ready for the tool / endpoint / dashboard tile."""
    blob = datasource.active().get_fleet() or {}
    roster = blob.get("fleet") or []
    own_cfg = blob.get("own") or {}
    scoring = blob.get("scoring") or {}
    method = (scoring.get("method") or scoring.get("system") or "").lower()
    is_tod = "distance" in method or "tod" in method        # else default ToT

    gaps = []
    if not roster:
        return {"available": False, "note": "No fleet roster loaded — load the RaceDefinition "
                "fleet block onboard (POST /fleet/load).", "fleet": [], "traffic": []}
    if not scoring:
        gaps.append("no scoring method loaded — assuming Time-on-Time")

    own = ais._own_ship()
    raw = datasource.active().ais_targets(ais.AIS_WINDOW_MIN)

    # course marks (the loaded homework) → flat-plane nm polyline about own position (or first mark)
    route = _active_route()
    marks = datasource.active().marks(route)
    pts = None
    origin = None
    if len(marks) >= 2:
        origin = (marks[0]["lat"], marks[0]["lon"])
        pts = [_nm(origin[0], origin[1], m["lat"], m["lon"]) for m in marks]
    else:
        gaps.append("no course loaded — distance-to-finish/leverage unavailable")

    # own progress
    own_dtf = own_lev = None
    own_tot = _tot_coeff(own_cfg, method) or 1.0
    own_alw = _allowance_s_per_nm(own_cfg)
    if pts and own.get("lat") is not None:
        oe, on = _nm(origin[0], origin[1], own["lat"], own["lon"])
        own_dtf, own_lev, _ = _course_progress(oe, on, pts)

    fleet, unmatched = [], []
    for r in raw:
        entry, matched_by, mconf = _match(r, roster)
        if entry is None:
            unmatched.append(r)                  # keep the full target for tracker identity-resolution
            continue
        row, _ = _fleet_row(entry, {**r, "_posconf": 1.0}, matched_by, mconf, "ais",
                            pts, origin, own, own_dtf, method, is_tod, own_tot, own_alw)
        fleet.append(row)

    # --- public race tracker: a permitted, DELAYED, over-the-horizon source ----------------------
    tk_status = _merge_tracker(blob, roster, fleet, unmatched, pts, origin, own, own_dtf,
                               method, is_tod, own_tot, own_alw, gaps)

    # unmatched targets that the tracker couldn't identify stay in the collision layer (a light passthrough)
    traffic = [{"mmsi": r.get("mmsi"), "name": r.get("name"),
                "sog": round(r["sog"], 1) if r.get("sog") is not None else None} for r in unmatched]

    # rivals first: smallest |corrected delta|, then live AIS over the delayed tracker, then nearest
    fleet.sort(key=lambda x: (abs(x.get("corrected_delta_s", 1e9)),
                              _SRC_RANK.get(x.get("source"), 9), x.get("range_nm", 1e9)))
    return {
        "available": True,
        "scoring_method": scoring.get("method") or ("Time-on-Distance" if is_tod else "Time-on-Time"),
        "roster_size": len(roster),
        "own": {"dtf_nm": round(own_dtf, 2) if own_dtf is not None else None,
                "leverage_nm": round(own_lev, 2) if own_lev is not None else None,
                "fix": own.get("lat") is not None},
        "count_matched": len(fleet), "count_traffic": len(traffic),
        "count_ais": sum(1 for f in fleet if f.get("source") == "ais"),
        "count_tracker": sum(1 for f in fleet if f.get("source") == "tracker"),
        "tracker": tk_status,
        "fleet": fleet, "traffic": traffic,
        "gaps": gaps or None,
        "note": ("Corrected-time is a projection to the finish; AIS coverage is partial and the "
                 "public tracker is delayed — soft signals, confidence-flagged."),
    }


def _merge_tracker(blob, roster, fleet, unmatched, pts, origin, own, own_dtf,
                   method, is_tod, own_tot, own_alw, gaps):
    """Fold a permitted public race tracker into the fleet view. Two uses (perflab item-6): (a) IDENTITY
    — resolve an unmatched AIS target to a roster boat by proximity to that boat's tracker fix (fills the
    AIS↔roster MMSI-match gap); (b) OVER-THE-HORIZON — add roster boats seen on the tracker but not on
    our AIS at all, as aged + confidence-reduced rows (`source='tracker'`). Mutates `fleet`/`unmatched`
    in place. Returns the tracker status dict (or None if no tracker is configured)."""
    tcfg = blob.get("tracker") or {}
    if not tcfg:
        return None
    if not tcfg.get("permitted"):
        gaps.append("public tracker configured but NOT permitted for this race (rules_profile) — "
                    "over-the-horizon fleet withheld")
        return {"permitted": False, "available": False,
                "note": "Tracker not permitted for this race — confirm the SI."}
    tk = tracker.positions(tcfg)
    status = {"permitted": True, "available": tk["available"], "count": len(tk["positions"]),
              "fetched_at": tk["fetched_at"], "error": tk["error"], "delay_min": tk["delay_min"],
              "note": tk["note"]}
    if tk["error"]:
        gaps.append(f"public tracker fetch issue: {tk['error']}")
    # match each tracker fix to a roster entry by NAME (the tracker's value-add is identity)
    tk_matched = []
    for fx in tk["positions"]:
        e, _, _ = _match({"name": fx.get("name")}, roster)
        if e is not None:
            tk_matched.append((e, fx))
    have = {_norm(row["boat"]) for row in fleet}     # roster boats already represented by live AIS

    # (a) identity-resolve unmatched AIS targets via proximity to a roster boat's tracker fix
    still_unmatched = []
    for r in unmatched:
        resolved = None
        if r.get("lat") is not None:
            for e, fx in tk_matched:
                if _norm(e["boat"]) in have:
                    continue
                de, dn = ais._enu_nm(r["lat"], r["lon"], fx["lat"], fx["lon"])
                if math.hypot(de, dn) <= _TRACKER_MATCH_NM:
                    resolved = e
                    break
        if resolved is not None:
            # a LIVE AIS position with an identity borrowed from the tracker → source stays 'ais',
            # confidence reduced for the fuzzy position match
            row, _ = _fleet_row(resolved, {**r, "_posconf": 0.6}, "tracker_position", 0.5, "ais",
                                pts, origin, own, own_dtf, method, is_tod, own_tot, own_alw)
            row["note"] = "identity from tracker (position match)"
            fleet.append(row)
            have.add(_norm(resolved["boat"]))
        else:
            still_unmatched.append(r)
    unmatched[:] = still_unmatched

    # (b) over-the-horizon: roster boats on the tracker but not on our AIS at all
    for e, fx in tk_matched:
        if _norm(e["boat"]) in have:
            continue
        obj = {"lat": fx["lat"], "lon": fx["lon"], "sog": fx.get("sog"), "cog": fx.get("cog"),
               "mmsi": None, "_posconf": fx["confidence"]}
        row, _ = _fleet_row(e, obj, "tracker_name", 0.7, "tracker", pts, origin, own, own_dtf,
                            method, is_tod, own_tot, own_alw, age_s=fx["age_s"])
        fleet.append(row)
        have.add(_norm(e["boat"]))
    return status


def _corrected_delta(entry, method, is_tod, dtf, own_dtf, sog, own_sog, own_tot, own_alw):
    """Projected corrected-time delta of `entry` vs own boat, in seconds. Negative = the competitor
    is projected to BEAT us (less corrected time); positive = we beat them. Basis 'remaining' = only
    the part of the race still in play (no race-start time needed; the locked-in elapsed can't change
    the tactical picture). Returns (delta_s, basis, confidence) or (None, None, 0)."""
    if dtf is None or own_dtf is None:
        return None, None, 0.0
    # project time-to-finish from each boat's speed made good toward the finish (fuzzy: use SOG)
    v_them = sog if (sog and sog > 0.5) else None
    v_us = own_sog if (own_sog and own_sog > 0.5) else None
    if v_them is None or v_us is None:
        return None, None, 0.0
    ttf_them = dtf / v_them * 3600.0          # s
    ttf_us = own_dtf / v_us * 3600.0
    if is_tod:
        alw = _allowance_s_per_nm(entry)
        if alw is None or own_alw is None:
            return None, None, 0.0
        # ToD: corrected = elapsed − allowance·distance. Over the REMAINING course each boat owes its
        # allowance on the miles still to sail; delta_remaining = (ttf_them − alw·dtf) − (ttf_us − alw_us·own_dtf)
        corr_them = ttf_them - alw * dtf
        corr_us = ttf_us - own_alw * own_dtf
        return corr_them - corr_us, "remaining", 0.6
    # ToT: corrected = elapsed × coeff. Remaining-corrected = ttf × coeff.
    tot_them = _tot_coeff(entry, method)
    if tot_them is None:
        return None, None, 0.0
    return ttf_them * tot_them - ttf_us * own_tot, "remaining", 0.6


def _tag(row):
    """Deterministic tactical tag the LLM can elaborate (it never originates strategy)."""
    cd = row.get("corrected_delta_s")
    if cd is None:
        lead = row.get("on_water_lead_nm")
        if lead is None:
            return "fleet"
        return "ahead_on_water" if lead > 0 else "behind_on_water"
    if abs(cd) <= _RIVAL_BAND_S:
        return "rival"                      # the boat you're actually racing on corrected time
    # tag is the COMPETITOR's standing vs us: cd<0 = they're projected to beat us → ahead.
    return "ahead_corrected" if cd < 0 else "behind_corrected"
