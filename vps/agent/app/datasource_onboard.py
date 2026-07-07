"""OnboardSource — the Pi-side data backend for the deterministic engine (Phase 9.1).

Mirrors `CloudSource` (datasource.py) but reads the boat's LOCAL data instead of the cloud
TimescaleDB, so the SAME engine modules (navigator / routing / tactics / fatigue + the live
polar target) run unchanged onboard — which is what makes them legal in-race under RRS 41
(the boat's own computer crunching its own sensors is not an "outside source").

Where the data comes from onboard:
  - **telemetry history** — the Phase-2 full-resolution SQLite archive (`readings` table on the
    `sk_archive` volume, written by pi/archiver). Read-only here.
  - **freshest live value** — an optional in-process Signal K WS subscriber keeps an in-memory
    latest-per-(path, source) cache so current values have lower latency than the ~2-s archive
    flush. If the WS is unavailable the archive is used instead, so the engine still works.
  - **polars** — parsed once from the committed ORC polar file (`polars_sr33.sql`); no DB.
  - **course marks** — a small local SQLite store (the boat has no `waypoints` Postgres table);
    holds the generated practice course (and any loaded course).

Every method returns **raw SI** values and **epoch-second** timestamps, identical in shape to
`CloudSource`, so the modules' unit conversions produce byte-identical outputs to the cloud path.

Selected by env `DATA_SOURCE=onboard` (datasource.active() imports this lazily).
"""
import math
import os
import re
import sqlite3
import threading
import time as _time
from datetime import datetime, timedelta, timezone

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
_MS_TO_KN = 1.943844


def _mmsi_from_context(ctx):
    """Pull the numeric MMSI out of an AIS vessel context urn, else None.

    e.g. 'vessels.urn:mrn:imo:mmsi:366123456' -> 366123456. Own ship is a uuid context, so it
    (correctly) returns None and is never mistaken for a target. Mirrors pi/uplink/uplink.py."""
    if ctx and "mmsi:" in ctx:
        tail = ctx.split("mmsi:")[-1].strip()
        return int(tail) if tail.isdigit() else None
    return None


ARCHIVE_DB = os.environ.get("ARCHIVE_DB", "/var/lib/sr33/archive/archive.db")
ENGINE_DB = os.environ.get("ENGINE_DB", "/var/lib/sr33/engine/engine.db")
POLARS_FILE = os.environ.get("POLARS_FILE", "/srv/polars_sr33.sql")
SIGNALK_WS = os.environ.get(
    "SIGNALK_WS", "ws://localhost:3010/signalk/v1/stream?subscribe=all"
)
# The live SK cache is a latency optimisation; default on, but the engine works without it.
LIVE_ENABLED = os.environ.get("ONBOARD_LIVE_WS", "true").strip().lower() == "true"
LIVE_TTL_S = float(os.environ.get("ONBOARD_LIVE_TTL_S", "12"))  # ignore stale cache entries


def _epoch(iso):
    """ISO8601 (archive `time`, always UTC with a trailing Z) -> epoch seconds, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cutoff_str(minutes):
    """A second-precision UTC cutoff string that lexicographically pre-filters the archive.

    Archive times are zero-padded ISO8601 with a fractional part + `Z`; a fraction-less cutoff
    is a prefix of any same-second row, so `time > cutoff` is safe + slightly inclusive. Callers
    re-filter precisely by parsed epoch."""
    c = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return c.strftime("%Y-%m-%dT%H:%M:%S"), c.timestamp()


class OnboardSource:
    """Local Pi data backend — same interface as CloudSource, byte-identical outputs."""

    def __init__(self):
        self._archive = sqlite3.connect(
            f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=30, check_same_thread=False
        )
        self._archive.row_factory = sqlite3.Row
        self._engine = self._open_engine()
        self._live = {}            # (path, source) -> (epoch, value)  [own ship only]
        self._ais = {}             # mmsi -> {mmsi, time, name, lat, lon, sog(kn), cog(deg)}
        self._self_ctx = None      # the SK 'self' context urn, from the hello frame
        self._live_lock = threading.Lock()
        self._polars = _load_polars()
        if LIVE_ENABLED:
            self._start_live()

    # --- local marks store (writable) --------------------------------------
    def _open_engine(self):
        os.makedirs(os.path.dirname(ENGINE_DB), exist_ok=True)
        conn = sqlite3.connect(ENGINE_DB, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS marks ("
            "route TEXT NOT NULL, seq INTEGER NOT NULL, name TEXT NOT NULL, "
            "lat REAL NOT NULL, lon REAL NOT NULL, PRIMARY KEY (route, seq))"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        return conn

    # --- live Signal K cache (optional, lowest-latency current values) ------
    def _start_live(self):
        t = threading.Thread(target=self._live_run, name="onboard-sk-live", daemon=True)
        t.start()

    def _live_run(self):
        import asyncio
        try:
            import websockets
        except ImportError:
            print("[onboard] websockets not installed; live cache disabled "
                  "(archive fallback)", flush=True)
            return

        async def loop():
            while True:
                try:
                    async with websockets.connect(SIGNALK_WS, ping_interval=20) as ws:
                        print(f"[onboard] live SK cache connected {SIGNALK_WS}", flush=True)
                        async for msg in ws:
                            self._ingest_live(msg)
                except Exception as exc:
                    print(f"[onboard] live SK WS error ({exc}); retry 3s", flush=True)
                    await asyncio.sleep(3)

        asyncio.new_event_loop().run_until_complete(loop())

    def _ingest_live(self, msg):
        import json
        try:
            data = json.loads(msg)
        except ValueError:
            return
        # The hello frame names the self context; remember it so own-ship deltas can be told
        # apart from other vessels heard on AIS (subscribe=all delivers both).
        if "self" in data and "updates" not in data:
            self._self_ctx = data["self"]
            return
        ctx = data.get("context")
        mmsi = _mmsi_from_context(ctx)
        is_other = bool(ctx) and ctx != self._self_ctx and mmsi is not None
        now = _time.time()
        with self._live_lock:
            if is_other:
                # An AIS target: accumulate per-MMSI, NEVER into the own-ship live cache.
                for upd in data.get("updates", []):
                    for v in upd.get("values", []):
                        self._record_ais(mmsi, now, v.get("path"), v.get("value"))
                return
            for upd in data.get("updates", []):
                source = (upd.get("$source")
                          or (upd.get("source") or {}).get("label") or "unknown")
                for v in upd.get("values", []):
                    path, val = v.get("path"), v.get("value")
                    if not path:
                        continue
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        self._live[(path, source)] = (now, float(val))
                    elif isinstance(val, dict):  # flatten position/attitude like the archiver
                        for k, sub in val.items():
                            if isinstance(sub, (int, float)) and not isinstance(sub, bool):
                                self._live[(f"{path}.{k}", source)] = (now, float(sub))

    def _record_ais(self, mmsi, now, path, value):
        """Accumulate the fields we need from one AIS target's delta, in kn / deg true —
        the same shape the cloud uplink writes to `ais_targets` (caller holds _live_lock)."""
        t = self._ais.setdefault(mmsi, {"mmsi": mmsi})
        t["time"] = now
        if path in ("name", "") and isinstance(value, str) and value:
            t["name"] = value
        elif path == "navigation.position" and isinstance(value, dict):
            if isinstance(value.get("latitude"), (int, float)):
                t["lat"] = float(value["latitude"])
            if isinstance(value.get("longitude"), (int, float)):
                t["lon"] = float(value["longitude"])
        elif path == "navigation.speedOverGround" and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            t["sog"] = round(float(value) * _MS_TO_KN, 2)
        elif path == "navigation.courseOverGroundTrue" and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            t["cog"] = round(math.degrees(float(value)) % 360, 1)

    def _live_fresh(self, path):
        """[(source, epoch, value)] for a path from the live cache, only entries within TTL."""
        cut = _time.time() - LIVE_TTL_S
        with self._live_lock:
            return [(s, e, val) for (p, s), (e, val) in self._live.items()
                    if p == path and e >= cut]

    # --- CloudSource interface --------------------------------------------
    def latest_value(self, path):
        """Freshest raw SI value (any source) for a path — live cache first, then archive."""
        live = self._live_fresh(path)
        if live:
            return max(live, key=lambda r: r[1])[2]
        row = self._archive.execute(
            "SELECT value FROM readings WHERE boat_id=? AND path=? AND value IS NOT NULL "
            "ORDER BY time DESC LIMIT 1", (BOAT_ID, path),
        ).fetchone()
        return row["value"] if row else None

    def series(self, path, minutes):
        """[(epoch_s, raw_value)] for a path over the window, all sources, time-ordered."""
        cut_s, cut_e = _cutoff_str(minutes)
        rows = self._archive.execute(
            "SELECT time, value FROM readings WHERE boat_id=? AND path=? AND value IS NOT NULL "
            "AND time > ? ORDER BY time", (BOAT_ID, path, cut_s),
        ).fetchall()
        out = []
        for r in rows:
            e = _epoch(r["time"])
            if e is not None and e >= cut_e:
                out.append((e, float(r["value"])))
        return out

    def series_by_source(self, path, minutes):
        """[(source, epoch_s, raw_value)] for a path over the window, time-ordered."""
        cut_s, cut_e = _cutoff_str(minutes)
        rows = self._archive.execute(
            "SELECT source, time, value FROM readings WHERE boat_id=? AND path=? "
            "AND value IS NOT NULL AND time > ? ORDER BY time", (BOAT_ID, path, cut_s),
        ).fetchall()
        out = []
        for r in rows:
            e = _epoch(r["time"])
            if e is not None and e >= cut_e:
                out.append((r["source"], e, float(r["value"])))
        return out

    def best_angles(self, tws_kn):
        """(optimal_upwind_twa, optimal_downwind_twa) at the nearest TWS; None if absent.
        Matches CloudSource: nearest TWS, then max target_vmg."""
        def pick(predicate):
            cand = [p for p in self._polars if predicate(p["twa"]) and p["target_vmg"] is not None]
            if not cand:
                return None
            cand.sort(key=lambda p: (abs(p["tws"] - tws_kn), -p["target_vmg"]))
            return cand[0]["twa"]
        return pick(lambda a: a < 90), pick(lambda a: a > 90)

    def polars_stw(self):
        """[(tws, twa, target_stw)] across the whole polar table (callers filter NULL)."""
        return [(p["tws"], p["twa"], p["target_stw"]) for p in self._polars]

    def polar_nearest(self, tws, twa):
        """Nearest polar bucket by abs(tws-)+abs(twa-): {tws,twa,target_stw,target_vmg} or None."""
        if not self._polars:
            return None
        best = min(self._polars,
                   key=lambda p: abs(p["tws"] - tws) + abs(p["twa"] - abs(twa)))
        return {"tws": best["tws"], "twa": best["twa"],
                "target_stw": best["target_stw"], "target_vmg": best["target_vmg"]}

    def marks(self, route):
        """Course waypoints in sequence: [{seq,name,lat,lon}]."""
        rows = self._engine.execute(
            "SELECT seq, name, lat, lon FROM marks WHERE route=? ORDER BY seq", (route,),
        ).fetchall()
        return [dict(r) for r in rows]

    def save_course(self, route, marks):
        """Replace `route` with these [(seq, name, lat, lon)] marks."""
        self._engine.execute("DELETE FROM marks WHERE route=?", (route,))
        self._engine.executemany(
            "INSERT INTO marks (route, seq, name, lat, lon) VALUES (?,?,?,?,?)",
            [(route, seq, name, mlat, mlon) for seq, name, mlat, mlon in marks],
        )
        self._engine.commit()

    def save_practice_course(self, marks):
        """Replace the 'practice' route with these [(seq, name, lat, lon)] marks."""
        self.save_course("practice", marks)

    def save_fleet(self, blob):
        """Persist the loaded fleet homework (roster + scoring + own rating) as a JSON blob in the
        engine SQLite `kv` store (key 'race_fleet'). Replaces any prior roster."""
        import json
        self._engine.execute(
            "INSERT INTO kv (key, value) VALUES ('race_fleet', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(blob),))
        self._engine.commit()

    def get_fleet(self):
        """The loaded fleet homework blob ({fleet, scoring, own}) or {} if none."""
        import json
        row = self._engine.execute("SELECT value FROM kv WHERE key = 'race_fleet'").fetchone()
        return json.loads(row["value"]) if row else {}

    def save_playbook(self, blob):
        """Persist the frozen playbook bundle (Lab-2 `c4.playbook/v1`) in the engine SQLite `kv`
        store (key 'race_playbook'). The route-deviation core reads the active variant's frozen
        track from it. Replaces any prior playbook."""
        import json
        self._engine.execute(
            "INSERT INTO kv (key, value) VALUES ('race_playbook', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(blob),))
        self._engine.commit()

    def get_playbook(self):
        """The loaded playbook bundle or {} if none aboard."""
        import json
        row = self._engine.execute("SELECT value FROM kv WHERE key = 'race_playbook'").fetchone()
        return json.loads(row["value"]) if row else {}

    def save_sail_state(self, blob):
        """Persist the crew-set sail state ({hoisted, out_of_service, ts}) in the engine kv —
        the matcher's crew-armed signals (a blown kite has no instrument)."""
        import json
        self._engine.execute(
            "INSERT INTO kv (key, value) VALUES ('sail_state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(blob),))
        self._engine.commit()

    def get_sail_state(self):
        """The crew-set sail state or {} if never set."""
        import json
        row = self._engine.execute("SELECT value FROM kv WHERE key = 'sail_state'").fetchone()
        return json.loads(row["value"]) if row else {}

    def ais_targets(self, max_age_min):
        """Latest AIS observation per MMSI within the window — other-vessel Signal K contexts
        captured by the live cache. Shape-matched to CloudSource: [{mmsi, name, lat, lon,
        sog(kn), cog(deg true), time(epoch)}]; targets without a position fix are skipped."""
        cut = _time.time() - max_age_min * 60.0
        out = []
        with self._live_lock:
            for mmsi, t in self._ais.items():
                if t.get("time", 0) < cut or t.get("lat") is None or t.get("lon") is None:
                    continue
                out.append({"mmsi": mmsi, "name": t.get("name"),
                            "lat": t["lat"], "lon": t["lon"],
                            "sog": t.get("sog"), "cog": t.get("cog"), "time": t.get("time")})
        return out

    # --- onboard-only helpers for the live instrument strip ---------------
    # (the cloud builds these in tools.py off Postgres; onboard we read the archive + live cache)
    def latest_per_source(self, paths, max_age_min):
        """[{path, source, value, epoch}] — latest reading per (path, source) within the window,
        merging the live cache (freshest) over the archive."""
        cut_s, cut_e = _cutoff_str(max_age_min)
        placeholders = ",".join("?" * len(paths))
        rows = self._archive.execute(
            f"SELECT path, source, value, time FROM readings WHERE boat_id=? "
            f"AND path IN ({placeholders}) AND value IS NOT NULL AND time > ? ORDER BY time",
            (BOAT_ID, *paths, cut_s),
        ).fetchall()
        best = {}
        for r in rows:
            e = _epoch(r["time"])
            if e is None or e < cut_e:
                continue
            k = (r["path"], r["source"])
            if k not in best or e > best[k][0]:
                best[k] = (e, float(r["value"]))
        with self._live_lock:
            for (p, s), (e, val) in self._live.items():
                if p in paths and e >= cut_e and ((p, s) not in best or e > best[(p, s)][0]):
                    best[(p, s)] = (e, val)
        return [{"path": p, "source": s, "value": v, "epoch": e}
                for (p, s), (e, v) in best.items()]

    def sources(self, max_age_min):
        """[{source, last_epoch, paths, samples}] active in the window (for /sources).

        Merges the live SK cache (what's reporting right now) over the archive — important on
        the bench, where the sample log's source timestamps fall outside any wall-clock window."""
        cut_s, cut_e = _cutoff_str(max_age_min)
        rows = self._archive.execute(
            "SELECT source, max(time) AS last, count(DISTINCT path) AS paths, count(*) AS n "
            "FROM readings WHERE boat_id=? AND time > ? GROUP BY source ORDER BY source",
            (BOAT_ID, cut_s),
        ).fetchall()
        agg = {}
        for r in rows:
            e = _epoch(r["last"])
            if e is None or e < cut_e:
                continue
            agg[r["source"]] = {"source": r["source"], "last_epoch": e,
                                "paths": r["paths"], "samples": r["n"]}
        with self._live_lock:
            live_paths, live_last = {}, {}
            for (p, s), (e, _v) in self._live.items():
                if e < cut_e:
                    continue
                live_paths.setdefault(s, set()).add(p)
                live_last[s] = max(live_last.get(s, 0), e)
        for s, paths in live_paths.items():
            cur = agg.get(s)
            if cur is None:
                agg[s] = {"source": s, "last_epoch": live_last[s],
                          "paths": len(paths), "samples": len(paths)}
            else:
                cur["last_epoch"] = max(cur["last_epoch"], live_last[s])
                cur["paths"] = max(cur["paths"], len(paths))
        return sorted(agg.values(), key=lambda r: r["source"])


_POLAR_RE = re.compile(
    r"\(\s*'[^']*'\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*"
    r"([0-9.]+|NULL)\s*\)"
)


def _load_polars():
    """Parse the committed ORC polar SQL into [{tws,twa,target_stw,target_vmg}].

    The single canonical polar source is `polars_sr33.sql` (generated by build_speed_guide.py);
    onboard we parse its INSERT tuples rather than maintain a second copy."""
    try:
        with open(POLARS_FILE) as f:
            text = f.read()
    except OSError as exc:
        print(f"[onboard] polars file unavailable ({exc}); polar tools will be empty",
              flush=True)
        return []
    out = []
    for tws, twa, stw, vmg in _POLAR_RE.findall(text):
        out.append({"tws": float(tws), "twa": float(twa), "target_stw": float(stw),
                    "target_vmg": None if vmg == "NULL" else float(vmg)})
    print(f"[onboard] loaded {len(out)} polar points from {POLARS_FILE}", flush=True)
    return out
