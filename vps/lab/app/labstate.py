"""labstate — a tiny JSON key/value store for Lab-wide settings that must persist.

Things like the **active boat** and the **chart source** (Natural Earth vs NOAA ENC) are Lab-wide
selections, not per-race artifacts, so they live here rather than in a RaceDefinition. Persisted on
the writable `lab_ingested` volume (same place reviewed races land) so a container restart keeps the
selection. Dependency-free; last-write-wins (single-user team login, no concurrency to speak of).
"""
import json
import os

_PATH = os.path.join(os.environ.get("INGESTED_DIR", "/srv/ingested"), "_labstate.json")


def _read() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def get(key, default=None):
    return _read().get(key, default)


def set(key, value):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    state = _read()
    state[key] = value
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _PATH)            # atomic
    return value


def all() -> dict:
    return _read()
