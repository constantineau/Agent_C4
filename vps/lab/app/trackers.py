"""Race tracker providers (#310): where retro, debrief tracks and the fleet entry list get tracks.

A race definition's `tracker.provider` picks the provider. Each one answers three questions:

  - `entries(definition)`     — the fleet entry list (who's racing), for the Fleet tab import.
  - `boat_track(definition, boat)` — OUR boat's sailed track, for the debrief.
  - `fleet(ref, definition)`  — every boat's track (+ entries, results when known), for the retro.

Providers:

  - **yb** (YB Tracking; aliases ybtracking/yellowbrick/bycmack) — fetched from the public feed.
    Behaviour is exactly the pre-#310 code, moved here.
  - **file** (aliases upload/gpx/csv) — the fleet's tracks come from a file the user uploads: a
    multi-track GPX (one `<trk>` per boat, named), several GPX files (one per boat), or a CSV with
    boat,time,lat,lon rows. No account, no API key. Chosen because the other trackers sailboat
    regattas use (TracTrac, Kattack, Sailing Analytics/SAP, the event's own) publish no documented
    public API; most can export GPX from their replay, and Expedition/Adrena/phones write GPX too.
    Once a fleet file is in the retro archive, the debrief and the entry list read it from there.

Any other provider name gets a plain note, never a guess. Adding a tracker = one class here
with the same three methods, plus its names in `_PROVIDERS`.
"""
import csv
import io
import json
import os
import re
import xml.etree.ElementTree as ET

from shared import race_def

from . import retrostore as rs
from . import track as track_mod

YB_NAMES = ("yb", "ybtracking", "yellowbrick", "bycmack")
FILE_NAMES = ("file", "upload", "gpx", "csv")

NO_FILE = "No fleet file for this race yet. Upload one in Debrief, Fleet retro."
FILE_HINT = "Upload the fleet tracks: GPX or CSV."
NO_TIMES = "No fixes with a usable time. Times need a zone, or set the race's time zone."


def provider_name(definition) -> str:
    return str(((definition or {}).get("tracker") or {}).get("provider") or "").strip().lower()


def unknown_note(prov: str) -> str:
    return f"Can't read {prov} tracks. Upload a GPX or CSV of the fleet instead."


class YB:
    name = "yb"
    label = "YB"
    fetches = True          # tracks come off the web; a race id is needed

    @staticmethod
    def ref(cfg):
        return str(cfg.get("race") or "").strip() or None

    @staticmethod
    def entries(definition):
        from . import fleetimport
        return fleetimport.roster_from_yb(definition)

    @staticmethod
    def boat_track(definition, boat=None):
        return track_mod.fetch_yb_track(definition, boat)

    @staticmethod
    def fleet(race_id, definition=None):
        return yb_fleet(race_id)


class File:
    name = "file"
    label = "fleet file"
    fetches = False         # nothing to fetch: the user uploads the file

    @staticmethod
    def ref(cfg):
        return None

    @staticmethod
    def entries(definition):
        rid = (definition or {}).get("race_id")
        rows = rs.get_entries(rid) if rid else []
        if not rows:
            return {"ok": False, "note": NO_FILE}
        entries = [{"boat": e["boat"], "sail": e.get("sail") or "", "cls": e.get("model") or "",
                    "owner": e.get("owner") or "", "division": e.get("division") or "",
                    "orc_gph": None, "rating": None, "mmsi": None, "source": "file"}
                   for e in rows if (e.get("boat") or "").strip()]
        return {"ok": True, "entries": entries, "count": len(entries)}

    @staticmethod
    def boat_track(definition, boat=None):
        d = definition or {}
        rid = d.get("race_id")
        rows = rs.get_entries(rid) if rid else []
        if not rows:
            return {"ok": False, "note": NO_FILE}
        want = (boat or (d.get("tracker") or {}).get("boat")
                or (d.get("boat") or {}).get("name") or "").strip()
        hit = next((e for e in rows if (e.get("boat") or "").strip().lower() == want.lower()), None) \
            if want else None
        fixes = rs.get_track(rid, hit["team_id"]) if hit else None
        if not fixes:
            return {"ok": False, "note": f"could not match boat {want!r} in the fleet file",
                    "boats": [e.get("boat") for e in rows][:60]}
        track_mod._derive_sog_cog(fixes)
        return {"ok": True, "source": "file", "boat": hit["boat"], "matched_by": "name",
                "race": rid, "fixes": fixes, "n": len(fixes)}

    @staticmethod
    def fleet(race_id, definition=None):
        return {"ok": False, "note": FILE_HINT}


_PROVIDERS = {**{n: YB for n in YB_NAMES}, **{n: File for n in FILE_NAMES}}


def get(definition_or_name):
    """The provider for a race definition (or a provider name); None when unknown or unset."""
    prov = (definition_or_name.strip().lower() if isinstance(definition_or_name, str)
            else provider_name(definition_or_name))
    return _PROVIDERS.get(prov)


def entries(definition):
    """The tracker's entry list for a race, or a plain note why there is none."""
    prov = provider_name(definition)
    p = get(prov)
    if p is None:
        return {"ok": False, "note": unknown_note(prov) if prov and prov != "sample"
                else "No tracker set for this race."}
    return p.entries(definition)


def boat_track(definition, boat=None):
    """Our boat's track from the race's tracker, or a plain note why not."""
    prov = provider_name(definition)
    p = get(prov)
    if p is None:
        return {"ok": False, "note": (f"Can't read {prov} tracks. Upload our GPX." if prov
                                      and prov != "sample" else "No tracker set for this race.")}
    return p.boat_track(definition, boat)


# ---- YB: the full fleet (was retro.ingest_race's fetch half) ----------------------------------
_YB_HOST = "https://cf.yb.tl"


def _yb(path, raw=False):
    return track_mod._yb_get(f"{_YB_HOST}/{path}", raw=raw)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def yb_fleet(race_id: str) -> dict:
    """Fetch one YB race: RaceSetup entries + AllPositions3 tracks (anchored and matched by each
    team's GetPositions latest fix, within 1 nm, nearest first) + the leaderboard verbatim.
    Returns the normalized fleet `retro.store_fleet` writes."""
    setup = _yb(f"JSON/{race_id}/RaceSetup")
    teams = setup.get("teams") or []
    if not teams:
        return {"ok": False, "note": "race has no teams (unpublished/dormant feed)"}
    tags = {t.get("id"): t for t in setup.get("tags") or []}
    out_teams = []
    for t in teams:
        division = json.dumps([tags[i]["name"] for i in (t.get("tags") or []) if i in tags])
        out_teams.append({**t, "division": division, "tcf": _f(t.get("tcf1"))})

    binb = _yb(f"BIN/{race_id}/AllPositions3", raw=True)
    blocks = track_mod._decode_allpositions3(binb, len(teams))
    anchors = {}
    try:
        gp = _yb(f"API3/Race/{race_id}/GetPositions?t=0")
        for tm in gp.get("teams") or []:
            ps = tm.get("positions") or []
            if ps:
                p = max(ps, key=lambda x: x.get("gpsAtMillis") or 0)
                if p.get("gpsAtMillis"):
                    anchors[(tm.get("name") or "").lower()] = (
                        p["latitude"], p["longitude"], p["gpsAtMillis"] / 1000.0)
    except Exception:
        pass

    tracks, used, unanchored = {}, set(), []
    for t in teams:
        a = anchors.get((t.get("name") or "").lower())
        if not a:
            unanchored.append(t.get("name"))
            continue
        best, bestd = None, 1e9
        for bi, fx in enumerate(blocks):
            if bi in used or not fx:
                continue
            d = track_mod._hav_nm((fx[-1]["lat"], fx[-1]["lon"]), (a[0], a[1]))
            if d < bestd:
                best, bestd = bi, d
        if best is None or bestd > 1.0:
            unanchored.append(t.get("name"))
            continue
        used.add(best)
        fx = blocks[best]
        tmax = fx[-1]["t"]
        fixes = [{"lat": f["lat"], "lon": f["lon"], "t": a[2] - (tmax - f["t"]),
                  "sog": None, "cog": None} for f in fx]
        track_mod._derive_sog_cog(fixes)
        tracks[t.get("id")] = fixes

    results, results_note = [], None
    try:
        lb = _yb(f"JSON/{race_id}/leaderboard")
        lb_tags = lb.get("tags") or []
        # tag identity: by id when present, else zip against the setup's lb-enabled tags in order
        ordered = [t for t in sorted((setup.get("tags") or []), key=lambda x: x.get("sort") or 0)
                   if t.get("lb")]
        for i, lt in enumerate(lb_tags):
            tag = tags.get(lt.get("id")) or (ordered[i] if i < len(ordered) else {})
            name = tag.get("name") or f"tag{lt.get('id') or i}"
            for r in lt.get("teams") or []:
                results.append({"team_id": r.get("id"), "division": name,
                                "elapsed": _f(r.get("elapsed")), "corrected": _f(r.get("cElapsed")),
                                "tcf": _f(r.get("tcf")), "rank": r.get("rankR"),
                                "finished": bool(r.get("finished")),
                                "status": str(r.get("status") or "")})
    except Exception as exc:
        results, results_note = [], f"leaderboard unavailable ({type(exc).__name__}) — results skipped"

    return {"ok": True, "source": "yb", "setup": setup, "start": setup.get("start"),
            "teams": out_teams, "tracks": tracks, "results": results,
            "results_note": results_note, "blocks": len(blocks), "unmatched": unanchored}


# ---- File: GPX / CSV fleet tracks ------------------------------------------------------------
MAX_FILE_BYTES = int(os.environ.get("FLEET_FILE_MAX_BYTES", str(60 * 1024 * 1024)))

_CSV_COLS = {
    "boat": ("boat", "name", "yacht", "team", "boat name", "boatname"),
    "time": ("time", "timestamp", "datetime", "utc", "date time", "gps time", "t"),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "lng", "long", "longitude"),
    "sail": ("sail", "sail number", "sail no", "sailno", "sail #"),
    "division": ("division", "class", "fleet", "div"),
    "rank": ("rank", "place", "position", "pos"),
}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _gpx_tracks(data: bytes, stem: str):
    """[(boat, fixes)] from a GPX: one per named <trk>; an unnamed lone track takes the file name."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"{stem}: not valid GPX ({e})")
    trks = [el for el in root.iter() if _local(el.tag) == "trk"]
    if not trks:
        trks = [root]
    out = []
    for i, trk in enumerate(trks):
        name = next((c.text for c in trk if _local(c.tag) == "name" and (c.text or "").strip()), None)
        if not name:
            name = stem if len(trks) == 1 else f"{stem} {i + 1}"
        pts = []
        for el in trk.iter():
            if _local(el.tag) != "trkpt":
                continue
            try:
                lat, lon = float(el.get("lat")), float(el.get("lon"))
            except (TypeError, ValueError):
                continue
            t = next((track_mod._parse_iso(c.text) for c in el if _local(c.tag) == "time"), None)
            pts.append({"lat": lat, "lon": lon, "t": t})
        out.append((name.strip(), pts, {}))
    return out


def _csv_tracks(text: str, stem: str, tz_name: str):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"{stem}: empty CSV")
    head = [h.strip().lower() for h in rows[0]]
    col = {k: next((head.index(a) for a in names if a in head), None) for k, names in _CSV_COLS.items()}
    missing = [k for k in ("boat", "time", "lat", "lon") if col[k] is None]
    if missing:
        raise ValueError(f"{stem}: CSV needs columns boat, time, lat, lon (missing {', '.join(missing)})")
    by_boat = {}
    for r in rows[1:]:
        cell = lambda k: (r[col[k]].strip() if col[k] is not None and col[k] < len(r) else "")
        boat = cell("boat")
        if not boat:
            continue
        try:
            lat, lon = float(cell("lat")), float(cell("lon"))
        except ValueError:
            continue
        name, pts, meta = by_boat.setdefault(boat.lower(), (boat, [], {}))
        pts.append({"lat": lat, "lon": lon, "t": race_def._gun_epoch(cell("time"), tz_name)})
        for k in ("sail", "division", "rank"):
            if cell(k) and not meta.get(k):
                meta[k] = cell(k)
    return list(by_boat.values())


def parse_fleet_files(files, definition=None) -> dict:
    """Uploaded fleet tracks → the normalized fleet `retro.store_fleet` writes.

    `files` = [(filename, bytes)]. GPX: one boat per named <trk> (or one per file). CSV: one row
    per fix with boat, time, lat, lon (+ optional sail, division, rank). Times must be absolute:
    GPX times are UTC; a CSV time without a zone is read in the race's time zone, and refused
    when the race has none. Boats with no timed fix are listed, not stored."""
    d = definition or {}
    tz_name = d.get("timezone") or ""
    if not files:
        return {"ok": False, "note": FILE_HINT}
    if sum(len(b or b"") for _, b in files) > MAX_FILE_BYTES:
        return {"ok": False, "note": "File too big."}
    boats = {}
    for fname, data in files:
        stem = os.path.splitext(os.path.basename(fname or "track"))[0] or "track"
        head = (data or b"").lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
        if (fname or "").lower().endswith(".gpx") or head == b"<":
            got = _gpx_tracks(data, stem)
        else:
            got = _csv_tracks((data or b"").decode("utf-8-sig", "replace"), stem, tz_name)
        for name, pts, meta in got:
            cur = boats.setdefault(name.lower(), {"name": name, "pts": [], "meta": {}})
            cur["pts"].extend(pts)
            for k, v in meta.items():
                cur["meta"].setdefault(k, v)

    guns = {}
    for k, v in (d.get("division_starts") or {}).items():
        e = race_def._gun_epoch(v, tz_name)
        if e is not None:
            guns[str(k)] = e

    teams, tracks, results, untimed = [], {}, [], []
    for b in boats.values():
        fixes = sorted(({"lat": p["lat"], "lon": p["lon"], "t": p["t"], "sog": None, "cog": None}
                        for p in b["pts"] if p["t"] is not None
                        and -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180),
                       key=lambda f: f["t"])
        if len(fixes) < 2:
            untimed.append(b["name"])
            continue
        track_mod._derive_sog_cog(fixes)
        tid = len(teams) + 1
        div = b["meta"].get("division") or ""
        teams.append({"id": tid, "name": b["name"], "sail": b["meta"].get("sail") or "",
                      "division": div, "tcf": None, "start": guns.get(div)})
        tracks[tid] = fixes
        rank = b["meta"].get("rank")
        if rank and re.fullmatch(r"\d+", rank):
            results.append({"team_id": tid, "division": div or "overall", "elapsed": None,
                            "corrected": None, "tcf": None, "rank": int(rank), "finished": True,
                            "status": ""})
    if not teams:
        return {"ok": False, "note": NO_TIMES if untimed else "No tracks found in the file."}
    start = min(guns.values()) if guns else min(f[0]["t"] for f in tracks.values())
    return {"ok": True, "source": "file", "setup": {"source": "file", "boats": len(teams)},
            "start": start, "teams": teams, "tracks": tracks, "results": results,
            "results_note": None if results else "No finish ranks in the file, so no report.",
            "blocks": len(teams), "unmatched": untimed}
