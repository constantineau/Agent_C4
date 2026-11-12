"""#309: each weather model declares where it has data, and a race outside a model's area never
asks for it. Today that is HRRR (US only): a race off the US gets no HRRR download and says so
plainly; a Great Lakes race keeps GFS + HRRR exactly as before.

No network: every fetch is stubbed (a fetch for HRRR off the US fails the test).
  1) the coverage registry: HRRR covers Lake Huron and Florida, not the English Channel or
     Palma; the global models cover everything; the model skill's checks read the same registry;
  2) build_windfield on a Channel box asks GFS only, carries "HRRR: not here (US only)" in its
     meta, and says it in the progress log; on a Lake Huron box it asks both, as before;
  3) archive (debrief/retro) sources and the Orin's wrapped sources follow the same rule;
  4) the route's `data` notes and the Orin's weather block say it, and HRRR is not "missing";
  5) the plume recorder skips HRRR off the US without a download.
"""
import os
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
os.environ["GRIB_ISOLATE_PARSE"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import modelskill, optimizer, plume  # noqa: E402
from app.wind import archive, models as M, windfield as WF  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


CHANNEL = (50.6, 49.9, -1.6, -0.5)        # (n, s, w, e): off the Isle of Wight
HURON = (46.0, 43.0, -84.0, -81.5)
T0 = 1_811_224_800
NOW = int(time.time()) // 3600 * 3600    # the live loader only asks for hours a cycle reaches

# ---- 1) the registry --------------------------------------------------------------------------
print("coverage registry:")
check("HRRR covers Lake Huron and Florida", M.covers("hrrr", 44.5, -82.5) and M.covers("hrrr", 26.1, -80.05))
check("HRRR does not cover the Channel or Palma",
      not M.covers("hrrr", 50.3, -1.2) and not M.covers("hrrr", 39.5, 2.5))
check("global models cover everywhere",
      all(M.covers(m, 50.3, -1.2) and M.covers(m, -33.8, 151.3) for m in ("gfs", "ecmwf", "icon", "gem", "gefs")))
check("a race box: HRRR in for Huron, out for the Channel",
      M.covers_bbox("hrrr", HURON) and not M.covers_bbox("hrrr", CHANNEL))
check("a box only partly in HRRR's area keeps HRRR (the blend skips the points it misses)",
      M.covers_bbox("hrrr", (22.0, 20.0, -81.0, -79.0)))
check("the plain note names the area", M.coverage_note("hrrr") == "HRRR: not here (US only)", M.coverage_note("hrrr"))
check("/api/models says each model's area",
      M.available_models()["hrrr"]["coverage"] == "US only" and M.available_models()["gfs"]["coverage"] == "global")
check("model skill reads the same registry",
      modelskill.covers is M.covers and modelskill.coverage_note("hrrr") == M.coverage_note("hrrr"))

# ---- 2) build_windfield -----------------------------------------------------------------------
print("build_windfield:")
asked = []


def stub_fetch(self, cycle, fhr, member, bbox, timeout=60):
    asked.append((self.name, tuple(bbox)))
    return None                                  # nothing posted: no file, no network


real_fetch = M.ModelSource.fetch
M.ModelSource.fetch = stub_fetch
try:
    log = []
    wf = WF.build_windfield(CHANNEL, NOW, NOW + 12 * 3600, models=("gfs", "hrrr"),
                            on_progress=log.append, fallback_tries=0)
    names = {n for n, _b in asked}
    check("Channel: GFS asked, HRRR never asked", "gfs" in names and "hrrr" not in names, names)
    hr = [m for m in wf.meta if m["model"] == "hrrr"]
    check("Channel: HRRR's meta says not here", hr and hr[0].get("skipped") == M.NOT_HERE
          and hr[0].get("frames") == 0 and hr[0].get("note") == "HRRR: not here (US only)", hr)
    check("…and the progress log says it", "HRRR: not here (US only)" in log, log)
    check("coverage_notes turns it into a sentence",
          WF.coverage_notes(wf.meta) == ["HRRR: not here (US only)."], WF.coverage_notes(wf.meta))

    asked.clear()
    wf = WF.build_windfield(HURON, NOW, NOW + 12 * 3600, models=("gfs", "hrrr"), fallback_tries=0)
    names = {n for n, _b in asked}
    check("Lake Huron: GFS and HRRR both asked, as before", names == {"gfs", "hrrr"}, names)
    check("Lake Huron: no not-here note", WF.coverage_notes(wf.meta) == [])

    # ---- 3) archive sources (debrief / retro) and the Orin's wrapped sources -----------------
    print("archive and wrapped sources:")
    asked.clear()
    srcs = archive.gun_sources(T0, context="test")
    check("archive HRRR declares the same area", not srcs[1].covers_bbox(CHANNEL) and srcs[1].covers_bbox(HURON))

    def no_archive_fetch(self, cycle, fhr, member, bbox, timeout=90):
        asked.append((self.name, tuple(bbox)))
        return None

    real_arch = archive._ArchiveMixin.fetch
    archive._ArchiveMixin.fetch = no_archive_fetch
    try:
        wf = WF.build_windfield(CHANNEL, T0, T0 + 6 * 3600, models=srcs, fallback_tries=0)
    finally:
        archive._ArchiveMixin.fetch = real_arch
    check("debrief archive: HRRR's whole-US file never fetched off the US",
          "hrrr" not in {n for n, _b in asked} and "gfs" in {n for n, _b in asked}, asked[:3])
    check("…and said", WF.coverage_notes(wf.meta) == ["HRRR: not here (US only)."])

    class Wrapped:                               # the Orin's GribSource forwards to its model
        def __init__(self, src):
            self._src = src

        def __getattr__(self, k):
            return getattr(self._src, k)

    check("a wrapped source carries its model's area",
          not Wrapped(M.MODELS["hrrr"]).covers_bbox(CHANNEL) and Wrapped(M.MODELS["gfs"]).covers_bbox(CHANNEL))
finally:
    M.ModelSource.fetch = real_fetch

# ---- 4) what the route and the Orin say -------------------------------------------------------
print("route data notes and the Orin weather block:")
fake = WF.WindField({}, [{"model": "gfs", "frames": 3}, WF.not_here_meta(M.MODELS["hrrr"])],
                    CHANNEL, T0, T0 + 3600)
check("optimizer.wind_notes", optimizer.wind_notes(fake) == ["HRRR: not here (US only)."])
check("no field → no notes", optimizer.wind_notes(None) == [])

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "orin", "reroute"))
try:
    import orin_weather as W
except Exception as exc:   # noqa: BLE001
    W = None
    print(f"  [SKIP] orin_weather not importable here ({type(exc).__name__})")
if W is not None:
    status = {"models": [{"model": "gfs", "cycle": "2027-05-25 00Z", "frames": 5, "expected_frames": 5},
                         WF.not_here_meta(M.MODELS["hrrr"])]}
    srcs = {"gfs": M.MODELS["gfs"], "hrrr": M.MODELS["hrrr"]}

    class Budget:
        online, downloads, exhausted = False, 0, False

    w = W.summarize(status, T0, Budget(), "test", sources=list(srcs.values()), requested=["gfs", "hrrr"])
    check("Orin: HRRR off the US is not 'missing' (no 'No forecast from hrrr' warning)",
          w.get("missing") == [], w.get("missing"))

# ---- 5) the plume recorder --------------------------------------------------------------------
print("plume recorder:")
calls = []


def fake_run(src, bbox, times, now, parser):
    calls.append(src.name)
    return None


spec = {"key": "ch__route", "race_id": "ch", "basis": "route", "label": "plan",
        "path": [{"lat": 50.3, "lon": -1.3, "t": T0}, {"lat": 50.1, "lon": -1.0, "t": T0 + 6 * 3600}]}
say = []
plume.record(spec, now=T0, models=("gfs", "hrrr"), fetch=fake_run, on_progress=say.append)
check("Channel plume: GFS checked, HRRR not", calls == ["gfs"], calls)
check("…and said", "HRRR: not here (US only)" in say, say)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
