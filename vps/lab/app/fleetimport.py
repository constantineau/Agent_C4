"""Automated fleet-roster ingestion from PUBLIC data — the entry list + ORC handicaps.

The fleet roster (perflab item-6 / handicap-aware fleet tactics) used to be hand-entered. Both halves
are public, so we automate them:

  - **Entry list (who's racing) — the YB tracker `RaceSetup`.** The same feed we decode for the boat
    track also carries the full entry list: per-team name, sail number, owner, skipper, model. One
    fetch (reusing the race's `tracker` block) → the roster's identities. (Falls back to the pasted /
    Opus-extracted entry list when a race has no YB tracker.)
  - **Handicaps (how they're rated) — the ORC public certificate database.** `data.orc.org` publishes
    every active cert per national authority as JSON (GPH + ToT/ToD coefficients, incl. race-specific
    columns). Matched to the entry list by sail number → yacht name → the roster's `orc_gph` /
    `rating`. WHICH column is the race's number comes from the race definition
    (`rules_profile.scoring`, see shared/race_def.rating_spec); with none, `rating` stays empty and
    the roster says "Rating column unknown: pick one" — never a guessed offshore number (#311).
    ORC is one feed behind `ratings` (the rating-feed registry); other systems plug in there.

Everything is a DRAFT: `import_fleet` returns a proposed roster (with per-boat match confidence + an
unmatched list); the human reviews/edits it in the Lab Fleet tab and Saves — nothing is auto-committed,
same as the rest of the Lab's ingestion. Pure-stdlib (urllib/json), cached so re-imports are cheap.
"""
import json
import os
import re
import time
import urllib.request

from shared import race_def

from . import extract, ratings

_TIMEOUT = float(os.environ.get("FLEET_IMPORT_TIMEOUT_S", "50"))
_ORC_TTL = float(os.environ.get("ORC_CACHE_TTL_S", "86400"))   # the country dump changes slowly
_YB_HOSTS = {"yb", "bycmack", "ybtracking", "yellowbrick"}
_ORC_URL = "https://data.orc.org/public/WPub.dll?action=DownRMS&CountryId={cc}&ext=json"

COLUMN_UNKNOWN = "Rating column unknown: pick one"
COUNTRY_UNKNOWN = "No ORC country known. Enter one."
# cert keys that are identity or bookkeeping, not a rating a race can score on (the column picker)
_NOT_RATINGS = {"GPH", "CDL", "LOA", "IMSL", "MB", "Dspl", "DSPL", "Age", "Year", "SailNo",
                "YachtName", "Class", "Builder", "Designer", "CertNo", "RefNo", "VPPYear"}


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
        return {"ok": False, "note": f"YB entry list not available (HTTP {e.code}). The race may "
                "not be published yet. Try nearer race time, or paste the entry list below."}
    except Exception as e:
        return {"ok": False, "note": f"YB RaceSetup fetch failed: {type(e).__name__}"}
    teams = setup.get("teams") or []
    if not teams:
        return {"ok": False, "note": "YB race not published yet. Try nearer race time."}
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
    cc = str(country or "").strip().upper()
    if not cc:                         # no country default (#310): the caller names one
        raise ValueError(COUNTRY_UNKNOWN)
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


def _tot_col(definition, course_id=None):
    """The ORC certificate column this race scores on, from its definition, or None (unknown)."""
    return race_def.rating_spec(definition or {}, course_id)["column"]


def _tod_col(definition, course_id=None):
    """The ORC time-on-distance allowance column (s/nm) the race names, or None (then GPH)."""
    return race_def.rating_spec(definition or {}, course_id)["column_tod"]


def _cols(definition, entry, course_id=None):
    """(ToT column, ToD column) for one boat: the course asked, else its division's course, else
    the race default. Same rule as the save check (race_def.rating_spec_for)."""
    spec = race_def.rating_spec_for(definition or {}, entry or {}, course_id)
    return spec["column"], spec["column_tod"]


def drop_stale(e, tot_col, tod_col):
    """Clear numbers imported off a different column than the one asked for now (#311). The tag
    rules (imported / typed / legacy) live in race_def so the import and every save agree."""
    return race_def.stale_numbers(e, tot_col, tod_col)


def _set_numbers(e, rec, tot_col, tod_col):
    """Put a cert's numbers on an entry, each tagged with the column it came from. Returns the
    rating (None when the race names no column or the cert lacks it)."""
    gph, rating = _orc_handicap(rec, tot_col)
    if gph is not None:
        e["orc_gph"] = gph
    # the race's column only: a rating read off another column (an earlier pick, a hand entry
    # before the match) must not survive under this one
    e["rating"] = rating
    if rating is not None:
        e["rating_col"] = tot_col
    else:
        e.pop("rating_col", None)
    alw = _as_num(rec.get(tod_col)) if tod_col else None
    e["tod_allowance"] = alw
    if alw is not None:
        e["tod_col"] = tod_col
    else:
        e.pop("tod_col", None)
    return rating


def _as_num(v):
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _orc_handicap(rec, tot_col):
    """(GPH, rating) off one cert. `rating` is the race's column only: None when the race names no
    column or this cert lacks it. No fallback to another number."""
    gph = rec.get("GPH")
    rating = rec.get(tot_col) if tot_col else None
    try:
        gph = float(gph) if gph is not None else None
    except (TypeError, ValueError):
        gph = None
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    return gph, rating


def _countries(country, definition=None, entries=None):
    """'USA,CAN' → ['USA', 'CAN'] (each is a separate ORC dump). Nothing typed → the race
    definition's list (its scoring block, else the venue country + entrants' sail prefixes)."""
    if isinstance(country, (list, tuple)):
        typed = [str(c).strip().upper() for c in country if str(c).strip()]
    else:
        typed = [c.strip().upper() for c in str(country or "").split(",") if c.strip()]
    return typed or race_def.rating_countries(definition or {}, entries)


def _rating_keys(rec):
    """Numeric cert columns a race could score on (offered when the race names none)."""
    out = set()
    for k, v in rec.items():
        if k in _NOT_RATINGS or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.add(k)
    return out


def enrich_from_orc(entries, country=None, definition=None, course_id=None):
    """Fill orc_gph + rating (and tidy class) on each entry by matching to the ORC cert DB. Match by
    sail number across EACH listed country's dump (in order), then yacht name the same way.
    `rating` is the race definition's column only (see `_tot_col`); with none named, ratings stay empty and the result
    carries `note` = COLUMN_UNKNOWN plus `orc_columns` to pick from. Returns the enriched entries +
    match stats; unmatched keep their identity (the human can fill the handicap by hand)."""
    tot_col = _tot_col(definition, course_id)          # the race default, for the summary
    cols = [_cols(definition, e, course_id) for e in entries]   # each boat's own course
    # first, whatever the lookup brings: numbers imported off another column go, matched or not
    dropped = [e for e, (tc, dc) in zip(entries, cols) if drop_stale(e, tc, dc)]
    stale = len(dropped)
    countries = _countries(country, definition, entries)
    if not countries:
        return {"ok": False, "note": COUNTRY_UNKNOWN, "entries": entries, "stale_cleared": stale}
    idxs, certs, fetched = [], 0, []
    for cc in countries:
        try:
            idx = _orc_dump(cc)
            idxs.append((cc, idx))
            certs += idx["n"]
            fetched.append(cc)
        except Exception as e:
            print(f"[fleet] ORC dump {cc} failed ({type(e).__name__}) — skipping", flush=True)
    if not idxs:
        return {"ok": False, "note": f"ORC database fetch failed ({','.join(countries)})",
                "entries": entries, "stale_cleared": stale}
    matched, lacking, seen_cols, lacking_cols = 0, 0, set(), set()
    for e, (e_tot, e_tod) in zip(entries, cols):
        # a sail number in ANY listed country beats a name match in an earlier one (#341)
        rec, by, cc_hit = None, None, None
        for kind, table, key in (("sail", "by_sail", _norm_sail(e.get("sail"))),
                                 ("name", "by_name", _norm(e.get("boat")))):
            rec, cc_hit = next(((idx[table][key], cc) for cc, idx in idxs
                                if idx[table].get(key)), (None, None))
            if rec:
                by = kind
                break
        if not rec:
            e["orc_match"] = None
            continue
        rating = _set_numbers(e, rec, e_tot, e_tod)
        seen_cols |= _rating_keys(rec)
        if rating is None and e_tot:
            lacking += 1
            lacking_cols.add(e_tot)
        if not e.get("cls") and rec.get("Class"):
            e["cls"] = rec.get("Class")
        e["source"] = (e.get("source") or "") + "+orc" if e.get("source") else "orc"
        e["orc_match"] = {"by": by, "country": cc_hit,
                          "yacht": rec.get("YachtName"), "sail": rec.get("SailNo"),
                          "class": rec.get("Class"), "col": e_tot}
        matched += 1
    out = {"ok": True, "entries": entries, "matched": matched, "total": len(entries),
           "orc_country": ",".join(fetched), "orc_certs": certs, "tot_col": tot_col,
           "tod_col": _tod_col(definition, course_id),
           # cleared and not read again off the new column (a re-read one isn't a loss)
           "stale_cleared": sum(1 for e in dropped if e.get("rating") is None)}
    if not any(tc for tc, _dc in cols) and not (tot_col and not entries):
        out["note"] = COLUMN_UNKNOWN
        out["orc_columns"] = sorted(seen_cols)
    elif lacking:
        out["note"] = (f"{lacking} boat{'s lack' if lacking != 1 else ' lacks'} "
                       f"{', '.join(sorted(lacking_cols))} on the cert.")
    return out


def orc_candidates(boat, sail=None, country=None, definition=None, course_id=None, limit=6):
    """Fuzzy ORC-cert candidates for ONE unrated boat — the human picks; nothing auto-applies.
    Scores name similarity (difflib) + an exact-sail boost across every listed country's dump."""
    import difflib
    tot_col, tod_col = _cols(definition, {}, course_id)
    target = _norm(boat or "")
    target_sail = _norm_sail(sail)
    out = []
    countries = _countries(country, definition, [{"sail": sail}] if sail else None)
    for cc in countries:
        try:
            idx = _orc_dump(cc)
        except Exception:
            continue
        for key, rec in idx["by_name"].items():
            score = difflib.SequenceMatcher(None, target, key).ratio() if target else 0.0
            if target_sail and _norm_sail(rec.get("SailNo")) == target_sail:
                score = max(score, 0.99)
            if score < 0.55:
                continue
            gph, rating = _orc_handicap(rec, tot_col)
            out.append({"score": round(score, 2), "country": cc,
                        "yacht": rec.get("YachtName"), "sail": rec.get("SailNo"),
                        "cls": rec.get("Class"), "orc_gph": gph, "rating": rating,
                        "tod_allowance": _as_num(rec.get(tod_col)) if tod_col else None})
    out.sort(key=lambda c: -c["score"])
    res = {"boat": boat, "candidates": out[:limit], "tot_col": tot_col, "tod_col": tod_col,
           "orc_country": ",".join(countries)}
    if not countries:
        res["note"] = COUNTRY_UNKNOWN
    elif not tot_col:
        res["note"] = COLUMN_UNKNOWN
    return res


class OrcFeed(ratings.RatingFeed):
    """ORC public certificates as a rating feed (the registry's only one today)."""
    system = "ORC"

    def enrich(self, entries, definition, course_id, countries):
        return enrich_from_orc(entries, country=countries, definition=definition,
                               course_id=course_id)

    def candidates(self, boat, sail, definition, course_id, countries, limit=6):
        return orc_candidates(boat, sail=sail, country=countries, definition=definition,
                              course_id=course_id, limit=limit)


ratings.register(OrcFeed())


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
        return {"ok": False, "note": "No boats found there. Copy the entry list from the page "
                "and paste it, or upload the PDF."}
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
        return {"ok": False, "note": "YachtScoring has no entries. Check the link, or the list "
                "may not be public yet."}
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
    return {"ok": False, "note": "Regatta Network has no entries yet. Paste the entry list or "
            "upload the PDF instead."}


def roster_from_url(url):
    """Fetch a regatta entry-list URL → roster. Known platforms (YachtScoring, Regatta Network) are pulled
    from their public data directly; otherwise a plain HTML/PDF fetch + Opus (works for static pages; a
    JS-rendered hub returns nothing → the note tells the user to paste the text or upload the PDF)."""
    if not (url or "").strip():
        return {"ok": False, "note": "no URL given"}
    if "yachtscoring.com" in url.lower():
        m = _YS_EVENT_RE.search(url)
        if not m:
            return {"ok": False, "note": "That YachtScoring link has no event number "
                    "(it should look like .../emenu/<id>)."}
        return roster_from_yachtscoring(m.group(1))
    if "regattanetwork.com" in url.lower():
        m = _RN_EVENT_RE.search(url)
        if not m:
            return {"ok": False, "note": "That Regatta Network link has no regatta_id "
                    "(it should end in ?regatta_id=<id>)."}
        return roster_from_regatta_network(m.group(1))
    try:
        _label, text = extract.text_from_url(url)
    except Exception as e:
        return {"ok": False, "note": f"Couldn't read that page ({type(e).__name__}). Paste the "
                "entry list or upload the PDF instead."}
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


def _feed(definition):
    """(system, feed) for the race. An unstated system is tried as ORC (the column check still
    applies); a stated system with no feed returns feed None."""
    system = race_def.rating_system(((definition or {}).get("rules_profile") or {})
                                    .get("scoring") or {}) or "ORC"
    return system, ratings.feed_for(system)


def candidates(boat, sail=None, country=None, definition=None, course_id=None, limit=6,
               division=None):
    """Rating candidates for one boat from the race's feed (the Fleet tab's 'find' button). The
    boat's division picks its course's column, as in the import and the save check."""
    if not course_id and division:
        course_id = race_def.course_of_division(definition or {}, division)
    system, feed = _feed(definition)
    if feed is None:
        return {"boat": boat, "candidates": [], "note": ratings.no_feed_note(system)}
    return feed.candidates(boat, sail, definition, course_id, country, limit)


def pack_with_orc(entries, src, country, definition, course_id):
    """Wrap an entry list with rating enrichment + match stats — the shared tail for every entry
    source. The race's rating system picks the feed (ORC today)."""
    seeded = _preserve_seeded_mmsi(entries, definition)   # keep hand-seeded MMSIs across a re-import
    out = {"ok": True, "source": src, "entries": entries, "count": len(entries)}
    if seeded:
        out["seeded_mmsi"] = seeded
    system, feed = _feed(definition)
    if feed is None:
        out["rating_note"] = ratings.no_feed_note(system)
        return out
    # one country list for the fleet and our boat: typed, else the race's (venue + sail prefixes)
    country = _countries(country, definition, entries) or None
    en = feed.enrich(entries, definition, course_id, country)
    if not en.get("ok"):
        out["orc_error"] = en.get("note")                 # entry list still returned; handicap failed
    else:
        out.update(matched=en["matched"], total=en["total"], orc_certs=en["orc_certs"],
                   tot_col=en.get("tot_col"), orc_country=en.get("orc_country"),
                   unmatched=[e["boat"] for e in entries if not e.get("orc_match")])
        if en.get("note"):
            out["rating_note"] = en["note"]
        if en.get("orc_columns") is not None:
            out["orc_columns"] = en["orc_columns"]
    if en.get("stale_cleared"):
        out["stale_cleared"] = en["stale_cleared"]
    own = _refresh_own(feed, definition, course_id, country)
    if own is not None:
        out["own"], note = own
        if note:
            out["own_note"] = note
    return out


OWN_REENTER = "Re-enter our rating for this column"


def _refresh_own(feed, definition, course_id, country):
    """Our boat's numbers for the column asked now (#311): read off its certificate when the feed
    finds it (by sail, then name); else anything imported or typed for another column is cleared
    and the note asks for it again. Returns (own, note) or None when no own boat is set."""
    own = dict((definition or {}).get("own") or {})
    if not (own.get("boat") or own.get("sail")):
        return None
    tot_col, tod_col = _cols(definition, own, course_id)
    probe = {"boat": own.get("boat") or "", "sail": own.get("sail") or "",
             "division": own.get("division") or ""}
    try:
        en = feed.enrich([probe], definition, course_id, country)
    except Exception:
        en = {"ok": False}
    if en.get("ok") and probe.get("orc_match"):
        for k in ("orc_gph", "rating", "rating_col", "tod_allowance", "tod_col"):
            if k in probe:
                own[k] = probe[k]
            else:
                own.pop(k, None)
        missing = tot_col and own.get("rating") is None
        return own, (OWN_REENTER if missing else None)
    cleared = drop_stale(own, tot_col, tod_col)
    return own, (OWN_REENTER if cleared else None)


def import_fleet(definition, source="both", country=None, course_id=None, url=None, text=None):
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
            out = {"ok": True, "source": src, "entries": entries, "count": len(entries)}
            seeded = _preserve_seeded_mmsi(entries, definition)   # same as pack_with_orc
            if seeded:
                out["seeded_mmsi"] = seeded
            return out
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
