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
    the boat profile's `mmsi`, else the race's `own.mmsi`) is taken out of the list. ONLY a
    configured MMSI is ever dropped. A target within ECHO_RANGE_M that moves with us is flagged
    `possible_echo: true`, and that is all: it keeps its full CPA/TCPA and alarms like any other
    contact (a boat 40 m astern overtaking at 1 kn is a real collision). The copilot adds a quiet
    hint to set OWN_MMSI.
  * A STALE OWN FIX IS NO FIX. Own position older than OWN_FIX_MAX_AGE_S (wall clock) — the age of
    the very reading used (`latest_sample`: value and time from one priority pick) — gives
    `own_fix: false` with the reason, so the copilot says the watch is off instead of computing
    CPA against where the boat was. 0 turns the age check off (a replay of an old archive).
  * TARGETS ARE MOVED TO NOW. Each report is dead-reckoned by its age along its SOG/COG. A target
    with no SOG or COG is `motion: "unknown"`: no CPA is claimed for it (it is not "stopped").
  * A DROPPED AIS LINK IS BLIND. Onboard, targets arrive only over the engine's live Signal K
    link (the archive holds own ship only), so `ais_link_down` (not connected, or silent for
    AIS_LINK_STALE_S) is reported apart from the own fix, which the archive can keep fresh.
  * A SILENT FEED IS NOT AN EMPTY SEA, BUT NOT A BLIND WATCH EITHER. `feed_silent` is true when we
    are under way and nothing at all has been heard on AIS for AIS_SILENT_MIN (our own echo counts
    as heard): open water can be empty, so it is a "check the AIS" hint. `receiver_missing` is the
    real failure: the transceiver's own source has gone quiet on the bus while other data flows
    (onboard only; None = can't tell).
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
# A fix stamped more than this into the future is invalid (a clock step), not "age 0".
FUTURE_SKEW_S = _envf("AIS_FUTURE_SKEW_S", 5)
# The engine's live Signal K link (its only AIS input onboard) silent this long → AIS link down.
AIS_LINK_STALE_S = _envf("AIS_LINK_STALE_S", 30)
# The own-echo hint: a target this close that moves with us may be us. A hint only, never a filter.
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


def _latest_sample(path):
    """(value, epoch) of the own-ship reading actually used for `path`, from the data source's
    single priority pick (value and time can't come from different sources). epoch is None when
    the source can't say (a stub with only latest_value)."""
    src = datasource.active()
    fn = getattr(src, "latest_sample", None)
    if fn is None:
        v = src.latest_value(path)
        return (float(v) if v is not None else None), None
    smp = fn(path)
    if not smp or smp[0] is None:
        return None, None
    return float(smp[0]), (float(smp[1]) if smp[1] is not None else None)


# The last own fix seen, to derive speed/course when SOG/COG are missing: (lat, lon, epoch).
_PREV_FIX = None
DERIVE_MIN_S, DERIVE_MAX_S = 2.0, 120.0


def _motion_sample(path, now):
    """An own SOG/COG reading, or None when missing, stamped in the future, or older than the fix
    age limit (a stale speed is not a speed)."""
    v, e = _latest_sample(path)
    if v is None:
        return None
    if e is not None and (e > now + FUTURE_SKEW_S
                          or (OWN_FIX_MAX_AGE_S > 0 and now - e > OWN_FIX_MAX_AGE_S)):
        return None
    return v


def _own_ship():
    """Own position (deg) + motion (kn / deg true), best-available freshest source, plus the age
    of the position (`age_s`, None when the source can't say). With no usable SOG/COG, speed and
    course are derived from two successive own fixes (DERIVE_MIN_S..DERIVE_MAX_S apart) when we
    have them (`motion_source: "derived"`); otherwise they stay None — unknown, never "stopped"."""
    global _PREV_FIX
    lat, e_lat = _latest_sample("navigation.position.latitude")
    lon, e_lon = _latest_sample("navigation.position.longitude")
    now = _time.time()
    sog = _motion_sample("navigation.speedOverGround", now)
    cog = _motion_sample("navigation.courseOverGroundTrue", now)
    # the older of the two halves of the fix is the fix's age
    e = min(e_lat, e_lon) if e_lat is not None and e_lon is not None else None
    if e is not None and max(e_lat, e_lon) > now + FUTURE_SKEW_S:
        # stamped in the future (e.g. archive rows from before a backward clock step): not a fix
        # at all — never "age 0". The watch says "no GPS fix" until a real one arrives.
        lat = lon = e = None
    sog_kn = sog * _MS_TO_KN if sog is not None else None
    cog_deg = math.degrees(cog) % 360 if cog is not None else None
    src = "measured" if sog_kn is not None else None
    if lat is not None and lon is not None and e is not None:
        prev = _PREV_FIX
        if sog_kn is None and prev is not None and DERIVE_MIN_S <= e - prev[2] <= DERIVE_MAX_S:
            de, dn = _enu_nm(prev[0], prev[1], lat, lon)
            sog_kn = math.hypot(de, dn) / ((e - prev[2]) / 3600.0)
            if cog_deg is None and sog_kn > 0:
                cog_deg = (math.degrees(math.atan2(de, dn)) + 360) % 360
            src = "derived"
        if prev is None or e > prev[2]:
            _PREV_FIX = (lat, lon, e)
    return {
        "lat": lat, "lon": lon, "sog": sog_kn, "cog": cog_deg, "motion_source": src,
        "age_s": max(0.0, now - e) if e is not None else None,
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


def _link_down():
    """(down, age_s): is the data source's live AIS input link down? (None, None) when the source
    has no such link (the cloud reads AIS from Postgres). Down = switched off, not connected, or
    no message for AIS_LINK_STALE_S. The archive keeps own-ship contexts only (the archiver drops
    other vessels), so there is no fallback for targets: a down link is a blind AIS watch."""
    fn = getattr(datasource.active(), "live_link", None)
    if fn is None:
        return None, None
    try:
        lk = fn() or {}
    except Exception:           # noqa: BLE001
        return True, None
    age = lk.get("last_msg_age_s")
    down = (not lk.get("enabled") or not lk.get("connected") or age is None
            or age > AIS_LINK_STALE_S)
    return bool(down), (round(age) if age is not None else None)


def _receiver_missing(last_heard_s):
    """True when the data source can tell the boat's AIS transceiver has gone quiet on the bus for
    AIS_SILENT_MIN while other data flows, and no target has been heard either. None = can't tell
    (the cloud, no transceiver known, or the live link is down)."""
    fn = getattr(datasource.active(), "ais_receiver_age_s", None)
    if fn is None:
        return None
    try:
        age = fn()
    except Exception:           # noqa: BLE001
        return None
    if age is None:
        return None
    limit = AIS_SILENT_MIN * 60
    return bool(age > limit and (last_heard_s is None or last_heard_s > limit))


def _threat_key(t):
    """Closing contacts first, soonest TCPA first; then unknown motion by range; then the rest by
    range."""
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

    link_down, link_age = _link_down()
    rx_missing = None if link_down else _receiver_missing(last_heard_s)
    base["ais_link_down"], base["ais_link_age_s"] = link_down, link_age

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
                    "motion": "known" if _motion_known(r) else "unknown", "possible_echo": False}
                   for r, a in listed]
        return {**base, "count": len(targets), "own_fix": False, "own_fix_reason": reason,
                "underway": None, "feed_silent": False, "receiver_missing": rx_missing,
                "targets": targets,
                "note": ("No position yet, so no range or CPA." if reason == "no GPS fix" else
                         f"GPS fix is {base['own_fix_age_s']} s old, so no range or CPA.")}

    # own ship to now as well (the fix can be up to OWN_FIX_MAX_AGE_S old): same clock as the targets
    own = dict(own)
    own["lat"], own["lon"] = _dead_reckon(own["lat"], own["lon"], own["sog"], own["cog"], own.get("age_s"))
    # None = we don't know our speed (no SOG, stale, nothing to derive it from). False is kept for
    # a MEASURED speed below the threshold; every consumer treats None as "under way" for safety.
    underway = None if own["sog"] is None else own["sog"] >= UNDERWAY_KN
    feed_silent = bool(underway is not False
                       and (last_heard_s is None or last_heard_s > AIS_SILENT_MIN * 60))
    if rx_missing or link_down:
        feed_silent = False                  # the louder signal says it

    targets = []
    for r, age in listed:
        lat, lon = _dead_reckon(r["lat"], r["lon"], r.get("sog"), r.get("cog"), age)
        tgt = {"lat": lat, "lon": lon, "sog": r.get("sog"), "cog": r.get("cog")}
        rng, brg, cpa, tcpa, closing = _cpa(own, tgt)
        if rng > max_range_nm:
            continue
        known = _motion_known(tgt)
        possible_echo = False
        if (known and rng * _M_PER_NM <= ECHO_RANGE_M and own["sog"] is not None
                and _rel_speed_kn(own, tgt) <= ECHO_REL_KN):
            possible_echo = True             # maybe us: a hint only, the geometry stands
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
            "possible_echo": possible_echo,
        })
    targets.sort(key=_threat_key)
    return {**base, "count": len(targets), "own_fix": True, "own_fix_reason": None,
            "underway": underway, "own_sog_kn": round(own["sog"], 1) if own["sog"] is not None else None,
            "own_motion_source": own.get("motion_source"),
            "feed_silent": feed_silent, "receiver_missing": rx_missing,
            "targets": targets}
