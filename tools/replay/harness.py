"""Race Rewind — precompute a scrubbable timeline of a recorded race.

Steps a virtual clock across the race and, at each step, asks the REAL onboard engine app what
it would have served. The result is one frame per timestamp holding the exact JSON the iPad
would have received, so the review UI can scrub instantly with no engine in the loop — and two
timelines computed either side of a code change can be diffed to show what the change moved.

Why the real FastAPI app rather than calling modules directly: the frame is then keyed by the
same paths `dashboard.js` fetches, so the review UI can shim `fetch()` straight onto it with no
mapping layer, and endpoint wiring (query params, response shaping) is exercised too.

Fidelity notes, all deliberate:
  - The clock is frozen per frame (freezegun), which covers the 32 wall-clock sites across the
    15 engine modules that read `datetime.now()` / `time.time()` directly, not just the data
    source's window cutoffs.
  - `NAV_PROGRESS_LATCH=false` so a frame depends only on its own timestamp and never on which
    frames were computed before it. Course progression is plane-geometry, which is monotone on
    a normal course, so this costs nothing on a real race track.
  - Endpoints that need the live internet (forecast, drift, buoy observations) cannot be
    replayed for a July race — the upstream APIs no longer serve those hours. They are excluded
    by default and recorded in the manifest as skipped, rather than silently returning junk.
  - AIS/fleet is empty: the archiver stored own-ship contexts only (see source.py).

Usage:
    python tools/replay/harness.py --start 2026-07-18T17:03:31Z --end 2026-07-18T20:40:00Z \
        --step 30 --out /home/constantineau/backups/replay-jul18/timeline
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# Endpoints the console polls that are deterministic functions of the archive + engine state.
# Ordered roughly by the console's own poll loop so a frame reads like one dashboard tick.
REPLAYABLE = [
    "/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/sources", "/fleet",
    "/watch", "/course", "/deviation", "/selector", "/strategy", "/plays", "/checklist",
    "/trend", "/session", "/sails/state", "/playbook/track", "/gps/status",
    # Added 2026-09-08 with the iPad's bank tile + instrument-health chip. These are the whole
    # reason the rig is worth having: the bank curve and the kicked compass are IN the recording,
    # so a scrub shows exactly when each tile would have spoken during the real race.
    "/power", "/health/sensors", "/conditions/full",
]
# Need the live internet (or a forecast archive we do not have) — see the module docstring.
NEEDS_NETWORK = ["/forecast", "/drift", "/buoys", "/plangap", "/reoptimize"]

# Per-endpoint recompute interval, seconds. Frames in between carry the last computed value
# forward — which is exactly what the console displays, since it polls each endpoint on its own
# timer rather than all of them every tick.
#
# `/strategy` is here for a substantive reason, not just speed. It runs a FULL isochrone
# re-optimize of the remaining course on every single call (~14 s here for the 259 nm left at
# 18:00Z, and this box is far quicker than a Pi 4) while `dashboard.js` polls it every 15 s.
# That went unnoticed because the broken mark sequencer made `reoptimize` route to the Start —
# a no-op. Fixing the sequencer turned it into a real 339 nm route. Recomputing a slow-moving
# strategic digest every 15 s is the wrong shape regardless; logged as a follow-up.
CADENCE_S = {"/strategy": 600.0}


def _hermetic():
    """Refuse non-loopback sockets for the duration of a build, and bound the ones we allow.

    Added 2026-09-08 after a build sat on an ESTABLISHED TLS connection to Open-Meteo for ten
    minutes at frame 282 and would have sat there forever: 376 bytes queued, no response, no
    socket timeout anywhere in the stack. `NEEDS_NETWORK` excludes /forecast, /drift, /buoys,
    /plangap and /reoptimize for exactly this reason, but `/strategy` is replayable and CHAINS
    into the same machinery when the verdict goes off-book, so the exclusion list was never
    enough. Two failures, not one:

      - a ~30-minute build can hang indefinitely on a third-party API, and
      - the frames it does write become a mix of replayed race and whatever the live internet
        said today, which is the fidelity problem the exclusion list exists to prevent.

    So the rig is hermetic by construction rather than by a list somebody has to maintain. A
    blocked call raises immediately, the engine module falls back to its no-forecast path (they
    all have one — that is the onboard design), and the frame records what happened instead of
    quietly borrowing today's weather. Set REPLAY_ALLOW_NET=true to opt out, and expect the
    frames to stop being reproducible if you do."""
    import socket
    if os.environ.get("REPLAY_ALLOW_NET", "").lower() in ("1", "true", "yes"):
        socket.setdefaulttimeout(20)          # at minimum, never hang forever
        print("[replay] REPLAY_ALLOW_NET set — frames will not be reproducible", flush=True)
        return
    socket.setdefaulttimeout(20)
    _real = socket.socket.connect
    _real_ex = socket.socket.connect_ex

    def _local_only(fn):
        def guard(self, address, *a, **kw):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and (host in ("localhost", "::1") or
                                          host.startswith("127.") or host.startswith("/")):
                return fn(self, address, *a, **kw)
            raise OSError(f"replay is hermetic: refused outbound connection to {host!r} "
                          f"(set REPLAY_ALLOW_NET=true to allow, and lose reproducibility)")
        return guard

    socket.socket.connect = _local_only(_real)
    socket.socket.connect_ex = _local_only(_real_ex)


def _setup(archive_db, engine_db, polars_file, spool_db=None):
    os.environ.update(
        ARCHIVE_DB=archive_db, ENGINE_DB=engine_db, POLARS_FILE=polars_file,
        DATA_SOURCE="onboard", ONBOARD_LIVE_WS="false",
        NAV_PROGRESS_LATCH="false",     # each frame stands alone — see the docstring
        REOPT_SWR="false",              # ...and so does the re-optimizer: its background
                                        # refresh would read a source the harness has already
                                        # moved to the next frame
    )
    # A second, coarser recording of the same race, attached read-only by ReplaySource so the
    # timeline can run past 20:40:30Z where the Pi's archive stops. See spool_to_sqlite.py.
    # Passed by env rather than argument because the harness builds a fresh source per frame.
    if spool_db:
        os.environ["REPLAY_SPOOL_DB"] = spool_db
    else:
        os.environ.pop("REPLAY_SPOOL_DB", None)
    for p in (HERE, ROOT, os.path.join(ROOT, "vps", "agent"), os.path.join(ROOT, "pi", "engine")):
        if p not in sys.path:
            sys.path.insert(0, p)


def build(archive_db, engine_db, polars_file, start, end, step_s, out_dir, endpoints=None,
          spool_db=None):
    _setup(archive_db, engine_db, polars_file, spool_db=spool_db)
    import freezegun
    from freezegun import freeze_time
    from fastapi.testclient import TestClient

    # freezegun's DEFAULT_IGNORE_LIST contains 'threading', and it decides whether to serve the
    # frozen clock by inspecting a bounded window of the call stack. TestClient runs each sync
    # endpoint on an AnyIO worker thread, so for most endpoints the `threading` frame is inside
    # that window and `time.time()` returned the REAL wall clock — 51 days after the race.
    #
    # This was silent because the ONE endpoint it did not affect is `/conditions`: `get_strip`
    # adds a stack frame, which pushes `threading` out of the inspected window. So the first
    # thing anyone checks looked right while `/sources` reported ages of 4,394,415 s in every
    # frame of the timeline, and every channel in `/conditions/full` read as `fell_back` because
    # nothing could be fresher than the 45 s failover window. A frozen clock the rig only mostly
    # applies is worse than no clock at all — clear the list.
    freezegun.configure(default_ignore_list=[])
    _hermetic()
    from source import ReplaySource
    from app import datasource
    import engine_app

    eps = list(endpoints or REPLAYABLE)
    client = TestClient(engine_app.app)
    os.makedirs(out_dir, exist_ok=True)

    frames_path = os.path.join(out_dir, "frames.jsonl")
    n_frames = int((end - start).total_seconds() // step_s) + 1
    errors, t_wall = {}, time.time()
    last_val, last_at, recomputes = {}, {}, {}

    with open(frames_path, "w") as fh:
        for i in range(n_frames):
            T = start + timedelta(seconds=i * step_s)
            ts = T.timestamp()
            with freeze_time(T):
                # A fresh source per frame: its per-frame series cache is keyed on `at`, and a
                # new instance also drops any thread-local archive cursors from the last frame.
                datasource._SOURCE = ReplaySource(at=ts)
                frame = {"t": T.isoformat().replace("+00:00", "Z"), "data": {}, "stale": []}
                for ep in eps:
                    every = CADENCE_S.get(ep)
                    if every and ep in last_val and ts - last_at[ep] < every:
                        frame["data"][ep] = last_val[ep]      # carry forward, as the iPad does
                        frame["stale"].append(ep)
                        continue
                    try:
                        r = client.get(ep)
                        val = r.json() if r.status_code == 200 else None
                        if r.status_code != 200:
                            errors[ep] = errors.get(ep, 0) + 1
                    except Exception as exc:
                        val = None
                        errors[ep] = errors.get(ep, 0) + 1
                        frame.setdefault("errors", {})[ep] = f"{type(exc).__name__}: {exc}"[:200]
                    frame["data"][ep] = val
                    last_val[ep], last_at[ep] = val, ts
                    recomputes[ep] = recomputes.get(ep, 0) + 1
                fh.write(json.dumps(frame) + "\n")
            if i % 20 == 0 or i == n_frames - 1:
                pct = (i + 1) / n_frames * 100
                print(f"[replay] frame {i+1}/{n_frames} ({pct:5.1f}%) {T:%H:%M:%S}Z", flush=True)

    manifest = {
        "built_at_wall_s": round(time.time() - t_wall, 1),
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "step_s": step_s, "frames": n_frames,
        "archive_db": archive_db, "engine_db": engine_db, "spool_db": spool_db,
        "endpoints": eps,
        "skipped_need_network": NEEDS_NETWORK,
        "cadence_s": {k: v for k, v in CADENCE_S.items() if k in eps},
        "recomputes_per_endpoint": recomputes,
        "endpoint_error_counts": errors,
        "notes": [
            "Frames are the real onboard engine's responses with the clock frozen per frame.",
            "AIS/fleet is empty — the archiver stored own-ship contexts only.",
            "Tier-2 copilot narration was never archived and cannot be replayed.",
        ],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[replay] {n_frames} frames -> {frames_path}")
    if errors:
        print(f"[replay] endpoints with errors: {errors}")
    return manifest


def _iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="Precompute a Race Rewind timeline.")
    ap.add_argument("--archive", default="/home/constantineau/backups/c4-boat-pull-2026-08-30/"
                                         "work/archive-backfill.db")
    ap.add_argument("--engine", default="/home/constantineau/backups/replay-jul18/engine.db")
    ap.add_argument("--polars", default=os.path.join(ROOT, "vps", "db", "seed", "polars_sr33.sql"))
    ap.add_argument("--spool", help="a readings-schema SQLite file of uplink aggregates "
                                    "(tools/replay/spool_to_sqlite.py) attached alongside the "
                                    "archive, so the timeline can run past where it stops")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--step", type=float, default=30.0, help="seconds between frames")
    ap.add_argument("--out", required=True)
    ap.add_argument("--endpoints", help="comma-separated override of the endpoint list")
    a = ap.parse_args()
    build(a.archive, a.engine, a.polars, _iso(a.start), _iso(a.end), a.step, a.out,
          endpoints=[e.strip() for e in a.endpoints.split(",")] if a.endpoints else None,
          spool_db=a.spool)


if __name__ == "__main__":
    main()
