"""SR33 sail crossover model — routing fidelity 2b (the per-leg SAIL PLAN source).

The Lab optimizer routes on the Best-Performance polar envelope, which IS the max-over-sails speed —
so the route's SPEED is already sail-optimal, but the route doesn't say WHICH sail achieves it. This
module supplies that: the per-(TWS,TWA) optimal sail + the per-TWS crossover bands, precomputed from
the ORC certificate into `sr33_crossovers.json` (by `vps/agent/knowledge/build_speed_guide.py`). The
optimizer attaches `sail` to each leg; the playbook bundle freezes the whole crossover table as the
reviewed, onboard-loadable boat sail model. (Lab-3 will make this boat-scoped via BoatProfile; the
file is already keyed by `boat_id`.)
"""
import json
import os

CROSSOVERS_FILE = os.environ.get("CROSSOVERS_FILE", "/srv/sr33_crossovers.json")
_CACHE = None


def _load():
    global _CACHE
    if _CACHE is None:
        try:
            with open(CROSSOVERS_FILE) as f:
                _CACHE = json.load(f)
        except (OSError, ValueError):
            _CACHE = {"crossovers": {}, "tws_buckets": [], "inventory": [], "sail_names": {}}
    return _CACHE


def available() -> bool:
    return bool(_load().get("crossovers"))


def model() -> dict:
    """The full sail model (for the bundle's boat_model block + the review UI)."""
    d = _load()
    return {"boat_id": d.get("boat_id"), "source": d.get("source"),
            "inventory": d.get("inventory", []), "sail_names": d.get("sail_names", {}),
            "tws_buckets": d.get("tws_buckets", []), "crossovers": d.get("crossovers", {})}


def _nearest_tws(tws):
    buckets = _load().get("tws_buckets") or []
    if not buckets or tws is None:
        return None
    return min(buckets, key=lambda b: abs(b - tws))


def crossovers(tws):
    """The sail zones for the nearest TWS bucket: [{sail, short, label, twa_min, twa_max}]."""
    b = _nearest_tws(tws)
    return _load().get("crossovers", {}).get(str(b), []) if b is not None else []


def optimal_sail(tws, twa):
    """The crew-shorthand sail (e.g. 'A3') optimal at (tws, twa). TWA is folded to 0–180 (port/
    starboard symmetric). A leg's TWA is the DIRECT-course angle, so an upwind beat can read below
    the close-hauled angle (you tack up on the beat sail) — clamp to the first/last band rather than
    returning nothing. None only when the model is unavailable."""
    if twa is None:
        return None
    twa = abs(((twa + 180) % 360) - 180)
    zones = crossovers(tws)
    if not zones:
        return None
    for z in zones:
        if z["twa_min"] <= twa <= z["twa_max"]:
            return z.get("short") or z.get("sail")
    if twa < zones[0]["twa_min"]:                     # below the beat angle → beating on the up sail
        return zones[0].get("short") or zones[0].get("sail")
    return zones[-1].get("short") or zones[-1].get("sail")   # past the last band → the run sail
