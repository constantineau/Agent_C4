"""Debrief — ACTUAL boat-track ingestion + helm-vs-optimal scoring (Lab-4 enrichment).

The judge loop (`judge.py`) re-routes the course on the wind that actually blew (the ORACLE) and
measures plan-vs-foresight regret. This module adds the missing half: the boat's REAL sailed track,
scored against that oracle-optimal route — how the HELM executed vs the perfect line. Two inputs
(per the perflab §5 fuzzy-adherence baseline, which is the metric set here):

  - **GPX upload** — the certain, offline path: the crew exports a track from Expedition / a Vakaros /
    the boat instruments / a phone after the race. Deterministic, no network.
  - **YB our-boat** — auto-fetch our boat's full sailed track from the permitted public YB tracker
    (`cf.yb.tl` AllPositions3 binary). The JSON GetPositions feed carries only the LATEST fix; the
    full track is in the delta-encoded binary (format reverse-engineered + verified 2026-06-30 — see
    `_decode_allpositions3`). Shore-side debrief use of a public tracker is always fine (the in-race
    onboard-use gate `rules_profile.tracker_permitted` is a separate concern).

Scoring (`score_track`) yields the perflab §5 metrics — time behind optimal, sailed-distance excess
(XTE / oversail), the first-beat side the boat actually WORKED, and %-of-achievable polar from the
realized wind — geometry the Opus critique then interprets (helm vs conditions vs tactical). The
boat NEVER follows the optimal line exactly, so these are coaching deltas, never pass/fail.
"""
import json
import math
import os
import struct
import urllib.request
import xml.etree.ElementTree as ET

from . import optimizer

_R_NM = 3440.065
TRACK_DIR = os.environ.get("INGESTED_DIR", "/srv/ingested")
_YB_TIMEOUT = float(os.environ.get("TRACK_YB_TIMEOUT_S", "40"))
_YB_HOSTS = {"yb", "bycmack", "ybtracking", "yellowbrick"}


# ---- geometry (equirectangular at these scales; reuse the optimizer's haversine/bearing) ----------
def _hav_nm(a, b):
    return optimizer._hav_nm(a[0], a[1], b[0], b[1])


def _pt_seg_nm(p, a, b):
    """Distance (nm) from point p to segment a-b, equirectangular, lat-scaled longitude."""
    latr = math.radians((a[0] + b[0]) / 2)
    k = math.cos(latr)
    ax, ay = (a[1] - p[1]) * k, a[0] - p[0]
    bx, by = (b[1] - p[1]) * k, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        d = math.hypot(ax, ay)
    else:
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        d = math.hypot(ax + t * dx, ay + t * dy)
    return math.radians(d) * _R_NM


def _xte_to_path(p, path):
    """Min cross-track distance (nm) from p to the optimal-route polyline `path` [(lat,lon),...]."""
    if len(path) < 2:
        return None
    return min(_pt_seg_nm(p, path[i], path[i + 1]) for i in range(len(path) - 1))


def _path_len_nm(pts):
    return sum(_hav_nm(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def _nearest_idx(fixes, target):
    return min(range(len(fixes)),
               key=lambda i: _hav_nm((fixes[i]["lat"], fixes[i]["lon"]), target))


# ---- GPX --------------------------------------------------------------------------------------
def _parse_iso(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_gpx(data: bytes):
    """Parse GPX <trkpt lat lon><time>…</time></trkpt> into chronological fixes [{lat,lon,t,sog,cog}].

    Namespace-agnostic (matches any `*}trkpt`). `t` is epoch seconds when <time> is present, else None
    (then scoring anchors relative time on the race start). sog/cog are derived between fixes.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not valid GPX/XML: {e}")
    pts = []
    for el in root.iter():
        if not el.tag.endswith("}trkpt") and el.tag != "trkpt":
            continue
        try:
            lat = float(el.get("lat")); lon = float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        t = None
        for child in el:
            if child.tag.endswith("}time") or child.tag == "time":
                t = _parse_iso(child.text)
        pts.append({"lat": lat, "lon": lon, "t": t, "sog": None, "cog": None})
    if not pts:
        raise ValueError("no <trkpt> points found in the GPX")
    if any(p["t"] is not None for p in pts):
        pts = [p for p in pts if p["t"] is not None]
        pts.sort(key=lambda p: p["t"])
    _derive_sog_cog(pts)
    return {"source": "gpx", "fixes": pts, "n": len(pts)}


def _derive_sog_cog(pts):
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        d = _hav_nm((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if a["t"] is not None and b["t"] is not None and b["t"] > a["t"]:
            pts[i]["sog"] = round(d / ((b["t"] - a["t"]) / 3600.0), 2)
        pts[i]["cog"] = round(optimizer._bearing(a["lat"], a["lon"], b["lat"], b["lon"]), 1)
    if len(pts) > 1:
        pts[0]["sog"] = pts[1]["sog"]
        pts[0]["cog"] = pts[1]["cog"]


# ---- YB AllPositions3 binary (delta-encoded full track) ---------------------------------------
def _decode_allpositions3(b, nteams):
    """Decode the YB AllPositions3 binary into per-team tracks (RaceSetup teams[] order, but DNS/
    sparse blocks may be skipped — match identity by position, see fetch_yb_track).

    Format (big-endian, reverse-engineered + verified — see the YB-format reference memory): a per-team
    block = [i32 lat][i32 lon][u32 sep=0] then 8-byte delta records [u16 tag][i16 dlat][i16 dlon]
    [u16 extra]; lat/lon are deg*1e5, dt = tag & 0x7fff seconds, the tag high bit (0x8000) marks a
    delta record. A block ends at the first 8-byte-aligned u16 with the high bit CLEAR (the next team's
    inter-team header). Records are stored newest-first (base = latest fix), so each decoded track is
    REVERSED to chronological order here. Self-resyncs to each independent base signature so one bad
    block can't corrupt the rest.
    """
    N = len(b)
    u16 = lambda o: struct.unpack(">H", b[o:o + 2])[0]
    i16 = lambda o: struct.unpack(">h", b[o:o + 2])[0]
    i32 = lambda o: struct.unpack(">i", b[o:o + 4])[0]
    u32 = lambda o: struct.unpack(">I", b[o:o + 4])[0]

    def is_base(o):
        if o + 12 > N:
            return False
        la, lo = i32(o), i32(o + 4)
        return (-9000000 <= la <= 9000000 and -18000000 <= lo <= 18000000
                and u32(o + 8) == 0 and not (la == 0 and lo == 0)
                and (o + 14 > N or (u16(o + 12) & 0x8000)))

    blocks, o = [], 4
    while len(blocks) < nteams:
        while o <= N - 14 and not is_base(o):
            o += 1
        if o > N - 14:
            break
        lat, lon, t = i32(o) / 1e5, i32(o + 4) / 1e5, 0
        rev = [(lat, lon, 0)]
        o += 12
        while o <= N - 8 and (u16(o) & 0x8000):
            t += u16(o) & 0x7fff
            lat += i16(o + 2) / 1e5
            lon += i16(o + 4) / 1e5
            rev.append((lat, lon, t))
            o += 8
        total = rev[-1][2]                       # base = newest; rev[-1] = oldest (the start)
        fixes = [{"lat": la, "lon": lo, "t": total - ts, "sog": None, "cog": None}
                 for (la, lo, ts) in reversed(rev)]
        blocks.append(fixes)
    return blocks


def _yb_get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=_YB_TIMEOUT) as r:
        d = r.read()
    return d if raw else json.loads(d.decode("utf-8", "replace"))


def _yb_base(cfg):
    race = (cfg.get("race") or "").strip()
    host = (cfg.get("host") or "cf.yb.tl").strip()
    return (f"https://{host}", race) if race else (None, None)


def fetch_yb_track(definition, boat_name=None):
    """Fetch + decode OUR boat's full sailed track from the permitted YB tracker.

    Identity: decode every block, then pick the one whose latest fix matches our boat's GetPositions
    latest (lat,lon) — which also self-validates the decode. Falls back to the RaceSetup team-index of
    the named boat when GetPositions has no live fix for us (faded/archived feed)."""
    cfg = (definition or {}).get("tracker") or {}
    prov = (cfg.get("provider") or "").lower()
    if prov not in _YB_HOSTS:
        return {"ok": False, "note": "no YB tracker configured for this race (tracker.provider)"}
    base, race = _yb_base(cfg)
    if not race:
        return {"ok": False, "note": "tracker has no race id (tracker.race)"}
    want = (boat_name or cfg.get("boat") or (definition.get("boat") or {}).get("name") or "").strip()
    try:
        setup = _yb_get(f"{base}/JSON/{race}/RaceSetup")
        teams = setup.get("teams") or []
        binb = _yb_get(f"{base}/BIN/{race}/AllPositions3", raw=True)
    except Exception as e:
        return {"ok": False, "note": f"YB fetch failed: {type(e).__name__}"}
    if not teams:
        return {"ok": False, "note": "YB race not published yet (no teams) — re-check nearer race time"}
    blocks = _decode_allpositions3(binb, len(teams))
    if not blocks:
        return {"ok": False, "note": "YB tracks not available yet (binary empty/dormant)"}

    # identity by latest-fix match against GetPositions (the reliable, self-validating link)
    latest = {}
    try:
        gp = _yb_get(f"{base}/API3/Race/{race}/GetPositions?t=0")
        for tm in gp.get("teams") or []:
            ps = tm.get("positions") or []
            if ps:
                p = max(ps, key=lambda x: x.get("gpsAtMillis") or 0)
                latest[(tm.get("name") or "").lower()] = (p["latitude"], p["longitude"])
    except Exception:
        pass

    chosen, matched_by = None, None
    tgt = latest.get(want.lower()) if want else None
    if tgt:
        chosen = min(blocks, key=lambda fx: _hav_nm((fx[-1]["lat"], fx[-1]["lon"]), tgt))
        if _hav_nm((chosen[-1]["lat"], chosen[-1]["lon"]), tgt) < 1.0:
            matched_by = "gps_latest_fix"
        else:
            chosen = None
    if chosen is None and want:                  # fall back to team-order index
        idx = next((i for i, t in enumerate(teams) if (t.get("name") or "").lower() == want.lower()), None)
        if idx is not None and idx < len(blocks):
            chosen, matched_by = blocks[idx], "team_index"
    if chosen is None:
        return {"ok": False, "note": f"could not match boat {want!r} in the YB feed",
                "boats": [t.get("name") for t in teams][:60]}
    _derive_sog_cog(chosen)
    return {"ok": True, "source": "yb", "boat": want, "matched_by": matched_by,
            "race": race, "fixes": chosen, "n": len(chosen)}


# ---- scoring: actual track vs the oracle-optimal route -----------------------------------------
def score_track(track, oracle, marks, start_epoch, wf=None, polars=None):
    """The actual_track block: helm execution vs the oracle line (perflab §5 metrics).

    `oracle` is the judge's oracle result (path + total_hours/total_nm); `marks` flattened course
    waypoints [(name,type,lat,lon),...]; `wf` the actual-wind field (for %-of-polar). The track is
    clipped to the racing portion (nearest fix to start mark → nearest to finish mark) so pre-start /
    post-finish wandering doesn't pollute the numbers. Times are anchored on `start_epoch` when the
    track carries no absolute clock (YB / a GPX with no <time>)."""
    fixes = (track or {}).get("fixes") or []
    if len(fixes) < 2:
        return {"available": False, "note": "track too short to score"}
    path = oracle.get("path") or []
    opt_pts = [(p["lat"], p["lon"]) for p in path]
    start_pt = (marks[0][2], marks[0][3]) if marks else (fixes[0]["lat"], fixes[0]["lon"])
    finish_pt = (marks[-1][2], marks[-1][3]) if marks else (fixes[-1]["lat"], fixes[-1]["lon"])

    # clip to the racing window
    i0 = _nearest_idx(fixes, start_pt)
    i1 = _nearest_idx(fixes, finish_pt)
    if i1 <= i0:
        i0, i1 = 0, len(fixes) - 1
    seg = fixes[i0:i1 + 1]
    pts = [(f["lat"], f["lon"]) for f in seg]

    # time: prefer absolute timestamps; else anchor relative time on the gun
    if seg[0]["t"] is not None and seg[-1]["t"] is not None:
        rel0 = seg[0]["t"]
        epochs = [(f["t"] - rel0) + (start_epoch or 0) for f in seg]
        elapsed_h = (seg[-1]["t"] - seg[0]["t"]) / 3600.0
    else:
        elapsed_h = None
        epochs = [None] * len(seg)

    sailed_nm = _path_len_nm(pts)
    opt_nm = oracle.get("total_sailed_nm") or (_path_len_nm(opt_pts) if opt_pts else None)
    rhumb_nm = _hav_nm(start_pt, finish_pt)
    xtes = [x for x in (_xte_to_path(p, opt_pts) for p in pts) if x is not None]
    side = optimizer_first_beat_side(pts, start_pt, (marks[1][2], marks[1][3]) if len(marks) > 1 else None)

    oracle_h = oracle.get("total_hours")
    out = {
        "available": True, "source": track.get("source"), "boat": track.get("boat"),
        "fixes_scored": len(seg), "fixes_total": len(fixes),
        "elapsed_hours": round(elapsed_h, 2) if elapsed_h is not None else None,
        "oracle_hours": oracle_h,
        "time_behind_optimal_min": (round((elapsed_h - oracle_h) * 60) if (elapsed_h and oracle_h) else None),
        "sailed_nm": round(sailed_nm, 1),
        "optimal_nm": round(opt_nm, 1) if opt_nm else None,
        "rhumb_nm": round(rhumb_nm, 1),
        "extra_distance_pct": (round((sailed_nm / opt_nm - 1) * 100) if (opt_nm and opt_nm > 0) else None),
        "xte_mean_nm": round(sum(xtes) / len(xtes), 2) if xtes else None,
        "xte_p90_nm": round(sorted(xtes)[int(0.9 * (len(xtes) - 1))], 2) if xtes else None,
        "xte_max_nm": round(max(xtes), 2) if xtes else None,
        "side_worked": side,
    }
    pol = _polar_pct(seg, epochs, wf, polars)
    if pol:
        out.update(pol)
    bins = _performance_bins(seg, epochs, wf, polars)
    if bins:
        out["perf_bins"] = bins                # observed-vs-polar by (TWS,TWA) cell — Lab-4 mining input
    # honest self-checks (matches the project's degraded-signal ethos): flag non-physical readings
    # that usually mean favorable current / a soft rating / an oracle-window mismatch, not real perf.
    cav = []
    tb = out["time_behind_optimal_min"]
    if tb is not None and tb < -20:
        cav.append("boat faster than the oracle line — check the oracle wind window matches the "
                   "actual race (forecast-grade or wrong-day GRIB inflates this).")
    if out.get("polar_pct") and out["polar_pct"] > 110:
        cav.append(">100% of polar usually means a favorable current or a soft ORC rating, not real "
                   "overspeed; treat the helm number as a ceiling.")
    if cav:
        out["caveats"] = cav
    return out


def optimizer_first_beat_side(pts, start, first_mark, band_nm=0.4):
    """Which side of the first-beat rhumb the boat worked (mirrors judge._first_beat_side, on (lat,lon))."""
    if not pts or not first_mark:
        return "middle"
    beat = []
    for p in pts:
        beat.append(p)
        if _hav_nm(p, first_mark) < 0.5:
            break
    if len(beat) < 2:
        return "middle"
    xs = [optimizer._xtrack_nm(start[0], start[1], first_mark[0], first_mark[1], p[0], p[1]) for p in beat]
    ext = max(xs, key=abs)
    if abs(ext) < band_nm:
        return "middle"
    return "right" if ext > 0 else "left"


def _polar_pct(seg, epochs, wf, polars):
    """% of the flat-water polar the boat ACHIEVED — actual SOG vs the polar target at the realized
    wind (TWS/TWA from the actual-wind field) at each fix. The helm+conditions coaching number; needs
    the windfield + polars + derived SOG, else omitted."""
    if wf is None or polars is None or not getattr(wf, "loaded", False):
        return None
    P = polars
    ratios = []
    for f, ep in zip(seg, epochs):
        if ep is None or f.get("sog") is None or f.get("cog") is None:
            continue
        try:
            tws, twd = wf.wind_at(f["lat"], f["lon"], ep)
        except Exception:
            continue
        if not tws or tws <= 0:
            continue
        twa = abs(optimizer._wrap180(f["cog"] - twd))
        target = optimizer._polar_speed(P, tws, twa)
        if target and target > 0.5 and f["sog"] > 0.3:
            ratios.append(min(2.0, f["sog"] / target))
    if len(ratios) < 5:
        return None
    return {"polar_pct": round(100 * sum(ratios) / len(ratios)),
            "polar_samples": len(ratios)}


_BIN_MIN_SAMPLES, _BIN_PCTILE = 4, 80.0       # min slices to trust a cell; "best achievable" percentile


def _point_of_sail(twa):
    return "upwind" if twa <= 70 else ("reaching" if twa <= 120 else "downwind")


def _pctile(xs, q):
    s = sorted(xs)
    if not s:
        return None
    i = (q / 100.0) * (len(s) - 1)
    lo = int(i)
    return s[lo] if lo + 1 >= len(s) else s[lo] + (s[lo + 1] - s[lo]) * (i - lo)


def _performance_bins(seg, epochs, wf, polars):
    """Observed STW vs the polar target, snapped to the ORC cert's OWN (TWS,TWA) grid cells — the
    Lab-4 refined-polar input. Snapping to the cert grid (not arbitrary bins) is what lets an approved
    adjustment line up 1:1 with the cell the optimizer samples (`_polar_speed` nearest-neighbour), so
    the overlay actually bites. A high percentile (80th) of observed STW per cell = 'best achievable'
    (rejects lulls/steering scatter). Needs the actual-wind field + ≥ a few samples/cell, else []."""
    if wf is None or polars is None or not getattr(wf, "loaded", False):
        return []
    cells = {}            # (cert_tws, cert_twa) -> [observed stw]
    for f, ep in zip(seg, epochs):
        if ep is None or f.get("sog") is None or f.get("cog") is None or f["sog"] <= 0.3:
            continue
        try:
            tws, twd = wf.wind_at(f["lat"], f["lon"], ep)
        except Exception:
            continue
        if not tws or tws <= 0:
            continue
        twa = abs(optimizer._wrap180(f["cog"] - twd))
        if twa < 30:
            continue
        cell = min(polars, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))   # nearest cert cell
        if abs(cell[0] - tws) > 3.0 or abs(cell[1] - twa) > 18.0:             # too far → off-grid, skip
            continue
        cells.setdefault((cell[0], cell[1], cell[2]), []).append(f["sog"])
    out = []
    for (tws_c, twa_c, target), stws in sorted(cells.items()):
        if len(stws) < _BIN_MIN_SAMPLES or not target or target <= 0.5:
            continue
        best = _pctile(stws, _BIN_PCTILE)
        out.append({"tws": tws_c, "twa": twa_c, "point_of_sail": _point_of_sail(twa_c),
                    "samples": len(stws), "best_stw": round(best, 2),
                    "target_stw": round(target, 2), "pct": round(100 * best / target)})
    return out


# ---- persistence (one stored track per race; '_'-prefixed so the race library skips it) --------
def _path(race_id):
    rid = "".join(c for c in str(race_id).lower() if c.isalnum() or c in "_-")
    return os.path.join(TRACK_DIR, f"_track_{rid}.json")


def save_track(race_id, track):
    os.makedirs(TRACK_DIR, exist_ok=True)
    meta = {"race_id": race_id, "source": track.get("source"), "boat": track.get("boat"),
            "n": track.get("n") or len(track.get("fixes") or []),
            "matched_by": track.get("matched_by")}
    with open(_path(race_id), "w") as fh:
        json.dump({**meta, "fixes": track.get("fixes") or []}, fh)
    return meta


def load_track(race_id):
    try:
        with open(_path(race_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def clear_track(race_id):
    try:
        os.remove(_path(race_id))
        return True
    except OSError:
        return False
