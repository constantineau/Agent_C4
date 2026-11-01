"""AIS targets — cloud-side range / bearing / CPA / TCPA.

The boat (em-trak B951, Class B) hears other vessels on the N2K bus; Signal K surfaces each
as its own vessel context. The Pi uplink forwards the RAW target observation only
(mmsi, name, lat, lon, sog, cog) to `ais_targets` — it does NOT compute geometry, in keeping
with the collect-everything paradigm (the boat is dumb; the cloud reasons).

This module computes range, bearing, and the closest point of approach (CPA / TCPA) here,
against the boat's OWN latest position+motion, so the geometry always reflects current
own-ship state rather than whatever was true when the target was heard.

Both the raw targets and own-ship state are read through `datasource.active()`, so the SAME
geometry runs in the CLOUD (`CloudSource`: the `ais_targets` table + `telemetry_raw`) and
ONBOARD the Pi (`OnboardSource`: other-vessel Signal K contexts + the live cache) — collision
/ fleet awareness is always legal in-race (own AIS receiver, own computer).

CPA/TCPA use a local flat-plane (equirectangular) relative-motion model in nautical miles —
fine at the ranges that matter (a few nm) and matches navigator.py's projection.

What makes the watch trustworthy (2026-11-02 system review, collision items 1, 4, 5):
  * OWN SHIP. Our own AIS transmission can come back as a target (MMSI 316053364 at 0.0 nm raised
    "Collision risk" at the dock). A target whose MMSI is ours (`own_mmsi()`: env OWN_MMSI, else
    the boat profile's `mmsi`, else the race's `own.mmsi`) is taken out of the list. A target
    within ECHO_RANGE_M that moves with us is flagged `own_ship: "echo?"` and never alarms, but it
    stays in the list so the copilot can say so.
  * A STALE OWN FIX IS NO FIX. Own position older than OWN_FIX_MAX_AGE_S (wall clock) gives
    `own_fix: false` with the reason, so the copilot says the watch is off instead of computing
    CPA against where the boat was. 0 turns the age check off (a replay of an old archive).
  * TARGETS ARE MOVED TO NOW. Each report is dead-reckoned by its age along its SOG/COG. A target
    with no SOG or COG is `motion: "unknown"`: no CPA is claimed for it (it is not "stopped").
  * A SILENT FEED IS NOT AN EMPTY SEA. `feed_silent` is true when we are under way and nothing at
    all has been heard on AIS for AIS_SILENT_MIN (our own echo counts as heard).
"""
import math
import os
import time as _time

from . import datasource

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
AIS_WINDOW_MIN = 5          # a target unheard this long is dropped
_MS_TO_KN = 1.943844
_EPS_KN = 0.05              # below this relative speed there's effectively no CPA
_M_PER_NM = 1852.0


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# Own position older than this (wall clock) is not a fix. Onboard the GPS writes several times a
# second; the cloud sees it through the uplink, so it gets more slack. 0 = no age check (replay).
OWN_FIX_MAX_AGE_S = _envf("AIS_OWN_FIX_MAX_AGE_S",
                          30 if os.environ.get("DATA_SOURCE", "cloud").strip().lower() == "onboard"
                          else 120)
# Nothing heard on AIS for this long while under way → the feed is called silent.
AIS_SILENT_MIN = _envf("AIS_SILENT_MIN", 15)
UNDERWAY_KN = _envf("AIS_UNDERWAY_KN", 2.0)
# The own-echo heuristic: a target this close that moves with us is probably us.
ECHO_RANGE_M = _envf("AIS_ECHO_RANGE_M", 50)
ECHO_REL_KN = _envf("AIS_ECHO_REL_KN", 1.5)


def _mmsi_str(x):
    s = str(x or "").strip()
    return s if s.isdigit() and len(s) == 9 else None


def own_mmsi():
    """Our own MMSI, from config: env OWN_MMSI, else the boat profile the Lab pushed aboard
    (`mmsi`), else the loaded race homework (`own.mmsi`). None when nobody has set it."""
    m = _mmsi_str(os.environ.get("OWN_MMSI"))
    if m:
        return m
    try:
        from . import orinassets
        rec = orinassets._read() or {}
        m = _mmsi_str(((rec.get("assets") or {}).get("boat") or {}).get("mmsi"))
        if m:
            return m
    except Exception:           # noqa: BLE001 — a missing/odd assets file just means "not set here"
        pass
    try:
        m = _mmsi_str(((datasource.active().get_fleet() or {}).get("own") or {}).get("mmsi"))
        if m:
            return m
    except Exception:           # noqa: BLE001
        pass
    return None


def _latest(path):
    """Freshest numeric value (any source) for an own-ship path, SI units."""
    v = datasource.active().latest_value(path)
    return float(v) if v is not None else None


def _latest_epoch(path):
    """Epoch of the newest own-ship sample for `path`, or None (unknown / not supported)."""
    fn = getattr(datasource.active(), "latest_epoch", None)
    if fn is None:
        return None
    try:
        e = fn(path)
    except Exception:           # noqa: BLE001 — an unreadable age is treated as unknown
        return None
    return float(e) if e is not None else None


def _own_ship():
    """Own position (deg) + motion (kn / deg true), best-available freshest source, plus the age
    of the position (`age_s`, None when the source can't say)."""
    lat = _latest("navigation.position.latitude")
    lon = _latest("navigation.position.longitude")
    sog = _latest("navigation.speedOverGround")
    cog = _latest("navigation.courseOverGroundTrue")
    e = _latest_epoch("navigation.position.latitude")
    return {
        "lat": lat, "lon": lon,
        "sog": sog * _MS_TO_KN if sog is not None else None,
        "cog": math.degrees(cog) % 360 if cog is not None else None,
        "age_s": max(0.0, _time.time() - e) if e is not None else None,
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


def _motion_known(t):
    return t.get("sog") is not None and t.get("cog") is not None


def _dead_reckon(lat, lon, sog, cog, age_s):
    """Move a reported position forward by `age_s` along SOG (kn) / COG (deg true)."""
    if sog is None or cog is None or not age_s or age_s <= 0:
        return lat, lon
    ve, vn = _vel_nm_h(sog, cog)
    h = age_s / 3600.0
    return (lat + vn * h / 60.0,
            lon + ve * h / (60.0 * max(0.01, math.cos(math.radians(lat)))))


def _cpa(own, tgt):
    """Range/bearing now + CPA (nm) and TCPA (min) of `tgt` relative to `own`.

    Positive TCPA = approaching (CPA in the future); negative/zero = opening or already past.
    Returns range_nm, bearing, cpa_nm, tcpa_min, closing(bool). A target with no SOG or COG has
    unknown motion: range and bearing only, CPA/TCPA/closing None (never "stopped")."""
    de, dn = _enu_nm(own["lat"], own["lon"], tgt["lat"], tgt["lon"])
    rng = math.hypot(de, dn)
    brg = (math.degrees(math.atan2(de, dn)) + 360) % 360
    if not _motion_known(tgt):
        return rng, brg, None, None, None

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


def _rel_speed_kn(own, tgt):
    ove, ovn = _vel_nm_h(own["sog"], own["cog"])
    tve, tvn = _vel_nm_h(tgt.get("sog"), tgt.get("cog"))
    return math.hypot(tve - ove, tvn - ovn)


def _latest_targets(max_age_min):
    """Latest raw observation per MMSI within the window, via the active data source.

    Each row is a dict with mmsi/name/lat/lon/sog(kn)/cog(deg true)/time(epoch), plus pos_time
    (epoch of the position report) where the source keeps it."""
    return datasource.active().ais_targets(max_age_min)


def _threat_key(t):
    """Closing contacts first, soonest TCPA first; then unknown motion by range; then the rest by
    range; a probable own echo last."""
    if t.get("own_ship"):
        return (3, t["range_nm"] if t.get("range_nm") is not None else 1e9, 0)
    if t.get("closing"):
        return (0, t["tcpa_min"], t["cpa_nm"])
    if t.get("motion") == "unknown":
        return (1, t["range_nm"] if t.get("range_nm") is not None else 1e9, 0)
    return (2, t["range_nm"] if t.get("range_nm") is not None else 1e9, 0)


def get_ais_targets(max_range_nm: float = 12):
    """Current AIS traffic with range/bearing and freshly computed CPA/TCPA vs own ship.

    Sorted most-threatening first: closing contacts by TCPA, then unknown-motion contacts, then the
    rest by range. Also says whether the watch can see (`own_fix`, `own_fix_reason`,
    `feed_silent`) so a blind watch never reads as an empty sea."""
    now = _time.time()
    own = _own_ship()
    me = own_mmsi()
    rows = _latest_targets(max(AIS_WINDOW_MIN, AIS_SILENT_MIN))
    heard = [r.get("time") for r in rows if r.get("time") is not None]
    last_heard_s = round(max(0.0, now - max(heard))) if heard else None
    own_seen = None
    listed = []
    for r in rows:
        if me and _mmsi_str(r.get("mmsi")) == me:
            own_seen = {"mmsi": me}          # it's us: out of the list (still proves the feed is alive)
            continue
        pt = r.get("pos_time") or r.get("time")
        age = max(0.0, now - pt) if pt is not None else None
        if age is not None and age > AIS_WINDOW_MIN * 60:
            continue                         # position too old to place the target
        listed.append((r, age))

    base = {"max_range_nm": max_range_nm, "own_mmsi": me, "own_seen": own_seen,
            "last_heard_s": last_heard_s, "own_fix_age_s":
            round(own["age_s"]) if own.get("age_s") is not None else None}

    reason = None
    if own["lat"] is None or own["lon"] is None:
        reason = "no GPS fix"
    elif OWN_FIX_MAX_AGE_S > 0 and own.get("age_s") is not None and own["age_s"] > OWN_FIX_MAX_AGE_S:
        reason = "GPS fix stale"
    if reason:
        # No usable own fix — we can still list targets, but no geometry.
        targets = [{"mmsi": r["mmsi"], "name": r["name"], "sog": r["sog"], "cog": r["cog"],
                    "range_nm": None, "bearing": None, "cpa_nm": None, "tcpa_min": None,
                    "closing": None, "age_s": round(a) if a is not None else None,
                    "motion": "known" if _motion_known(r) else "unknown", "own_ship": None}
                   for r, a in listed]
        return {**base, "count": len(targets), "own_fix": False, "own_fix_reason": reason,
                "underway": None, "feed_silent": False, "targets": targets,
                "note": ("No position yet, so no range or CPA." if reason == "no GPS fix" else
                         f"GPS fix is {base['own_fix_age_s']} s old, so no range or CPA.")}

    # own ship to now as well (the fix can be up to OWN_FIX_MAX_AGE_S old): same clock as the targets
    own = dict(own)
    own["lat"], own["lon"] = _dead_reckon(own["lat"], own["lon"], own["sog"], own["cog"], own.get("age_s"))
    underway = own["sog"] is not None and own["sog"] >= UNDERWAY_KN
    feed_silent = bool(underway and (last_heard_s is None or last_heard_s > AIS_SILENT_MIN * 60))

    targets = []
    for r, age in listed:
        lat, lon = _dead_reckon(r["lat"], r["lon"], r.get("sog"), r.get("cog"), age)
        tgt = {"lat": lat, "lon": lon, "sog": r.get("sog"), "cog": r.get("cog")}
        rng, brg, cpa, tcpa, closing = _cpa(own, tgt)
        if rng > max_range_nm:
            continue
        known = _motion_known(tgt)
        own_ship = None
        if (known and rng * _M_PER_NM <= ECHO_RANGE_M and own["sog"] is not None
                and _rel_speed_kn(own, tgt) <= ECHO_REL_KN):
            own_ship = "echo?"               # right on top of us and moving with us: probably us
            cpa, tcpa, closing = None, None, False
        targets.append({
            "mmsi": r["mmsi"], "name": r["name"],
            "sog": round(r["sog"], 1) if r.get("sog") is not None else None,
            "cog": round(r["cog"]) if r.get("cog") is not None else None,
            "range_nm": round(rng, 2), "range_m": round(rng * _M_PER_NM), "bearing": round(brg),
            "cpa_nm": round(cpa, 2) if cpa is not None else None,
            "tcpa_min": round(tcpa, 1) if tcpa is not None else None,
            "closing": closing,
            "motion": "known" if known else "unknown",
            "age_s": round(age) if age is not None else None,
            "own_ship": own_ship,
        })
    targets.sort(key=_threat_key)
    return {**base, "count": len(targets), "own_fix": True, "own_fix_reason": None,
            "underway": underway, "feed_silent": feed_silent, "targets": targets}
