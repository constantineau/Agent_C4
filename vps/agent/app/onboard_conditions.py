"""Live instrument conditions for the ONBOARD engine service (Phase 9.1).

The cloud builds the instrument strip / multi-source view in `tools.py` straight off Postgres
(`get_strip` / `get_current_conditions` / `get_sources`). Onboard there is no Postgres — the
same views are built here from `datasource.active()` (an `OnboardSource`: SQLite archive + live
Signal K cache). The collect-everything ethos is preserved: every (path, source) is surfaced
with freshness and a disagreement flag.

This mirrors `tools.PRESENT` / `DISAGREE`; kept standalone so the onboard image needs none of
the cloud tool stack (DB pool, alerts, summarizer, LLM). If a channel is added to tools.PRESENT,
mirror it here.
"""
from . import datasource
from . import fatigue

_ms_to_kn = lambda x: x * 1.943844
_rad_to_deg = lambda x: x * 57.295779513
_k_to_c = lambda x: x - 273.15
_id = lambda x: x

# Signal K path -> (channel, display unit, SI->display converter). Mirror of tools.PRESENT.
PRESENT = {
    "navigation.speedThroughWater":    ("stw", "kn", _ms_to_kn),
    "navigation.speedOverGround":      ("sog", "kn", _ms_to_kn),
    "navigation.courseOverGroundTrue": ("cog", "°", _rad_to_deg),
    "navigation.headingTrue":          ("heading_true", "°", _rad_to_deg),
    "navigation.headingMagnetic":      ("heading_mag", "°", _rad_to_deg),
    "navigation.attitude.roll":        ("heel", "°", _rad_to_deg),
    "navigation.attitude.pitch":       ("pitch", "°", _rad_to_deg),
    "navigation.rateOfTurn":           ("rate_of_turn", "°/s", _rad_to_deg),
    "navigation.position.latitude":    ("lat", "°", _id),
    "navigation.position.longitude":   ("lon", "°", _id),
    "environment.wind.speedApparent":  ("aws", "kn", _ms_to_kn),
    "environment.wind.angleApparent":  ("awa", "°", _rad_to_deg),
    "environment.wind.speedTrue":      ("tws", "kn", _ms_to_kn),
    "environment.wind.angleTrueWater": ("twa", "°", _rad_to_deg),
    "environment.wind.directionTrue":  ("twd", "°", _rad_to_deg),
    "environment.depth.belowTransducer": ("depth", "m", _id),
    "environment.water.temperature":   ("water_temp", "°C", _k_to_c),
    "steering.rudderAngle":            ("rudder_angle", "°", _rad_to_deg),
}
DISAGREE = {"°": 6.0, "kn": 0.6, "m": 1.0, "°C": 2.0, "°/s": 5.0}


def _age(epoch):
    import time
    return round(time.time() - epoch, 1)


def get_current_conditions(max_age_minutes: int = 5):
    """Every live quantity, from EVERY reporting source, with freshness + a disagreement flag.
    Freshest source wins as the lead (the cloud adds priority/failover from a Postgres table;
    onboard v1 uses freshest — refine when real-bus $source priority is loaded onboard)."""
    rows = datasource.active().latest_per_source(list(PRESENT.keys()), max_age_minutes)
    channels = {}
    for r in rows:
        ch, unit, conv = PRESENT[r["path"]]
        channels.setdefault(ch, {"unit": unit, "readings": []})
        channels[ch]["readings"].append(
            {"source": r["source"], "value": round(conv(r["value"]), 3),
             "age_s": _age(r["epoch"])}
        )
    for ch, c in channels.items():
        vals = [x["value"] for x in c["readings"]]
        c["freshest_age_s"] = min(x["age_s"] for x in c["readings"])
        if len(vals) > 1:
            c["spread"] = round(max(vals) - min(vals), 3)
            c["disagreement"] = c["spread"] > DISAGREE.get(c["unit"], 1e9)
        best = min(c["readings"], key=lambda x: x["age_s"])
        c["preferred"] = {"source": best["source"], "value": best["value"],
                          "age_s": best["age_s"]}
        c["preferred_reason"] = "freshest available (onboard)"
    if not channels:
        return {"available": False, "note": "no telemetry in window"}
    from datetime import datetime, timezone
    return {
        "available": True, "as_of": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "note": ("Onboard engine: every source kept; lead is the freshest reading. Cross-check "
                 "disagreement; treat stale/uncalibrated sources with caution."),
    }


def get_strip():
    """Compact best-value-per-channel for the iPad instrument strip (freshest source wins)."""
    cc = get_current_conditions(max_age_minutes=5)
    if not cc.get("available"):
        return {"available": False}
    ch = cc["channels"]

    def best(name):
        c = ch.get(name)
        return c["preferred"]["value"] if c else None

    ages = [c["freshest_age_s"] for c in ch.values()]
    heading = best("heading_true")
    if heading is None:
        heading = best("heading_mag")
    fi = fatigue.compute_fatigue_index()
    return {
        "available": True, "as_of": cc["as_of"],
        "data_age_seconds": min(ages) if ages else None,
        "stale": (min(ages) if ages else 999) > 30,
        "stw": best("stw"), "sog": best("sog"), "tws": best("tws"), "twa": best("twa"),
        "twd": best("twd"), "aws": best("aws"), "awa": best("awa"), "heading": heading,
        "heel": best("heel"), "depth": best("depth"),
        "cog": best("cog"), "lat": best("lat"), "lon": best("lon"),
        "fatigue": fi.get("index"), "fatigue_level": fi.get("level"),
    }


def get_sources(max_age_minutes: int = 10):
    """Active sensor sources onboard: what's reporting and how fresh (no curated notes onboard)."""
    out = []
    for s in datasource.active().sources(max_age_minutes):
        out.append({"source": s["source"], "last_seen_s": _age(s["last_epoch"]),
                    "paths": s["paths"], "samples": s["samples"],
                    "device": None, "reliability": "unknown", "note": None})
    return {"count": len(out), "sources": out,
            "note": "Onboard source list (curated reliability notes live in the cloud DB)."}
