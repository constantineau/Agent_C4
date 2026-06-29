"""Monitor — shore-side live view during the race.

Two sources, both shore-side (the boat itself uses the onboard console):
  - FLEET: the permitted public race tracker (YB Tracking / cf.yb.tl) — the whole fleet, delayed;
    every fix carries its own timestamp so the UI can age it. Shore-side viewing of a public tracker
    is always fine — the onboard-use gate (`rules_profile.tracker_permitted`) is a separate, in-race
    concern, surfaced here only as a note.
  - OWN BOAT: our boat's live position + instruments from the cloud agent (the uplinked telemetry),
    via GET /conditions. Best-effort: if the agent is unreachable or the boat isn't uplinking, the
    panel says so.
"""
import json
import os
import time
import urllib.request

_TIMEOUT = float(os.environ.get("MONITOR_TIMEOUT_S", "8"))
_AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000").rstrip("/")
_BOAT_PASSWORD = os.environ.get("BOAT_PASSWORD", "")

_YB_PROVIDERS = ("yb", "bycmack", "ybtracking", "yellowbrick")
_tok = {"value": None, "exp": 0.0}


def _http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode())


def _yb_url(cfg):
    url = (cfg.get("url") or "").strip()
    if url:
        return url
    race = (cfg.get("race") or "").strip()
    if not race:
        return None
    host = (cfg.get("host") or "cf.yb.tl").strip()
    return f"https://{host}/API3/Race/{race}/GetPositions?t=0"


def _parse_yb(payload):
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not teams:
        return []
    fixes = []
    for tm in teams:
        ps = (tm or {}).get("positions") or []
        if not ps:
            continue
        p = max(ps, key=lambda x: x.get("gpsAtMillis") or 0)   # newest fix for this boat
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            continue
        ms = p.get("gpsAtMillis")
        fixes.append({"name": tm.get("name"), "lat": lat, "lon": lon, "sog": p.get("sogKnots"),
                      "cog": p.get("cog"), "time": (ms / 1000.0 if ms else None), "dtf_nm": p.get("dtfNm")})
    return fixes


def _sample_fleet():
    now = time.time()
    boats = [("Il Mostro", 43.30, -82.20, 7.1, 18), ("Windquest", 43.42, -82.05, 6.4, 25),
             ("Natalie J", 43.18, -82.35, 5.9, 12), ("Equation", 43.55, -81.92, 7.8, 30)]
    return [{"name": n, "lat": la, "lon": lo, "sog": s, "cog": c, "time": now - 900, "dtf_nm": None}
            for (n, la, lo, s, c) in boats]


def fleet(definition, demo=False):
    cfg = (definition or {}).get("tracker") or {}
    rp = (definition or {}).get("rules_profile") or {}
    base = {"fixes": [], "provider": (cfg.get("provider") or None), "delay_min": cfg.get("delay_min"),
            "onboard_permitted": bool(rp.get("tracker_permitted")), "reason": ""}
    if demo or (cfg.get("provider") or "").lower() == "sample":
        base.update(fixes=_sample_fleet(), provider="sample", reason="demo fixture")
        return base
    prov = (cfg.get("provider") or "").lower()
    if prov not in _YB_PROVIDERS:
        base["reason"] = "no public tracker configured for this race"
        return base
    url = _yb_url(cfg)
    if not url:
        base["reason"] = "tracker has no race id / url"
        return base
    try:
        payload = _http_json(url)
    except Exception as e:
        base["reason"] = f"tracker fetch failed: {type(e).__name__}"
        return base
    if isinstance(payload, dict) and payload.get("error") and not payload.get("teams"):
        base["reason"] = "race not live yet (tracker dormant)"
        return base
    base["fixes"] = _parse_yb(payload)
    if not base["fixes"]:
        base["reason"] = "no positions yet"
    return base


def _agent_token():
    now = time.time()
    if _tok["value"] and _tok["exp"] > now:
        return _tok["value"]
    if not _BOAT_PASSWORD:
        return None
    j = _http_json(_AGENT_URL + "/auth", data=json.dumps({"password": _BOAT_PASSWORD}).encode(),
                   headers={"Content-Type": "application/json"})
    t = j.get("token")
    if t:
        _tok["value"], _tok["exp"] = t, now + 3600
    return t


def own():
    if not _BOAT_PASSWORD:
        return {"available": False, "reason": "agent link not configured (BOAT_PASSWORD unset)"}
    try:
        tok = _agent_token()
        if not tok:
            return {"available": False, "reason": "agent auth failed"}
        c = _http_json(_AGENT_URL + "/conditions", headers={"Authorization": "Bearer " + tok})
    except Exception as e:
        return {"available": False, "reason": f"agent unreachable: {type(e).__name__}"}
    if not c.get("available") or c.get("lat") is None:
        return {"available": False, "reason": "no live position from the boat yet"}
    return {"available": True, "lat": c.get("lat"), "lon": c.get("lon"), "sog": c.get("sog"),
            "cog": c.get("cog"), "heading": c.get("heading"), "stw": c.get("stw"),
            "tws": c.get("tws"), "twd": c.get("twd"), "as_of": c.get("as_of"),
            "age_s": c.get("data_age_seconds"), "stale": c.get("stale")}


def snapshot(definition, demo=False):
    return {"fleet": fleet(definition, demo=demo), "own": own()}
