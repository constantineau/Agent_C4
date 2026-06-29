"""Out-of-process GRIB parser worker (run as `python -m app.wind._grib_parser`).

Isolates cfgrib/eccodes parsing in a CHILD process so a native segfault (a known intermittent eccodes
finalizer crash) takes down only this child — not the optimize worker. The parent
(`grib.IsolatedGribParser`) feeds one JSON request per line on stdin: {"path": "<grib file>"}. We reply
one JSON line per request on stdout: {"ok": true, "npz": "<tmp .npz with lat/lon/u/v/regular>"} or
{"ok": false, "error": "..."}. Arrays go via a temp .npz (shared filesystem) so they don't have to be
encoded over the pipe. If THIS process dies, the parent respawns it and retries the file.
"""
import json
import os
import sys
import tempfile

import numpy as np

from app.wind.grib import open_uv


def main():
    # warm the heavy import once at startup (so the first real parse isn't slow, and an import-time
    # crash surfaces immediately rather than on the first request).
    try:
        import xarray  # noqa: F401
    except Exception:
        pass
    # test hook: simulate a native crash on any path containing this substring (used by the hardening
    # test to exercise respawn/skip without needing a genuinely-corrupt GRIB).
    crash_on = os.environ.get("GRIB_PARSER_CRASH_ON", "")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            path = req["path"]
            if crash_on and crash_on in path:
                os.abort()                       # hard crash, uncatchable — like the real segfault
            lat, lon, u, v, regular = open_uv(path)
            fd, out = tempfile.mkstemp(suffix=".npz", prefix="gribuv_")
            os.close(fd)
            np.savez(out, lat=lat, lon=lon, u=u, v=v, regular=np.array(bool(regular)))
            resp = {"ok": True, "npz": out}
        except Exception as e:                    # genuine parse error → reported, not crashed
            resp = {"ok": False, "error": str(e)[:300]}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
