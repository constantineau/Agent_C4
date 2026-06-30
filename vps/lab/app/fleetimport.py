"""Automated fleet-roster ingestion from PUBLIC data — the entry list + ORC handicaps.

The fleet roster (perflab item-6 / handicap-aware fleet tactics) used to be hand-entered. Both halves
are public, so we automate them:

  - **Entry list (who's racing) — the YB tracker `RaceSetup`.** The same feed we decode for the boat
    track also carries the full entry list: per-team name, sail number, owner, skipper, model. One
    fetch (reusing the race's `tracker` block) → the roster's identities. (Falls back to the pasted /
    Opus-extracted entry list when a race has no YB tracker.)
  - **Handicaps (how they're rated) — the ORC public certificate database.** `data.orc.org` publishes
    every active cert per national authority as JSON (GPH + ToT/ToD coefficients, incl. race-specific
    columns like Bayview Mackinac's `US_BAYMAC_*`). Matched to the entry list by sail number → yacht
    name → the roster's `orc_gph` / `rating`.

Everything is a DRAFT: `import_fleet` returns a proposed roster (with per-boat match confidence + an
unmatched list); the human reviews/edits it in the Lab Fleet tab and Saves — nothing is auto-committed,
same as the rest of the Lab's ingestion. Pure-stdlib (urllib/json), cached so re-imports are cheap.
"""
import json
import os
import re
import time
import urllib.request

_TIMEOUT = float(os.environ.get("FLEET_IMPORT_TIMEOUT_S", "50"))
_ORC_TTL = float(os.environ.get("ORC_CACHE_TTL_S", "86400"))   # the country dump changes slowly
_YB_HOSTS = {"yb", "bycmack", "ybtracking", "yellowbrick"}
_ORC_URL = "https://data.orc.org/public/WPub.dll?action=DownRMS&CountryId={cc}&ext=json"

# race-specific ORC ToT/ToD columns (the cert carries these precomputed). Generic default = the
# single-number offshore handicap; a known race maps to its dedicated column for exactness.
_RACE_ORC_COLS = {
    "bayview_mackinac": {"cove": ("US_BAYMAC_CV_TOT", "US_BAYMAC_CV_TOD"),
                         "shore": ("US_BAYMAC_SH_TOT", "US_BAYMAC_SH_TOD")},
    "chicago_mackinac": {"": ("US_CHIMAC_AP_TOT", None)},
}
_DEFAULT_TOT = ("TMF_Offshore", "APHT")     # try these in order for the generic ToT coefficient


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _norm_sail(s):
    """Sail numbers vary in formatting (USA 60, USA-60, US60) — strip to country+digits for matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---- entry list from the YB RaceSetup -------------------------------------------------------------
def _yb_setup_url(cfg):
    race = (cfg.get("race") or "").strip()
    host = (cfg.get("host") or "cf.yb.tl").strip()
    return f"https://{host}/JSON/{race}/RaceSetup" if race else None


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def roster_from_yb(definition):
    """Parse the YB RaceSetup entry list into roster identities (no handicaps yet)."""
    cfg = (definition or {}).get("tracker") or {}
    if (cfg.get("provider") or "").lower() not in _YB_HOSTS:
        return {"ok": False, "note": "no YB tracker configured for this race (tracker.provider)"}
    url = _yb_setup_url(cfg)
    if not url:
        return {"ok": False, "note": "tracker has no race id (tracker.race)"}
    try:
        setup = json.loads(_get(url).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "note": f"YB entry list unavailable (HTTP {e.code}) — the race likely "
                "isn't published yet (re-check nearer race time), or paste the entry list below"}
    except Exception as e:
        return {"ok": False, "note": f"YB RaceSetup fetch failed: {type(e).__name__}"}
    teams = setup.get("teams") or []
    if not teams:
        return {"ok": False, "note": "YB race not published yet (no entry list) — re-check nearer race time"}
    entries = []
    for t in teams:
        nm = (t.get("name") or "").strip()
        if not nm:
            continue
        entries.append({"boat": nm, "sail": (t.get("sail") or "").strip(),
                        "cls": (t.get("model") or "").strip(),
                        "owner": (t.get("owner") or t.get("captain") or "").strip(),
                        "division": (t.get("division") or "").strip(),
                        "orc_gph": None, "rating": None, "mmsi": None, "source": "yb"})
    return {"ok": True, "entries": entries, "count": len(entries)}


# ---- ORC handicap enrichment ----------------------------------------------------------------------
_orc_cache = {}     # cc -> (epoch, {by_sail, by_name, rows})


def _orc_dump(country):
    cc = (country or "USA").upper()
    now = time.time()
    hit = _orc_cache.get(cc)
    if hit and now - hit[0] < _ORC_TTL:
        return hit[1]
    rows = json.loads(_get(_ORC_URL.format(cc=cc)).decode("utf-8-sig")).get("rms") or []
    idx = {"by_sail": {}, "by_name": {}, "n": len(rows)}
    for r in rows:
        s = _norm_sail(r.get("SailNo"))
        n = _norm(r.get("YachtName"))
        if s:
            idx["by_sail"].setdefault(s, r)
        if n:
            idx["by_name"].setdefault(n, r)
    _orc_cache[cc] = (now, idx)
    return idx


def _tot_cols_for_race(definition, course_id=None):
    """Pick the ORC ToT/ToD column pair for this race/course (race-specific if known, else generic)."""
    rid = _norm(definition.get("race_id") or definition.get("name") or "")
    for key, courses in _RACE_ORC_COLS.items():
        if _norm(key) in rid:
            cid = _norm(course_id or "")
            for ck, cols in courses.items():
                if not ck or ck in cid:
                    return cols
            return next(iter(courses.values()))
    return (None, None)


def _orc_handicap(rec, tot_col):
    gph = rec.get("GPH")
    rating = None
    if tot_col and rec.get(tot_col) is not None:
        rating = rec.get(tot_col)
    else:
        for c in _DEFAULT_TOT:
            if rec.get(c) is not None:
                rating = rec.get(c)
                break
    try:
        gph = float(gph) if gph is not None else None
    except (TypeError, ValueError):
        gph = None
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    return gph, rating


def enrich_from_orc(entries, country="USA", definition=None, course_id=None):
    """Fill orc_gph + rating (and tidy class) on each entry by matching to the ORC cert DB. Match by
    sail number, then yacht name. Returns the enriched entries + match stats; unmatched keep their
    identity (the human can fill the handicap by hand)."""
    try:
        idx = _orc_dump(country)
    except Exception as e:
        return {"ok": False, "note": f"ORC database fetch failed ({country}): {type(e).__name__}",
                "entries": entries}
    tot_col, _tod = _tot_cols_for_race(definition or {}, course_id) if definition else (None, None)
    matched = 0
    for e in entries:
        rec = idx["by_sail"].get(_norm_sail(e.get("sail"))) or idx["by_name"].get(_norm(e.get("boat")))
        if not rec:
            e["orc_match"] = None
            continue
        gph, rating = _orc_handicap(rec, tot_col)
        if gph is not None:
            e["orc_gph"] = gph
        if rating is not None:
            e["rating"] = rating
        if not e.get("cls") and rec.get("Class"):
            e["cls"] = rec.get("Class")
        e["source"] = (e.get("source") or "") + "+orc" if e.get("source") else "orc"
        e["orc_match"] = {"by": "sail" if _norm_sail(e.get("sail")) in idx["by_sail"] else "name",
                          "yacht": rec.get("YachtName"), "sail": rec.get("SailNo"),
                          "class": rec.get("Class"), "col": tot_col or "GPH/offshore"}
        matched += 1
    return {"ok": True, "entries": entries, "matched": matched, "total": len(entries),
            "orc_country": country, "orc_certs": idx["n"], "tot_col": tot_col}


def import_fleet(definition, source="both", country="USA", course_id=None):
    """Orchestrate a DRAFT roster from public data. `source`: 'yb' (entry list only), 'orc' (enrich the
    existing roster), or 'both' (YB entry list → ORC handicaps). Returns the proposed roster for human
    review — NOT saved."""
    src = (source or "both").lower()
    if src in ("yb", "both"):
        r = roster_from_yb(definition)
        if not r.get("ok"):
            return r
        entries = r["entries"]
    else:
        entries = [dict(e) for e in (definition.get("fleet") or [])]
        if not entries:
            return {"ok": False, "note": "no existing roster to enrich — import the entry list first"}
    out = {"ok": True, "source": src, "entries": entries, "count": len(entries)}
    if src in ("orc", "both"):
        en = enrich_from_orc(entries, country=country, definition=definition, course_id=course_id)
        if not en.get("ok"):
            out.update(orc_error=en.get("note"))          # entry list still returned; handicap failed
        else:
            out.update(matched=en["matched"], total=en["total"], orc_certs=en["orc_certs"],
                       tot_col=en.get("tot_col"), unmatched=[e["boat"] for e in entries
                                                             if not e.get("orc_match")])
    return out
