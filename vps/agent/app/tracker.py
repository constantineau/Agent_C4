"""Over-the-horizon public race tracker as a fleet source (perflab item-6 remainder).

A public race tracker (YB Races / TracTrac-style, e.g. bycmack.com/tracking) carries the WHOLE
fleet's positions — including boats over the horizon and boats not running AIS — published with a
deliberate delay. Under RRS 41 it is common data "available to all boats"; whether it may be used
ONBOARD is a PER-RACE rules question (`RaceDefinition.rules_profile.tracker_permitted`) — default
conservative (off). For Bayview Mackinac the user confirmed it's allowed + normal in-race; for other
races it must be checked in the SI (the gate lives in `fleet.get_fleet`, not here).

Architecturally identical to the GRIB/buoy sources: a best-effort onboard PULL of a common feed,
CACHED with a TTL so the per-poll fleet view never blocks on the network, with EVERY position
explicitly AGED + CONFIDENCE-REDUCED — the feed is delayed, so a fix is never shown as current. The
tracker also supplies boat IDENTITY, which is the lever on the AIS↔roster MMSI-match gap: an unmatched
AIS target near a roster boat's tracker fix can be resolved by position (done in `fleet.py`).

Pluggable providers turn a tracker's endpoint into normalized fixes. `generic_json` handles the common
case — a JSON/XHR endpoint behind the web UI — via a per-race field map; `sample` returns a fixture for
the bench (there is no live race). Reasoning stays onboard; this module only fetches + normalizes +
ages. A fix = {name, lat, lon, sog(kn)|None, cog(deg true)|None, time(epoch)}.
"""
import json
import math  # noqa: F401  (kept for parity with the rest of the fleet layer / future geo use)
import os
import re
import time
import urllib.request

# --- tunables ----------------------------------------------------------------
_REFRESH_S = float(os.environ.get("TRACKER_REFRESH_S", "120"))   # cache TTL — don't hammer the feed
_TIMEOUT_S = float(os.environ.get("TRACKER_TIMEOUT_S", "6"))     # best-effort fetch timeout (s)
_STALE_MIN = float(os.environ.get("TRACKER_STALE_MIN", "45"))    # age (min) at which confidence floors
_CONF_FLOOR = 0.1

# module-level cache: (provider, url) -> {"at": epoch, "fixes": [...], "error": str|None}
_CACHE = {}


def _norm(s):
    """Normalize a vessel name for matching: lowercase, drop non-alphanumerics (matches fleet._norm)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- providers: parse a tracker endpoint into normalized fixes ---------------
def _provider_generic_json(cfg, payload):
    """Common case: a JSON/XHR endpoint behind the tracker web UI returns a list of boat objects.
    `cfg.fields` maps our keys → the feed's field names (defaults to the obvious names); `cfg.list_path`
    dots into the list when the boats are nested (e.g. "data.boats")."""
    fld = cfg.get("fields") or {}
    f_name = fld.get("name", "name"); f_lat = fld.get("lat", "lat"); f_lon = fld.get("lon", "lon")
    f_sog = fld.get("sog", "sog"); f_cog = fld.get("cog", "cog"); f_time = fld.get("time", "time")
    node = payload
    for k in (cfg.get("list_path") or "").split("."):
        if k and isinstance(node, dict):
            node = node.get(k)
    fixes = []
    for o in (node or []):
        if not isinstance(o, dict):
            continue
        lat, lon = _f(o.get(f_lat)), _f(o.get(f_lon))
        if lat is None or lon is None:
            continue
        t = _f(o.get(f_time))
        fixes.append({"name": o.get(f_name), "lat": lat, "lon": lon,
                      "sog": _f(o.get(f_sog)), "cog": _f(o.get(f_cog)), "time": t})
    return fixes


def _provider_sample(cfg):
    """Bench fixture — there is no live race. A few roster boats near the Mackinac course, recently
    timestamped (minus a realistic feed delay) so the age/confidence path is exercised offline."""
    now = time.time()
    delay = float(cfg.get("delay_min") or 15) * 60.0
    return [
        {"name": "Windquest",  "lat": 45.10, "lon": -82.85, "sog": 7.8, "cog": 20, "time": now - delay},
        {"name": "Il Mostro",  "lat": 45.35, "lon": -82.70, "sog": 8.4, "cog": 15, "time": now - delay - 120},
        {"name": "Defiance",   "lat": 44.95, "lon": -83.00, "sog": 7.1, "cog": 25, "time": now - delay - 60},
    ]


_PROVIDERS_GENERIC = {"generic_json", "yb", "ybtracking", "tractrac", "bycmack"}


def _fetch(cfg):
    """Best-effort PULL of the tracker endpoint → (fixes, error). Never raises — a failed/absent feed
    yields ([], reason), exactly like a not-yet-posted GRIB; the fleet view proceeds without it."""
    provider = (cfg.get("provider") or "").strip().lower()
    if provider == "sample":
        return _provider_sample(cfg), None
    url = cfg.get("url")
    if not url:
        return [], "no tracker url configured"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SR33-AI-Navigator/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:                       # best-effort like GRIB — never break the fleet view
        return [], f"tracker fetch failed: {type(e).__name__}"
    if provider in _PROVIDERS_GENERIC:
        # yb/tractrac/bycmack all expose the boats as a JSON list; the per-race field map adapts the
        # exact key names (verify against the live endpoint — see the honest-gap note in fleet_blob).
        try:
            return _provider_generic_json(cfg, payload), None
        except Exception as e:
            return [], f"tracker parse failed: {type(e).__name__}"
    return [], f"unknown tracker provider {provider!r}"


def _age_conf(age_s):
    """Confidence that a delayed tracker fix still reflects reality: decays linearly to a floor as the
    fix ages past TRACKER_STALE_MIN (the over-the-horizon picture degrades the older it is)."""
    if _STALE_MIN <= 0:
        return _CONF_FLOOR
    return max(_CONF_FLOOR, min(1.0, 1.0 - (age_s / 60.0) / _STALE_MIN))


def positions(cfg, now=None):
    """Cached tracker fixes, AGED + CONFIDENCE-REDUCED. Returns a status dict:
      {available, positions:[{name,lat,lon,sog,cog,time,age_s,confidence}], fetched_at, error,
       delay_min, note}
    Cached for TRACKER_REFRESH_S so the per-poll fleet view never blocks on the network (the feed is
    delayed by minutes — a 2-min cache costs nothing)."""
    now = now or time.time()
    cfg = cfg or {}
    key = (cfg.get("provider"), cfg.get("url"))
    cached = _CACHE.get(key)
    if cached is None or (now - cached["at"]) > _REFRESH_S:
        fixes, error = _fetch(cfg)
        cached = {"at": now, "fixes": fixes, "error": error}
        _CACHE[key] = cached
    out = []
    for fx in cached["fixes"]:
        t = fx.get("time") or cached["at"]
        age = max(0.0, now - t)
        out.append({**fx, "time": t, "age_s": round(age), "confidence": round(_age_conf(age), 2)})
    return {"available": bool(out), "positions": out, "fetched_at": cached["at"],
            "error": cached["error"], "delay_min": cfg.get("delay_min"),
            "note": "Public tracker is DELAYED — positions are aged + confidence-reduced; use for the "
                    "over-the-horizon picture, not live tactical calls."}


def _reset_cache():
    """Test hook — drop the fetch cache so a unit test sees a fresh fetch."""
    _CACHE.clear()
