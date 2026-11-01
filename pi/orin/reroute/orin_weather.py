"""Weather for the onboard re-route: the plume's GRIB cache first, downloads only when allowed.

The Orin's plume service (pi/orin/plume) keeps a GRIB cache in the Lab's own layout (GRIB_CACHE,
one file per model/cycle/forecast-hour/box). The re-route reads that same cache, so there is one
download layer on the boat. The Lab's wind loader (`app.wind.build_windfield`) runs unchanged; it
just gets these wrapped model sources instead of the live ones:

  * `GribSource.fetch` returns the cached file when there is one. Otherwise it downloads, but only
    while the internet is up and the run's download budget lasts (each request is capped too).
  * Offline, `GribSource.pick_cycle` picks the cycle the cache covers best for this run's window
    (newest on a tie), and the caller turns the loader's step-back-a-cycle retry off.
  * `ApiSource` (ICON/GEM via Open-Meteo) only runs online, inside the budget.

Cache files are keyed by the exact download box, so the run uses a box the cache already has when
one covers the course (`choose_bbox`): the plume's box (its route + 0.5°) or one a past re-route
used. A wider box than needed costs nothing; the frames are cropped to the course anyway.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time

from app.wind import models as M
from app.wind import windfield as WFM

REQUEST_TIMEOUT_S = float(os.environ.get("REROUTE_REQUEST_TIMEOUT_S", "20"))
DOWNLOAD_BUDGET_S = float(os.environ.get("REROUTE_DOWNLOAD_BUDGET_S", "180"))
BOXES_FILE = "boxes.json"        # the boxes past re-routes downloaded for (in REROUTE_DIR)
MAX_BOXES = 8


class Budget:
    """Whether this run may download, and for how long. Shared by every wrapped source."""

    def __init__(self, online, budget_s=DOWNLOAD_BUDGET_S, request_s=REQUEST_TIMEOUT_S):
        self.online = bool(online)
        self.deadline = time.monotonic() + budget_s
        self.request_s = request_s
        self.downloads = 0
        self.exhausted = False

    def left(self):
        return self.deadline - time.monotonic()

    def can_download(self):
        if not self.online:
            return False
        if self.left() <= 0.5:
            self.exhausted = True
            return False
        return True


def _tag(bbox):
    """The cache-file tag the Lab's sources use for a box (ModelSource._cache_path)."""
    return hashlib.md5(repr(bbox).encode()).hexdigest()[:6]


def cached_fhrs(name, member, bbox, cache_dir=None):
    """{cycle datetime: {fhr, ...}} of the files cached for this model, member and box."""
    cache_dir = cache_dir or M.CACHE
    suffix = f"_{_tag(bbox)}.grib2"
    out = {}
    try:
        names = os.listdir(os.path.join(cache_dir, name))
    except OSError:
        return out
    pre = f"{name}_"
    for n in names:
        if not (n.startswith(pre) and n.endswith(suffix)):
            continue
        parts = n[len(pre):-len(suffix)].split("_")       # YYYYMMDDHH, member, fNNN
        if len(parts) != 3 or parts[1] != member or not parts[2].startswith("f"):
            continue
        try:
            cyc = dt.datetime.strptime(parts[0], "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
            fhr = int(parts[2][1:])
        except ValueError:
            continue
        try:
            if os.path.getsize(os.path.join(cache_dir, name, n)) <= 100:
                continue
        except OSError:
            continue
        out.setdefault(cyc, set()).add(fhr)
    return out


class GribSource:
    """A live GRIB model source behind the cache-first, budgeted rules (see the module doc)."""

    def __init__(self, src, budget, bbox, t_start, t_end):
        self._src, self._budget = src, budget
        self._bbox, self._t0, self._t1 = bbox, t_start, t_end

    def __getattr__(self, k):
        return getattr(self._src, k)

    def best_cached_cycle(self):
        """The cached cycle with the most frames in this run's window; newest on a tie. None if
        nothing is cached for this box."""
        best = None
        for cyc, have in cached_fhrs(self._src.name, "det", self._bbox).items():
            want = set(WFM._fhrs_for_cycle(self._src, cyc, self._t0, self._t1))
            n = len(want & have)
            if n and (best is None or (n, cyc) > best):
                best = (n, cyc)
        return best[1] if best else None

    def pick_cycle(self, now=None, min_horizon_h=0):
        if self._budget.online:
            return self._src.pick_cycle(now, min_horizon_h=min_horizon_h)
        return self.best_cached_cycle() or self._src.pick_cycle(now, min_horizon_h=min_horizon_h)

    def fetch(self, cycle, fhr, member, bbox, timeout=None):
        path = self._src._cache_path(cycle, fhr, member, bbox)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
        if not self._budget.can_download():
            return None
        try:
            got = self._src.fetch(cycle, fhr, member, bbox,
                                  timeout=min(self._budget.request_s, max(1.0, self._budget.left())))
        except Exception:  # noqa: BLE001 — a failed frame is skipped, like the Lab does
            return None
        if got:
            self._budget.downloads += 1
        return got


class ApiSource:
    """ICON/GEM (Open-Meteo): no files to reuse, so online only, inside the budget."""

    def __init__(self, src, budget):
        self._src, self._budget = src, budget

    def __getattr__(self, k):
        return getattr(self._src, k)

    def _empty(self, why):
        return {}, {"model": self._src.name, "cycle": None, "members": 1, "frames": 0,
                    "expected_frames": 0, "cycle_fallbacks": 0, "priority": self._src.priority,
                    "kind": self._src.kind, "skipped": why}

    def load_series(self, bbox, t_start, t_end, on_progress=None):
        if not self._budget.can_download():
            if on_progress:
                on_progress(f"{self._src.name}: skipped (offline)")
            return self._empty("offline")
        box = {}

        def work():
            try:
                box["r"] = self._src.load_series(bbox, t_start, t_end, on_progress)
            except Exception:  # noqa: BLE001
                pass

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(max(0.5, self._budget.left()))
        if "r" not in box:
            self._budget.exhausted = self._budget.exhausted or th.is_alive()
            return self._empty("timed out" if th.is_alive() else "failed")
        series, meta = box["r"]
        if series:
            self._budget.downloads += 1
        return series, meta


def sources_for(names, budget, bbox, t_start, t_end):
    """Wrapped sources for the model names (unknown names and ensembles are dropped)."""
    out = []
    for n in names:
        src = M.MODELS.get(n)
        if src is None or src.kind != "deterministic":
            continue
        out.append(ApiSource(src, budget) if hasattr(src, "load_series")
                   else GribSource(src, budget, bbox, t_start, t_end))
    return out


# ---- which box ----------------------------------------------------------------------------------
def _contains(outer, inner, tol=1e-6):
    return (outer[0] >= inner[0] - tol and outer[1] <= inner[1] + tol
            and outer[2] <= inner[2] + tol and outer[3] >= inner[3] - tol)


def _area(b):
    return max(0.0, b[0] - b[1]) * max(0.0, b[3] - b[2])


def _overlap(a, b):
    n, s, w, e = min(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3])
    return _area((n, s, w, e)) if n > s and e > w else 0.0


def load_boxes(state_dir):
    try:
        with open(os.path.join(state_dir, BOXES_FILE)) as f:
            return [tuple(float(x) for x in b) for b in json.load(f)][:MAX_BOXES]
    except (OSError, ValueError, TypeError):
        return []


def remember_box(state_dir, bbox):
    """Keep the box this run used, newest first, so the next run can find its files."""
    boxes = [b for b in load_boxes(state_dir) if b != tuple(bbox)]
    boxes.insert(0, tuple(bbox))
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = os.path.join(state_dir, BOXES_FILE + ".tmp")
        with open(tmp, "w") as f:
            json.dump([list(b) for b in boxes[:MAX_BOXES]], f)
        os.replace(tmp, os.path.join(state_dir, BOXES_FILE))
    except OSError:
        pass


def choose_bbox(need, known, online):
    """The download box for this run → (bbox, why). The smallest known box that holds the whole
    course box. None does: online → the course box itself (it gets downloaded); offline → the
    known box that overlaps the course most (at least half of it), else the course box."""
    holds = [tuple(b) for b in known if _contains(b, need)]
    if holds:
        return min(holds, key=_area), "cache"
    if online or not known:
        return tuple(need), "course"
    best = max(known, key=lambda b: _overlap(b, need))
    if _overlap(best, need) >= 0.5 * _area(need):
        return tuple(best), "cache (part of the course)"
    return tuple(need), "course"


# ---- what the result says about the weather ---------------------------------------------------
def cycle_epoch(cycle):
    """The Lab's meta cycle string ('2026-11-01 12Z') → epoch, or None (ICON/GEM have none)."""
    try:
        return dt.datetime.strptime(str(cycle), "%Y-%m-%d %HZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def summarize(status, now, budget, bbox_why):
    """The weather block of the result: which models and cycles, how old, online or not."""
    used, ages = [], []
    for m in (status or {}).get("models") or []:
        if not m.get("frames"):
            continue
        ce = cycle_epoch(m.get("cycle"))
        age_h = round((now - ce) / 3600.0, 1) if ce else None
        if age_h is not None:
            ages.append(age_h)
        used.append({"model": m.get("model"), "cycle": m.get("cycle"),
                     "cycle_epoch": int(ce) if ce else None, "age_h": age_h,
                     "frames": m.get("frames"), "expected_frames": m.get("expected_frames")})
    return {"models": used,
            "newest_age_h": min(ages) if ages else None,
            "oldest_age_h": max(ages) if ages else None,
            "online": budget.online, "downloads": budget.downloads,
            "download_budget_spent": budget.exhausted,
            "box": bbox_why}
