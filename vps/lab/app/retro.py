"""Fleet retro study — ingest past races + run the optimizer for every boat (docs/RETRO_STUDY.md).

R1 (this module, ingest): YB RaceSetup (entries, start, TCFs, divisions) + the AllPositions3
full-fleet binary (decoder in `track.py`) + GetPositions (the ABSOLUTE-time anchor per team — the
binary carries only relative seconds) + the leaderboard (YB's own corrected results per division,
stored verbatim rather than recomputed) → the persistent `retrostore` archive.

R2 (also here): match every entry to its ORC cert (sail# → yacht-name, `fleetimport._orc_dump`) and
convert the cert's Allowances into an optimizer-shaped polar (`orcpolar.cert_polar`) — stored per
(race, team). Unmatched boats are reported, never silently dropped.

Block→team matching: AllPositions3 blocks come in teams[] order but DNS/sparse blocks may be
skipped, so identity is by DISTANCE — a team's GetPositions latest fix must sit within 1 nm of a
block's newest fix (the same self-validating link `fetch_yb_track` uses), greedily nearest-first.
"""
import json

from . import fleetimport as fi
from . import orcpolar
from . import retrostore as rs
from . import track as track_mod

_HOST = "https://cf.yb.tl"


def _yb(path, raw=False):
    return track_mod._yb_get(f"{_HOST}/{path}", raw=raw)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ingest_race(race_id: str) -> dict:
    """Pull one YB race into the archive: race + entries + anchored tracks + results."""
    setup = _yb(f"JSON/{race_id}/RaceSetup")
    teams = setup.get("teams") or []
    if not teams:
        return {"ok": False, "note": "race has no teams (unpublished/dormant feed)"}
    tags = {t.get("id"): t for t in setup.get("tags") or []}
    rs.upsert_race(race_id, setup, setup.get("start"))

    for t in teams:
        division = json.dumps([tags[i]["name"] for i in (t.get("tags") or []) if i in tags])
        rs.upsert_entry(race_id, {**t, "division": division, "tcf": _f(t.get("tcf1"))})

    # --- tracks: decode all blocks, anchor + match by GetPositions latest fix -------------------
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

    used, n_tracks, unanchored = set(), 0, []
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
        rs.save_track(race_id, t.get("id"), fixes)
        n_tracks += 1

    # --- results: the leaderboard verbatim (YB's own corrected order per division) --------------
    n_results, n_divisions = 0, 0
    try:
        lb = _yb(f"JSON/{race_id}/leaderboard")
        lb_tags = lb.get("tags") or []
        # tag identity: by id when present, else zip against the setup's lb-enabled tags in order
        ordered = [t for t in sorted((setup.get("tags") or []), key=lambda x: x.get("sort") or 0)
                   if t.get("lb")]
        for i, lt in enumerate(lb_tags):
            tag = tags.get(lt.get("id")) or (ordered[i] if i < len(ordered) else {})
            name = tag.get("name") or f"tag{lt.get('id') or i}"
            rows = lt.get("teams") or []
            if rows:
                n_divisions += 1
            for r in rows:
                rs.save_result(race_id, r.get("id"), name, _f(r.get("elapsed")),
                               _f(r.get("cElapsed")), _f(r.get("tcf")), r.get("rankR"),
                               bool(r.get("finished")), str(r.get("status") or ""))
                n_results += 1
    except Exception as exc:
        return {"ok": True, "race_id": race_id, "teams": len(teams), "blocks": len(blocks),
                "tracks": n_tracks, "results": 0, "divisions": 0,
                "note": f"leaderboard unavailable ({type(exc).__name__}) — results skipped",
                "unmatched_tracks": unanchored[:20]}

    return {"ok": True, "race_id": race_id, "teams": len(teams), "blocks": len(blocks),
            "tracks": n_tracks, "results": n_results, "divisions": n_divisions,
            "unmatched_tracks": unanchored[:20]}


def match_polars(race_id: str, country: str = "USA") -> dict:
    """R2: ORC cert + converted polar for every entry that matches the public dump."""
    entries = rs.get_entries(race_id)
    if not entries:
        return {"ok": False, "note": "race not ingested yet — run ingest first"}
    idx = fi._orc_dump(country)
    matched, misses = 0, []
    for e in entries:
        rec, by, conf = None, None, 0.0
        s = fi._norm_sail(e.get("sail"))
        if s and s in idx["by_sail"]:
            rec, by, conf = idx["by_sail"][s], "sail", 0.9
        else:
            n = fi._norm(e.get("boat"))
            if n and n in idx["by_name"]:
                rec, by, conf = idx["by_name"][n], "name", 0.6
        if not rec:
            misses.append(e.get("boat"))
            continue
        polar = orcpolar.cert_polar(rec)
        if not polar:
            misses.append(f"{e.get('boat')} (cert has no Allowances)")
            continue
        refno = next((rec.get(k) for k in ("RefNo", "CertNo", "FileId", "BIN") if rec.get(k)),
                     f"{rec.get('SailNo')}|{rec.get('YachtName')}")
        cert_id = rs.save_cert(country, refno, rec.get("YachtName"), rec.get("SailNo"), rec)
        rs.save_polar(race_id, e["team_id"], cert_id, polar, by, conf)
        matched += 1
    return {"ok": True, "race_id": race_id, "entries": len(entries), "matched": matched,
            "certs_in_dump": idx.get("n"), "unmatched": misses[:30],
            "unmatched_n": len(misses)}
