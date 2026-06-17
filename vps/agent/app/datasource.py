"""Pluggable data backend for the deterministic engine (Phase 9.0).

The engine modules (navigator / routing / tactics / sails / fatigue + the live polar target) read
telemetry and polars through this layer instead of querying TimescaleDB directly, so the SAME engine
code can run:
  - in the CLOUD (`CloudSource`: TimescaleDB) — today's behavior, byte-for-byte unchanged;
  - ONBOARD the Pi (`OnboardSource`: local SQLite archive + Signal K live) — Phase 9.1, legal in-race.

Selected by env `DATA_SOURCE` (`cloud` | `onboard`, default `cloud`). Methods return **raw SI** values
and epoch-second timestamps; the unit conversions stay in the modules, so engine behavior is identical
to the pre-9.0 code.

NOTE: the archive-mining polar analysis (`polar_tool.py`, Timescale `time_bucket`) is a between-races
**C4 Performance Lab** tool and stays cloud-only — it is not part of the onboard engine, so it is not
abstracted here.
"""
import os

from .db import pool

BOAT_ID = os.environ.get("BOAT_ID", "sr33")


class CloudSource:
    """TimescaleDB-backed source — reproduces the exact queries the modules used pre-9.0."""

    def latest_value(self, path):
        """Freshest raw SI value (any source) for a path, or None."""
        with pool.connection() as conn:
            r = conn.execute(
                "SELECT value FROM telemetry_raw WHERE boat_id=%s AND path=%s "
                "AND value IS NOT NULL ORDER BY time DESC LIMIT 1", (BOAT_ID, path),
            ).fetchone()
        return r["value"] if r else None

    def series(self, path, minutes):
        """[(epoch_s, raw_value)] for a path over the window, all sources, time-ordered."""
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT extract(epoch FROM time) AS t, value FROM telemetry_raw "
                "WHERE boat_id=%s AND path=%s AND value IS NOT NULL "
                "AND time > now() - %s::interval ORDER BY time",
                (BOAT_ID, path, f"{minutes} minutes"),
            ).fetchall()
        return [(float(r["t"]), float(r["value"])) for r in rows]

    def series_by_source(self, path, minutes):
        """[(source, epoch_s, raw_value)] for a path over the window, time-ordered."""
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, value, extract(epoch FROM time) AS t FROM telemetry_raw "
                "WHERE boat_id=%s AND path=%s AND time > now() - %s::interval AND value IS NOT NULL "
                "ORDER BY time", (BOAT_ID, path, f"{minutes} minutes"),
            ).fetchall()
        return [(r["source"], float(r["t"]), float(r["value"])) for r in rows]

    def best_angles(self, tws_kn):
        """(optimal_upwind_twa, optimal_downwind_twa) from the polar at the nearest TWS; None if absent."""
        with pool.connection() as conn:
            up = conn.execute(
                "SELECT twa FROM polars WHERE boat_id=%s AND twa<90 "
                "ORDER BY abs(tws-%s), target_vmg DESC LIMIT 1", (BOAT_ID, tws_kn)).fetchone()
            dn = conn.execute(
                "SELECT twa FROM polars WHERE boat_id=%s AND twa>90 "
                "ORDER BY abs(tws-%s), target_vmg DESC LIMIT 1", (BOAT_ID, tws_kn)).fetchone()
        return (up["twa"] if up else None), (dn["twa"] if dn else None)

    def polars_stw(self):
        """[(tws, twa, target_stw)] across the whole polar table (callers filter NULL target_stw)."""
        with pool.connection() as conn:
            rows = conn.execute("SELECT tws, twa, target_stw FROM polars WHERE boat_id=%s",
                                (BOAT_ID,)).fetchall()
        return [(r["tws"], r["twa"], r["target_stw"]) for r in rows]

    def polar_nearest(self, tws, twa):
        """Nearest polar bucket by abs(tws-)+abs(twa-): {tws,twa,target_stw,target_vmg} or None."""
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT tws, twa, target_stw, target_vmg, (abs(tws-%s)+abs(twa-%s)) AS dist "
                "FROM polars WHERE boat_id=%s ORDER BY dist LIMIT 1",
                (tws, abs(twa), BOAT_ID),
            ).fetchone()
        return dict(row) if row else None

    def marks(self, route):
        """Course waypoints in sequence: [{seq,name,lat,lon}]."""
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT seq, name, lat, lon FROM waypoints WHERE route=%s ORDER BY seq", (route,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_practice_course(self, marks):
        """Replace the 'practice' route with these [(seq, name, lat, lon)] marks."""
        with pool.connection() as conn:
            conn.execute("DELETE FROM waypoints WHERE route='practice'")
            for seq, name, mlat, mlon in marks:
                conn.execute("INSERT INTO waypoints (route, seq, name, lat, lon) VALUES "
                             "('practice',%s,%s,%s,%s)", (seq, name, mlat, mlon))
            conn.commit()


_SOURCE = None


def active():
    """The configured data source (memoized). `DATA_SOURCE=onboard` selects the Pi backend (9.1)."""
    global _SOURCE
    if _SOURCE is None:
        kind = os.environ.get("DATA_SOURCE", "cloud").strip().lower()
        if kind == "onboard":
            from .datasource_onboard import OnboardSource  # Phase 9.1 (not present until then)
            _SOURCE = OnboardSource()
        else:
            _SOURCE = CloudSource()
    return _SOURCE
