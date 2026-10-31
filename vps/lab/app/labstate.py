"""labstate — a tiny JSON key/value store for Lab-wide settings that must persist.

Things like the **active boat** and the **chart source** (Natural Earth vs NOAA ENC) are Lab-wide
selections, not per-race artifacts, so they live here rather than in a RaceDefinition. Persisted on
the writable `lab_ingested` volume (same place reviewed races land) so a container restart keeps the
selection. Dependency-free; writes are serialized in-process by a lock.
"""
import json
import os
import threading

_PATH = os.path.join(os.environ.get("INGESTED_DIR", "/srv/ingested"), "_labstate.json")


def _read() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def get(key, default=None):
    return _read().get(key, default)


# One writer at a time: handlers on the event loop and worker threads (a boat push records its
# result from the threadpool) both read-modify-replace the same file and share its .tmp.
_LOCK = threading.Lock()


def update(values: dict):
    """Set several keys in one read-modify-replace."""
    with _LOCK:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        state = _read()
        state.update(values)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _PATH)        # atomic
    return values


def set(key, value):
    update({key: value})
    return value


def all() -> dict:
    return _read()
