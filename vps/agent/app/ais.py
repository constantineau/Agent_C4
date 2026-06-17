"""AIS targets — cloud-side range / bearing / CPA / TCPA.

The boat (em-trak B951, Class B) hears other vessels on the N2K bus; Signal K surfaces each
as its own vessel context. The Pi uplink forwards the RAW target observation only
(mmsi, name, lat, lon, sog, cog) to `ais_targets` — it does NOT compute geometry, in keeping
with the collect-everything paradigm (the boat is dumb; the cloud reasons).

This module computes range, bearing, and the closest point of approach (CPA / TCPA) here,
against the boat's OWN latest position+motion from telemetry_raw, so the geometry always
reflects current own-ship state rather than whatever was true when the target was heard.

CPA/TCPA use a local flat-plane (equirectangular) relative-motion model in nautical miles —
fine at the ranges that matter (a few nm) and matches navigator.py's projection.
"""
import math
import os

from .db import pool

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AIS_WINDOW_MIN = 5          # a target unheard this long is dropped
_MS_TO_KN = 1.943844
_EPS_KN = 0.05              # below this relative speed there's effectively no CPA


def _latest(path):
    """Freshest numeric value (any source) for an own-ship path, SI units."""
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT value FROM telemetry_raw WHERE boat_id=%s AND path=%s "
            "AND value IS NOT NULL ORDER BY time DESC LIMIT 1", (BOAT_ID, path),
        ).fetchone()
    return float(r["value"]) if r else None


def _own_ship():
    """Own position (deg) + motion (kn / deg true), best-available freshest source."""
    lat = _latest("navigation.position.latitude")
    lon = _latest("navigation.position.longitude")
    sog = _latest("navigation.speedOverGround")
    cog = _latest("navigation.courseOverGroundTrue")
    return {
        "lat": lat, "lon": lon,
        "sog": sog * _MS_TO_KN if sog is not None else None,
        "cog": math.degrees(cog) % 360 if cog is not None else None,
    }


def _enu_nm(lat0, lon0, lat, lon):
    """Target position relative to own ship, in nm (east, north)."""
    dn = (lat - lat0) * 60.0
    de = (lon - lon0) * 60.0 * math.cos(math.radians(lat0))
    return de, dn


def _vel_nm_h(sog, cog):
    """(east, north) velocity components in nm/h from speed (kn) + course (deg true)."""
    if sog is None or cog is None:
        return 0.0, 0.0
    r = math.radians(cog)
    return sog * math.sin(r), sog * math.cos(r)


def _cpa(own, tgt):
    """Range/bearing now + CPA (nm) and TCPA (min) of `tgt` relative to `own`.

    Positive TCPA = approaching (CPA in the future); negative/zero = opening or already past.
    Returns range_nm, bearing, cpa_nm, tcpa_min, closing(bool)."""
    de, dn = _enu_nm(own["lat"], own["lon"], tgt["lat"], tgt["lon"])
    rng = math.hypot(de, dn)
    brg = (math.degrees(math.atan2(de, dn)) + 360) % 360

    ove, ovn = _vel_nm_h(own["sog"], own["cog"])
    tve, tvn = _vel_nm_h(tgt.get("sog"), tgt.get("cog"))
    rve, rvn = tve - ove, tvn - ovn                      # target velocity relative to own
    rel_spd2 = rve * rve + rvn * rvn

    if rel_spd2 < _EPS_KN * _EPS_KN:                     # no relative motion → never closes
        return rng, brg, rng, None, False

    tcpa_h = -(de * rve + dn * rvn) / rel_spd2
    if tcpa_h <= 0:                                      # CPA already passed — they're opening
        return rng, brg, rng, round(tcpa_h * 60.0, 1), False
    ce, cn = de + rve * tcpa_h, dn + rvn * tcpa_h
    return rng, brg, math.hypot(ce, cn), tcpa_h * 60.0, True


def _latest_targets(max_age_min):
    """Latest raw observation per MMSI within the window."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT DISTINCT ON (mmsi) mmsi, name, lat, lon, sog, cog, time "
            "FROM ais_targets WHERE boat_id=%s AND time > now()-%s::interval "
            "AND lat IS NOT NULL AND lon IS NOT NULL "
            "ORDER BY mmsi, time DESC", (BOAT_ID, f"{int(max_age_min)} minutes"),
        ).fetchall()


def get_ais_targets(max_range_nm: float = 12):
    """Current AIS traffic with range/bearing and freshly computed CPA/TCPA vs own ship.

    Sorted most-threatening first (smallest CPA among closing targets, then range)."""
    own = _own_ship()
    rows = _latest_targets(AIS_WINDOW_MIN)
    if own["lat"] is None or own["lon"] is None:
        # No own fix — we can still list targets, but no geometry.
        targets = [{"mmsi": r["mmsi"], "name": r["name"], "sog": r["sog"], "cog": r["cog"],
                    "range_nm": None, "bearing": None, "cpa_nm": None, "tcpa_min": None,
                    "closing": None} for r in rows]
        return {"count": len(targets), "max_range_nm": max_range_nm,
                "own_fix": False, "targets": targets,
                "note": "No own-ship position fix — range/CPA unavailable."}

    targets = []
    for r in rows:
        tgt = {"lat": r["lat"], "lon": r["lon"], "sog": r["sog"], "cog": r["cog"]}
        rng, brg, cpa, tcpa, closing = _cpa(own, tgt)
        if rng > max_range_nm:
            continue
        targets.append({
            "mmsi": r["mmsi"], "name": r["name"],
            "sog": round(r["sog"], 1) if r["sog"] is not None else None,
            "cog": round(r["cog"]) if r["cog"] is not None else None,
            "range_nm": round(rng, 2), "bearing": round(brg),
            "cpa_nm": round(cpa, 2), "tcpa_min": round(tcpa, 1) if tcpa is not None else None,
            "closing": closing,
        })
    # threat order: closing targets by CPA, then everyone by range
    targets.sort(key=lambda t: (not t["closing"], t["cpa_nm"] if t["cpa_nm"] is not None else 1e9,
                                t["range_nm"]))
    return {"count": len(targets), "max_range_nm": max_range_nm, "own_fix": True,
            "targets": targets}
