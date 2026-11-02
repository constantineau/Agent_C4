"""The archiver's status file is read by the engine, and says why recording stopped.

Before, `archiver-status.json` was written and nothing read it: a disk-full stop or a damaged
archive was visible only in `docker logs`, and the iPad's LOG button kept saying "Logging"
(pi review 2026-11-02 #2). Now the engine serves `shared/archiver_status.read()` on /session and
/health, and the console shows "Not recording: <reason>".

This tests the archiver's writer and the engine's reader against each other (the engine's
endpoint is a one-line call to the reader; its image needs fastapi, this one does not).

Run:  python3 pi/archiver/test_status_file.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))     # repo root: shared/
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402
from shared import archiver_status  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class Guard:
    """The DiskGuard fields write_status reads."""

    def __init__(self, paused=False):
        self.paused = paused
        self.pct = self.raw_pct = 96.0 if paused else 50.0
        self.free_bytes = 10**9
        self.hard_pct, self.high, self.low = 95.0, 85.0, 80.0
        self.reason = "hard_floor" if paused else None
        self.paused_since = "2026-11-01T12:00:00+00:00" if paused else None
        self.dropped = 7 if paused else 0


print("the writer and the reader agree on the file:")
check("same file name on both sides",
      archiver.STATUS_FILE.name == archiver_status.FILE_NAME == "archiver-status.json")
os.environ["ARCHIVE_DB"] = "/x/y/archive.db"
check("the engine finds it beside ARCHIVE_DB",
      archiver_status.default_path() == Path("/x/y/archiver-status.json"))
os.environ["ARCHIVE_STATUS_FILE"] = "/elsewhere/st.json"
check("ARCHIVE_STATUS_FILE (the archiver's variable) moves the reader too",
      archiver_status.default_path() == Path("/elsewhere/st.json"))
del os.environ["ARCHIVE_STATUS_FILE"]

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "archiver-status.json"

    print("each cause reads as 'Not recording: <reason>':")
    mono = 5000.0
    cases = [
        ("healthy", {"last_write_ok": mono, "buffered": 0}, Guard(), True, None),
        ("disk full", {}, Guard(paused=True), False, "Not recording: disk full"),
        ("damaged archive", {"rotate_pending": True}, Guard(), False,
         "Not recording: archive damaged, starting a new file"),
        ("no telemetry for 60 s", {"last_telemetry": mono - 60}, Guard(), False,
         "Not recording: no instrument data"),
        ("no telemetry for 5 s (a blip)", {"last_telemetry": mono - 5}, Guard(), True, None),
        ("writes failing 60 s, rows waiting", {"last_write_ok": mono - 60, "buffered": 900},
         Guard(), False, "Not recording: writes failing"),
        ("no write for 60 s but nothing waiting (quiet bus)",
         {"last_write_ok": mono - 60, "buffered": 0}, Guard(), True, None),
    ]
    for name, state, guard, recording, text in cases:
        archiver.write_status(state, guard, path=path, now=mono)
        got = archiver_status.read(path)
        check(f"{name}: recording={recording}, text={text!r}",
              got["recording"] is recording and got["text"] == text)

    print("a quiet note rides along without stopping 'recording':")
    archiver.write_status({"check_note": "Archive check overdue", "last_write_ok": mono},
                          Guard(), path=path, now=mono)
    got = archiver_status.read(path)
    check("note passed through, still recording",
          got["recording"] is True and got["note"] == "Archive check overdue"
          and got["text"] is None)

    print("disk full carries its numbers through:")
    archiver.write_status({}, Guard(paused=True), path=path, now=mono)
    got = archiver_status.read(path)
    check("rows not recorded are passed on", got["rows_not_recorded_disk_full"] == 7)
    check("reason key is passed on", got["reason"] == "disk_full")

    print("a dead archiver is not a recording one:")
    archiver.write_status({}, Guard(), path=path, now=mono)
    doc = json.loads(path.read_text())
    doc["updated"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(doc))
    got = archiver_status.read(path)
    check("a file 5 min old: Not recording: archiver stopped",
          got["recording"] is False and got["text"] == "Not recording: archiver stopped")
    check("...with its age", got["age_s"] is not None and got["age_s"] >= 299)
    check("a fresh one is fine",
          archiver_status.read(path, now=time.time() - 290)["recording"] is True)
    path.unlink()
    got = archiver_status.read(path)
    check("no file: Not recording: archiver not running",
          got["recording"] is False and got["text"] == "Not recording: archiver not running")
    path.write_text("{not json")
    got = archiver_status.read(path)
    check("garbage: Not recording, and no exception",
          got["recording"] is False and got["reason"] == "unreadable")

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
