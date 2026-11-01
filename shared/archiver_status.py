"""Read the Pi archiver's status file — is the full-res archive being written right now?

The archiver (pi/archiver/archiver.py `write_status`) rewrites `archiver-status.json` next to the
archive every few seconds. The engine mounts that volume read-only and serves this module's
`read()` on /session and /health, so the iPad's LOG button and a banner can say "Not recording:
disk full" instead of "Logging" while nothing reaches the disk (pi review 2026-11-02 #2).

A file that has stopped changing is itself a reason: a dead or crash-looping archiver writes
nothing, so its last word would otherwise read "recording" forever.

Stdlib only: it runs in the engine image and is tested in the archiver's.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

FILE_NAME = "archiver-status.json"
# The archiver writes every ~10 s (ARCHIVE_STATUS_EVERY_S). Older than this, it is not running.
STALE_S = float(os.environ.get("ARCHIVER_STATUS_STALE_S", "90"))

# reason key -> what the crew reads after "Not recording: ". Plain, short.
TEXT = {
    "disk_full": "disk full",
    "rotation_pending": "archive damaged, starting a new file",
    "write_failing": "writes failing",
    "no_signalk": "no instrument data",
    "stale": "archiver stopped",
    "missing": "archiver not running",
    "unreadable": "archiver status unreadable",
}


def default_path():
    """Beside the archive: ARCHIVER_STATUS_FILE, else next to ARCHIVE_DB."""
    explicit = os.environ.get("ARCHIVER_STATUS_FILE")
    if explicit:
        return Path(explicit)
    db = Path(os.environ.get("ARCHIVE_DB", "/var/lib/sr33/archive/archive.db"))
    return db.with_name(FILE_NAME)


def _not_recording(reason, **extra):
    out = {"recording": False, "reason": reason,
           "text": "Not recording: " + TEXT.get(reason, reason.replace("_", " "))}
    out.update(extra)
    return out


def read(path=None, now=None, stale_s=None):
    """{recording, reason, text, updated, age_s, ...}. `text` is None while recording, else
    "Not recording: <reason>". Never raises."""
    path = Path(path) if path else default_path()
    now = time.time() if now is None else float(now)
    stale_s = STALE_S if stale_s is None else float(stale_s)
    try:
        doc = json.loads(path.read_text())
        updated = doc["updated"]
        age = now - datetime.fromisoformat(updated).timestamp()
    except FileNotFoundError:
        return _not_recording("missing", updated=None, age_s=None)
    except Exception:
        return _not_recording("unreadable", updated=None, age_s=None)
    extra = {"updated": updated, "age_s": round(age, 1),
             "rows_dropped_buffer_full": doc.get("rows_dropped_buffer_full", 0),
             "rows_not_recorded_disk_full": doc.get("rows_not_recorded_disk_full", 0),
             "corrupt_rotations_this_run": doc.get("corrupt_rotations_this_run", 0),
             "background_check": doc.get("background_check")}
    if age > stale_s:
        return _not_recording("stale", **extra)
    if doc.get("recording") is False:
        return _not_recording(doc.get("reason") or "unknown", **extra)
    out = {"recording": True, "reason": None, "text": None}
    out.update(extra)
    return out
