"""Race library — load RaceDefinition instances from disk.

Bundled definitions ship in the image (`/srv/races`) as seeds; ingested/reviewed ones land on a
writable volume (`/srv/ingested`). The ingested dir takes PRECEDENCE — a reviewed/edited copy of a
race (same `race_id`) overrides the bundled seed — so the Course & Marks review can persist edits.
Validation reuses the shared schema validator.
"""
import glob
import json
import os

# Ingested (reviewed/edited) first so it overrides the bundled seed of the same race_id.
RACES_DIRS = [os.environ.get("INGESTED_DIR", "/srv/ingested"),
              os.environ.get("RACES_DIR", "/srv/races")]

from shared import race_def


def _files():
    out = []
    for d in RACES_DIRS:
        if os.path.isdir(d):
            out += sorted(glob.glob(os.path.join(d, "*.json")))
    return out


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_races():
    """Summaries for the library list, with validation counts (how much review is outstanding)."""
    races, seen = [], set()
    for f in _files():
        d = _load(f)
        if not d:
            continue
        rid = d.get("race_id") or os.path.splitext(os.path.basename(f))[0]
        if rid in seen:
            continue
        seen.add(rid)
        errs, warns = race_def.validate(d)
        reqs = d.get("requirements", [])
        races.append({
            "race_id": rid, "name": d.get("name"), "year": d.get("year"),
            "region": d.get("region"), "start_date": d.get("start_date"),
            "courses": len(d.get("courses", [])), "divisions": len(d.get("divisions", [])),
            "requirements": len(reqs),
            "ipad_items": len([r for r in reqs if r.get("deliver_to_ipad")]),
            "errors": len(errs), "warnings": len(warns),
            "review_status": d.get("provenance", {}).get("review_status", ""),
        })
    return races


def get_race(rid):
    for f in _files():
        d = _load(f)
        if d and (d.get("race_id") or os.path.splitext(os.path.basename(f))[0]) == rid:
            return d
    return None
