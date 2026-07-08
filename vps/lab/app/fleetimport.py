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

from . import extract

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


# ---- entry list from the REGATTA WEBSITE (URL / pasted text / uploaded PDF, via Opus) -------------
_ENTRY_PROMPT = (
    'Extract the race ENTRY / REGISTRATION list as JSON: {"entries":[{"boat","sail","owner","cls",'
    '"division"}]}. boat = yacht name (REQUIRED). sail = sail number ("" if none). owner = owner or '
    'skipper ("" if none). cls = boat class/design ("" if none). division = class/division/fleet '
    '("" if none). Output ONLY the JSON object. Include ONLY real entered boats — skip headers, '
    "totals, menus, ads, results columns. If there is no entry list in this content, return "
    '{"entries":[]}.')


def _llm_entry_list(text):
    """Opus → the raw entry-list rows from arbitrary page/PDF text. Monkeypatchable in tests."""
    if not extract.API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the Lab service")
    from . import llm as lab_llm
    txt, _model = lab_llm.complete(
        "You extract a sailing race entry/registration list into structured JSON for a "
        "race-strategy tool. Only real entered boats; never invent boats or handicaps.",
        _ENTRY_PROMPT + "\n\nPAGE CONTENT:\n" + (text or "")[:extract.MAX_DOC_CHARS],
        # a big fleet (Bayview Mackinac is ~180 boats) needs plenty of output room, or the JSON
        # truncates mid-array and fails to parse (env-tunable).
        max_tokens=int(os.environ.get("LAB_ENTRY_MAX_TOKENS", "20000")))
    return extract._parse_json(txt).get("entries") or []


def roster_from_text(text):
    """Extract roster identities from pasted entry-list / page text (any regatta-site format) via Opus."""
    if not (text or "").strip():
        return {"ok": False, "note": "no text to extract an entry list from"}
    try:
        raw = _llm_entry_list(text)
    except RuntimeError as e:
        return {"ok": False, "note": str(e)}
    except Exception as e:
        return {"ok": False, "note": f"entry-list extraction failed: {type(e).__name__}"}
    entries = []
    for e in raw:
        nm = (e.get("boat") or "").strip()
        if not nm:
            continue
        entries.append({"boat": nm, "sail": (e.get("sail") or "").strip(),
                        "cls": (e.get("cls") or e.get("class") or "").strip(),
                        "owner": (e.get("owner") or "").strip(),
                        "division": (e.get("division") or "").strip(),
                        "orc_gph": None, "rating": None, "mmsi": None, "source": "website"})
    if not entries:
        return {"ok": False, "note": "no boats found in that content — the page may be JS-rendered; "
                "paste the visible entry-list text or upload the entry-list PDF"}
    return {"ok": True, "entries": entries, "count": len(entries)}


# YachtScoring is a JS SPA, but its public API serves the entry list as JSON — fetch that directly
# (like YB), instead of the HTML shell a server-side fetch gets ("You need to enable JavaScript").
_YS_EVENT_RE = re.compile(r"(?:emenu/|e?id=|event/)(\d+)", re.I)
_YS_API = "https://api.yachtscoring.com/v1/public/event/{eid}/boats?page={page}&size={size}"


def roster_from_yachtscoring(event_id):
    """Pull a YachtScoring event's entry list from its public boats API (paginated JSON)."""
    entries, page, size, total = [], 1, 200, None
    while page <= 60:
        try:
            d = json.loads(_get(_YS_API.format(eid=event_id, page=page, size=size)).decode("utf-8", "replace"))
        except Exception as e:
            return {"ok": False, "note": f"YachtScoring fetch failed: {type(e).__name__}"}
        rows = d.get("rows") or []
        if total is None:
            total = d.get("count")
        for r in rows:
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            pre = str(r.get("sailPrefix") or "").strip().upper()
            num = str(r.get("sailNumber") or "").strip()
            sail = (pre + " " + num).strip() if (pre or num) else ""
            own = r.get("owner")            # a dict {firstName,lastName,...} on YachtScoring
            owner = (f"{own.get('firstName','')} {own.get('lastName','')}".strip()
                     if isinstance(own, dict) else str(own or "").strip())
            sp = r.get("split")             # a dict {splitDivision, splitClassName, splitCircle}
            division = ((sp.get("splitClassName") or sp.get("splitDivision") or "")
                        if isinstance(sp, dict) else str(sp or "")).strip()
            entries.append({"boat": nm, "sail": sail, "cls": str(r.get("design") or "").strip(),
                            "owner": owner, "division": division,
                            "orc_gph": None, "rating": None, "mmsi": None, "source": "yachtscoring"})
        if not rows or (total is not None and len(entries) >= total):
            break
        page += 1
    if not entries:
        return {"ok": False, "note": "YachtScoring returned no entries — check the event id, or the "
                "entry list may not be public yet"}
    return {"ok": True, "entries": entries, "count": len(entries)}


# ---- entry list from Regatta Network ---------------------------------------------------------------
# Regatta Network is server-rendered PHP (no JS SPA), so the results page is directly parseable: each
# division is a <tbody class="results" data-fleet="<Division>"> of rows Pos|Sail|Boat|Rating|Skipper|YC|…
_RN_EVENT_RE = re.compile(r"regatta_id=(\d+)", re.I)
_RN_RESULTS = "https://www.regattanetwork.com/clubmgmt/applet_regatta_results.php?regatta_id={rid}"
_RN_TBODY = re.compile(r'<tbody[^>]*class="results"[^>]*data-fleet="([^"]*)"[^>]*>(.*?)</tbody>', re.S | re.I)
_RN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_RN_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def _rn_text(cell):
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", " ", cell)).strip()


def roster_from_regatta_network(regatta_id):
    """A Regatta Network event's entry list from its public results page — a deterministic parse of the
    per-division `<tbody data-fleet=…>` tables (columns Pos | Sail | Boat | Rating | Skipper | Yacht Club).
    No Opus. Falls back to the generic Opus text extractor if the results table isn't present (e.g. an
    event not yet scored, or a different layout)."""
    url = _RN_RESULTS.format(rid=regatta_id)
    try:
        doc = _get(url).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "note": f"Regatta Network fetch failed: {type(e).__name__}"}
    entries = []
    for division, body in _RN_TBODY.findall(doc):
        division = _rn_text(division)
        for row in _RN_ROW.findall(body):
            cells = [_rn_text(c) for c in _RN_CELL.findall(row)]
            if len(cells) < 3:
                continue
            sail, boat = cells[1], cells[2]
            owner = re.split(r"\s{2,}", cells[4])[0].strip() if len(cells) > 4 else ""   # drop crew, keep skipper
            if not boat or boat.lower() in ("boat", "yacht"):        # skip a header row if it's in the tbody
                continue
            entries.append({"boat": boat, "sail": sail, "cls": division, "owner": owner,
                            "division": division, "orc_gph": None, "rating": None,
                            "mmsi": None, "source": "regatta_network"})
    if entries:
        return {"ok": True, "entries": entries, "count": len(entries)}
    # no structured results table (pre-race / different layout) → the generic Opus text path on the page
    try:
        _label, text = extract.text_from_url(url)
    except Exception as e:
        return {"ok": False, "note": f"Regatta Network fetch failed: {type(e).__name__}"}
    r = roster_from_text(text)
    if r.get("ok"):
        for e in r["entries"]:
            e["source"] = "regatta_network"
        return r
    return {"ok": False, "note": "Regatta Network: no entries on the results page — the event may not be "
            "published/scored yet; paste the entry list or upload the PDF instead"}


def roster_from_url(url):
    """Fetch a regatta entry-list URL → roster. Known platforms (YachtScoring, Regatta Network) are pulled
    from their public data directly; otherwise a plain HTML/PDF fetch + Opus (works for static pages; a
    JS-rendered hub returns nothing → the note tells the user to paste the text or upload the PDF)."""
    if not (url or "").strip():
        return {"ok": False, "note": "no URL given"}
    if "yachtscoring.com" in url.lower():
        m = _YS_EVENT_RE.search(url)
        if not m:
            return {"ok": False, "note": "couldn't find the YachtScoring event id in that URL "
                    "(expected .../emenu/<id> or ?eID=<id>)"}
        return roster_from_yachtscoring(m.group(1))
    if "regattanetwork.com" in url.lower():
        m = _RN_EVENT_RE.search(url)
        if not m:
            return {"ok": False, "note": "couldn't find the Regatta Network regatta_id in that URL "
                    "(expected .../applet_...php?regatta_id=<id>)"}
        return roster_from_regatta_network(m.group(1))
    try:
        _label, text = extract.text_from_url(url)
    except Exception as e:
        return {"ok": False, "note": f"entry-list fetch failed: {type(e).__name__} — if the page needs "
                "a browser to load, paste the entry-list text or upload the PDF instead"}
    return roster_from_text(text)


def roster_from_pdf(raw):
    """Extract the roster from an uploaded entry-list PDF."""
    try:
        text = extract.pdf_text(raw)
    except Exception as e:
        return {"ok": False, "note": f"PDF read failed: {type(e).__name__}"}
    return roster_from_text(text)


# ---- orchestration -------------------------------------------------------------------------------
def _preserve_seeded_mmsi(entries, definition):
    """MMSI seeding: an entry list (YB/website/ORC) NEVER carries an MMSI, so the AIS↔roster match starts
    on fuzzy name-matching. Once the crew SEEDS an MMSI on the saved roster (by hand, from a prior AIS
    fix), a re-import must NOT wipe it — carry seeded MMSIs over from the saved definition, matched by
    sail# then boat name. Returns the count carried over."""
    prior = (definition or {}).get("fleet") or []
    by_sail, by_name = {}, {}
    for e in prior:
        mm = str(e.get("mmsi") or "").strip()
        if not mm:
            continue
        s, n = _norm_sail(e.get("sail")), _norm(e.get("boat"))
        if s:
            by_sail.setdefault(s, mm)
        if n:
            by_name.setdefault(n, mm)
    if not (by_sail or by_name):
        return 0
    carried = 0
    for e in entries:
        if str(e.get("mmsi") or "").strip():
            continue
        mm = by_sail.get(_norm_sail(e.get("sail"))) or by_name.get(_norm(e.get("boat")))
        if mm:
            e["mmsi"] = mm
            carried += 1
    return carried


def pack_with_orc(entries, src, country, definition, course_id):
    """Wrap an entry list with ORC enrichment + match stats — the shared tail for every entry source."""
    seeded = _preserve_seeded_mmsi(entries, definition)   # keep hand-seeded MMSIs across a re-import
    out = {"ok": True, "source": src, "entries": entries, "count": len(entries)}
    if seeded:
        out["seeded_mmsi"] = seeded
    en = enrich_from_orc(entries, country=country, definition=definition, course_id=course_id)
    if not en.get("ok"):
        out["orc_error"] = en.get("note")                 # entry list still returned; handicap failed
    else:
        out.update(matched=en["matched"], total=en["total"], orc_certs=en["orc_certs"],
                   tot_col=en.get("tot_col"),
                   unmatched=[e["boat"] for e in entries if not e.get("orc_match")])
    return out


def import_fleet(definition, source="both", country="USA", course_id=None, url=None, text=None):
    """Orchestrate a DRAFT roster from public data — NOT saved. `source`:
    'yb' (YB entry list only) · 'both' (YB entry list → ORC) · 'website' (regatta-site URL/text → ORC) ·
    'orc' (enrich the existing roster). The entry list comes from YB OR the regatta website; ORC fills
    the handicaps for both."""
    src = (source or "both").lower()
    if src in ("yb", "both"):
        r = roster_from_yb(definition)
        if not r.get("ok"):
            return r
        entries = r["entries"]
        if src == "yb":                                   # entry list only (no ORC)
            return {"ok": True, "source": src, "entries": entries, "count": len(entries)}
    elif src in ("website", "web"):
        r = roster_from_url(url) if (url or "").strip() else roster_from_text(text)
        if not r.get("ok"):
            return r
        entries = r["entries"]
    elif src == "orc":
        entries = [dict(e) for e in (definition.get("fleet") or [])]
        if not entries:
            return {"ok": False, "note": "no existing roster to enrich — import the entry list first"}
    else:
        return {"ok": False, "note": f"unknown import source: {source}"}
    return pack_with_orc(entries, src, country, definition, course_id)
