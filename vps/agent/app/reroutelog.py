"""Re-route run log (#125, for the history in #127).

One line per event, appended on the Pi next to the engine DB (`reroute-runs.jsonl`):

  * `pressed`  — a crew member pressed Re-route now: when, which device, which engine (orin|pi),
                 and what happened (started, busy, refused with its reason, Orin unreachable);
  * `done`     — a route came back: who asked, when it was asked and finished, the weather it used
                 (models, cycles, ages), the gain vs the plan, its warnings;
  * `failed`   — a run that ended without a route, with the reason.

Nothing here decides anything. It only keeps what happened, so a run is never lost when the
stored "last route" is replaced by the next one. The file is trimmed to the newest MAX_LINES.
"""
import json
import os
import threading
import time

MAX_LINES = 2000
_LOCK = threading.Lock()


def _path():
    d = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db"))
    return os.path.join(d, "reroute-runs.jsonl")


def _lines():
    try:
        with open(_path()) as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def _parse(lines):
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def record(event, engine, once_key=None, **fields):
    """Append one event. With `once_key`, an event already logged under the same (event, engine,
    once_key) is not logged again (a finished run is seen by every poll). Never raises: losing a
    log line must not break a re-route."""
    rec = {"event": event, "engine": engine, "at": int(time.time())}
    if once_key is not None:
        rec["key"] = str(once_key)
    rec.update({k: v for k, v in fields.items() if v is not None})
    try:
        with _LOCK:
            lines = _lines()
            if once_key is not None:
                for r in _parse(lines):          # every retained line: a repeat never gets a new timestamp
                    if (r.get("event"), r.get("engine"), r.get("key")) == (event, engine, str(once_key)):
                        return False
            os.makedirs(os.path.dirname(_path()), exist_ok=True)
            if len(lines) >= MAX_LINES:
                keep = lines[-(MAX_LINES - 1):] + [json.dumps(rec, separators=(",", ":"))]
                tmp = _path() + ".part"
                with open(tmp, "w") as f:
                    f.write("\n".join(keep) + "\n")
                os.replace(tmp, _path())
            else:
                with open(_path(), "a") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        return True
    except OSError as exc:
        print(f"[reroutelog] could not log {event}: {exc}", flush=True)
        return False


def history(limit=50):
    """The newest `limit` events, newest first."""
    recs = _parse(_lines())
    return list(reversed(recs[-max(1, int(limit)):]))


def weather_summary(weather):
    """The part of a result's weather block worth keeping per run: which models and cycles, how
    old, and whether it was online."""
    w = weather or {}
    return {"models": [{"model": m.get("model"), "cycle": m.get("cycle"), "age_h": m.get("age_h")}
                       for m in (w.get("models") or []) if isinstance(m, dict)],
            "newest_age_h": w.get("newest_age_h"), "oldest_age_h": w.get("oldest_age_h"),
            "online": w.get("online"), "missing": w.get("missing") or []}
