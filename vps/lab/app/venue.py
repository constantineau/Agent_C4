"""Venue identity for model-skill weighting (Phase 2).

A "venue" groups races that sail the same water so their weather-model skill history accumulates
together (bayviewmack2025 + 2026 -> one venue). Keying is automatic: the course bbox centroid snapped
to a ~0.5 deg cell, overridable by an explicit `venue_tag` on the race. Each venue resolves to the
nearest OBSERVED-wind station (METAR or NDBC buoy) that anchors the forecast-vs-observed scoring; a
venue with no station within reach simply gets no skill weighting (identity — safe default).

See docs/MODEL_SKILL_WEIGHTING.md.
"""
from __future__ import annotations

import math

CELL_DEG = 0.5              # venue cell size for auto-keying
STATION_MAX_NM = 60.0      # a venue must have an obs station within this range to be scoreable

# Curated observed-wind stations near our racing waters (Great Lakes first; extend as venues grow).
# kind: "metar" (ICAO, Iowa State ASOS archive) or "ndbc" (buoy id, realtime2 + historical archive).
STATIONS = [
    # --- METAR (shore, year-round) ---
    {"id": "KAPN", "kind": "metar", "lat": 45.07, "lon": -83.56, "name": "Alpena, MI"},
    {"id": "KANJ", "kind": "metar", "lat": 46.48, "lon": -84.36, "name": "Sault Ste Marie, MI"},
    {"id": "KTVC", "kind": "metar", "lat": 44.74, "lon": -85.58, "name": "Traverse City, MI"},
    {"id": "KMBL", "kind": "metar", "lat": 44.27, "lon": -86.25, "name": "Manistee, MI"},
    {"id": "KMKG", "kind": "metar", "lat": 43.17, "lon": -86.24, "name": "Muskegon, MI"},
    {"id": "KMKE", "kind": "metar", "lat": 42.95, "lon": -87.90, "name": "Milwaukee, WI"},
    {"id": "KGRB", "kind": "metar", "lat": 44.48, "lon": -88.13, "name": "Green Bay, WI"},
    {"id": "KCGX", "kind": "metar", "lat": 41.86, "lon": -87.61, "name": "Chicago (Meigs), IL"},
    {"id": "KDTW", "kind": "metar", "lat": 42.21, "lon": -83.35, "name": "Detroit, MI"},
    {"id": "KESC", "kind": "metar", "lat": 45.72, "lon": -87.09, "name": "Escanaba, MI"},
    {"id": "KPLN", "kind": "metar", "lat": 45.57, "lon": -84.80, "name": "Pellston, MI"},
    # --- NDBC buoys (over-water, seasonal on the lakes) ---
    {"id": "45003", "kind": "ndbc", "lat": 45.35, "lon": -82.84, "name": "N Lake Huron buoy"},
    {"id": "45008", "kind": "ndbc", "lat": 44.28, "lon": -82.42, "name": "S Lake Huron buoy"},
    {"id": "45002", "kind": "ndbc", "lat": 45.34, "lon": -86.41, "name": "N Lake Michigan buoy"},
    {"id": "45007", "kind": "ndbc", "lat": 42.67, "lon": -87.02, "name": "S Lake Michigan buoy"},
]


def _haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065   # nautical miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def nearest_station(lat, lon, prefer=None, max_nm=STATION_MAX_NM):
    """Closest curated station to a point (optionally preferring a kind), or None if none in range."""
    cands = [s for s in STATIONS if prefer is None or s["kind"] == prefer] or STATIONS
    best, bd = None, max_nm + 1
    for s in cands:
        d = _haversine_nm(lat, lon, s["lat"], s["lon"])
        if d < bd:
            bd, best = d, s
    return dict(best, dist_nm=round(bd, 1)) if best else None


def _cell_key(lat, lon):
    la = round(lat / CELL_DEG) * CELL_DEG
    lo = round(lon / CELL_DEG) * CELL_DEG
    return f"v{abs(la):04.1f}{'N' if la >= 0 else 'S'}{abs(lo):05.1f}{'E' if lo >= 0 else 'W'}"


def resolve_from_bbox(bbox, tag=None, name=None):
    """Venue record from a course bbox=(n,s,w,e). `tag` (race.venue_tag) overrides the auto cell key.
    Returns {key, label, centroid:[lat,lon], station|None}. station=None -> not skill-scoreable."""
    n, s, w, e = bbox
    clat, clon = (n + s) / 2.0, (w + e) / 2.0
    key = tag or _cell_key(clat, clon)
    st = nearest_station(clat, clon)
    return {"key": key, "label": name or key, "centroid": [round(clat, 3), round(clon, 3)],
            "station": st}


def resolve(definition, course_id, bbox=None):
    """Venue for a race definition + course. Uses `definition['venue_tag']` if present, else the bbox
    centroid cell. `bbox` may be passed in (from optimizer.course_bbox) to avoid a re-import."""
    if bbox is None:
        from .optimizer import course_bbox
        bbox = course_bbox(definition, course_id)
    if not bbox:
        return None
    return resolve_from_bbox(bbox, tag=(definition or {}).get("venue_tag"),
                             name=(definition or {}).get("name"))
