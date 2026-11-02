"""Race-document ingestion → RaceDefinition (Lab-0 slice 2).

Dual input: a race URL (discover candidate PDFs), a pasted direct PDF link, or an uploaded PDF.
A frontier model reads the documents and returns a RaceDefinition matching `shared/race_def.py`.
The result is always a DRAFT — it goes to the human-review step in the Lab before it's
saved/activated (a wrong waypoint is dangerous).

Robustness (2026-11-02, after SORC 2027 failed on one JSON slip):
- PDFs go to the model AS PDFs (it sees tables and scanned pages), within a page/byte budget;
  over budget, or for HTML pages, it gets the extracted text instead.
- The reply is constrained to RACE_SCHEMA (structured outputs), so it is valid JSON by
  construction. A JSON check + one retry stays as a backstop.
- Nothing is dropped silently: every trimmed, empty, oversized or unreadable document becomes a
  plain note the review step shows (`notes`), or a plain error naming the document.
"""
import base64
import io
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

import pypdf

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MAX_DOC_CHARS = int(os.environ.get("LAB_MAX_DOC_CHARS", "300000"))
# PDFs sent as PDFs: each page costs ~1.5-3k text tokens + an image. 100 pages ≈ $1-3 a run.
# Docs past the budget go as extracted text (and the review step says so).
PDF_MAX_PAGES = int(os.environ.get("LAB_PDF_MAX_PAGES", "100"))
PDF_MAX_BYTES = int(os.environ.get("LAB_PDF_MAX_BYTES", str(32 * 1024 * 1024)))
MIN_TEXT_CHARS = 200          # chars/page below which a PDF read as text is probably a scan
EXTRACT_MAX_TOKENS = int(os.environ.get("LAB_EXTRACT_MAX_TOKENS", "64000"))
IFRAME_TIMEOUT = int(os.environ.get("LAB_IFRAME_TIMEOUT", "60"))   # embedded entry apps can be slow
FETCH_TIMEOUT = int(os.environ.get("LAB_FETCH_TIMEOUT", "30"))
_UA = {"User-Agent": "Mozilla/5.0 (C4 Performance Lab race-doc ingest)"}


class IngestError(RuntimeError):
    """A failure with a short, plain message meant for the person at the Lab (what went wrong and
    what to do). main.py shows it as-is."""


def fetch(url: str, timeout: int = 30):
    """GET a URL → (content_type, raw_bytes). HTTP errors raise."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (r.headers.get("Content-Type", "") or "").lower(), r.read()


def _transient(exc) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError))


def _why(exc, timeout) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return {404: "the link is broken (404 not found)",
                403: "the site refused access (403)",
                401: "the site wants a login (401)"}.get(
            exc.code, f"the site answered with an error ({exc.code})")
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return f"the site didn't answer within {timeout} s"
    if isinstance(exc, urllib.error.URLError):
        return "the site couldn't be reached"
    if isinstance(exc, ValueError):
        return "that isn't a web link"
    return type(exc).__name__


def doc_label(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] or url


def fetch_doc(url: str, timeout: int = FETCH_TIMEOUT):
    """fetch() with one retry on a transient failure (timeout, dropped connection, 5xx/429). A
    failure raises IngestError naming the document in plain words."""
    label = doc_label(url)
    for attempt in range(2):
        try:
            return fetch(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — every failure becomes a plain message
            if attempt == 0 and _transient(e):
                time.sleep(2)
                continue
            raise IngestError(f"Couldn't download {label}: {_why(e, timeout)}. Check the link "
                              f"opens in a browser, or download the PDF and upload it.") from e


def _pdf_reader(raw: bytes):
    return pypdf.PdfReader(io.BytesIO(raw))


def pdf_text(raw: bytes) -> str:
    reader = _pdf_reader(raw)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def load_pdf(label: str, raw: bytes, url: str = "") -> dict:
    """An uploaded or downloaded PDF → a doc: {label, url, text, pdf, pages}. pypdf trouble
    (odd or scanned files) isn't fatal: the model can still read the PDF itself."""
    if b"%PDF" not in raw[:1024]:
        raise IngestError(f"{label} isn't a PDF file. Upload the PDF, or paste its link.")
    try:
        reader = _pdf_reader(raw)
        note = ""
        if reader.is_encrypted:
            # Many race PDFs are "encrypted" with an EMPTY open password (print/copy limits
            # only): those open fine. Only a real open password is a dead end.
            if not reader.decrypt(""):
                raise IngestError(f"{label} is password-protected. Upload an unlocked copy.")
            raw, note = _unlocked_copy(reader, label)
        pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except IngestError:
        raise
    except Exception:  # noqa: BLE001 — pypdf can't parse it; count pages the crude way
        pages = max(1, len(re.findall(rb"/Type\s*/Page(?!s)", raw)))
        text, note = "", ""
    return {"label": label, "url": url, "text": text, "pdf": raw, "pages": pages, "note": note}


def _unlocked_copy(reader, label) -> tuple:
    """An empty-password PDF → (an unencrypted copy to send as a PDF, ""), or (None, note) when it
    can't be rewritten: then only its text goes to the model, and the review says so."""
    try:
        out = io.BytesIO()
        pypdf.PdfWriter(clone_from=reader).write(out)
        return out.getvalue(), ""
    except Exception:  # noqa: BLE001
        return None, (f"{label} has copy restrictions, so only its plain text was read. Tables "
                      f"and scanned pages may be missing. Check it by hand.")


def load_url(url: str) -> dict:
    """A document URL → a doc ({label, url, text, pdf, pages}); PDFs keep their bytes."""
    ctype, raw = fetch_doc(url)
    label = doc_label(url)
    if "pdf" in ctype or url.lower().endswith(".pdf") or raw[:5] == b"%PDF-":
        return load_pdf(label, raw, url)
    return {"label": label, "url": url, "text": _page_text(url, raw), "pdf": None, "pages": 0}


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


_IFRAME_SRC = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
# non-content iframes to skip (ads/analytics/social) so we don't waste fetches or add noise
_IFRAME_SKIP = ("google.com", "googletagmanager", "doubleclick", "facebook.", "youtube.",
                "gstatic", "recaptcha", "/gtag", "analytics", "hotjar", "twitter.")


def _page_text(url: str, raw: bytes, follow_iframes: bool = True) -> str:
    html = raw.decode("utf-8", "ignore")
    text = _html_to_text(html)
    if follow_iframes:
        from urllib.parse import urljoin
        seen = set()
        for src in _IFRAME_SRC.findall(html):
            full = urljoin(url, src)
            low = full.lower()
            if (not low.startswith(("http://", "https://")) or full in seen
                    or any(s in low for s in _IFRAME_SKIP)):
                continue
            seen.add(full)
            if len(seen) > 3:                     # bound the fan-out; one level deep, no recursion
                break
            # embedded entry-list apps (ColdFusion/etc.) can be slow + flaky (bycmack's entries.cfm
            # is a ~500 KB dynamic page that takes 5–20 s and sometimes resets) → longer timeout + 1 retry
            for attempt in range(2):
                try:
                    ct, rb = fetch(full, timeout=IFRAME_TIMEOUT)
                    text += "\n" + (pdf_text(rb) if ("pdf" in ct or low.endswith(".pdf"))
                                    else _html_to_text(rb.decode("utf-8", "ignore")))
                    break
                except Exception:
                    if attempt:                   # a dead/blocked iframe never breaks the outer fetch
                        break
                    time.sleep(1.5)
    return text


def text_from_url(url: str, follow_iframes: bool = True) -> tuple[str, str]:
    """(label, extracted_text) for a URL — PDF or HTML. HTML content embedded one level deep in a
    content `<iframe>` is followed and appended (bounded): regatta sites commonly serve the entry
    list / results from a separate app inside an iframe (e.g. bycmack's `current-entries` embeds
    `cf.bycmack.com/entries.cfm`), so the visible list is absent from the outer page's own HTML."""
    ctype, raw = fetch(url)
    label = doc_label(url)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return label, pdf_text(raw)
    return label, _page_text(url, raw, follow_iframes)


_PDF_LINK = re.compile(r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>',
                       re.I | re.S)


def discover_pdfs(url: str) -> list[dict]:
    """Scrape a race page for candidate document PDF links (auto-find)."""
    ctype, raw = fetch_doc(url)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return [{"label": doc_label(url), "url": url}]
    html = raw.decode("utf-8", "ignore")
    from urllib.parse import urljoin
    out, seen = [], set()
    for href, label in _PDF_LINK.findall(html):
        full = urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        label = re.sub(r"<[^>]+>", "", label)
        label = re.sub(r"\s+", " ", label).strip() or full.rsplit("/", 1)[-1]
        kw = (label + " " + full).lower()
        # rank docs we care about first
        rank = 0 if any(k in kw for k in ("nor", "notice", "sailing instruction", " si ",
                                          "ser", "safety", "course", "cruising")) else 1
        out.append({"label": label, "url": full, "rank": rank})
    out.sort(key=lambda d: d["rank"])
    return [{"label": d["label"], "url": d["url"]} for d in out]


SCHEMA_BRIEF = """Produce ONE RaceDefinition JSON object. Field meanings:
- race_id (kebab-case from name+year), name, year, organizing_authority, start_date (ISO date),
  start_area, region, timezone (the IANA tz name of the start venue, e.g. "America/Detroit" — INFER
  it from the venue / start area location; this is the one field you may derive from geography rather
  than the documents. Use "" only if the venue location is genuinely unknown).
- divisions: [{id, name, course_ref, boat_type}]; course_ref is the id of the course it sails.
- courses: [{id, name, applies_to_divisions:[div ids], distance_nm (or null), note,
  start:{type, ref, lat, lon, coords_source},
  marks:[{seq (sailing order 1..n), name, type in [start,waypoint,gate,island,buoy,finish], rounding
    in [port,starboard,gate,none], lat, lon, lat2, lon2 (gates only, else null), coords_source in
    [nor,si,chart,orc,approx,needs_review,si_pending], note}],
  finish:{type, points:[{name,lat,lon}] (1 mark or the 2 ends of a line), crossing, coords_source,
    note}}].
- zones: [{name, type in [exclusion,tss,hazard], geometry, note}]; geometry = {coordinates:
  [[[lon,lat], ...]]} (a polygon, lon first) ONLY when the documents give the corner coordinates;
  else {coordinates: []}.
- requirements: COMPREHENSIVE checklist [{id (kebab), category in [safety,structural,crew_safety,
  navigation,communications,registration,procedure,reporting,environmental,rules], phase in
  [pre_entry,pre_start,start,in_race,at_gate,at_finish,post_race], text, trigger_type in
  [none,time,event,location], trigger_detail, deliver_to_ipad (bool), critical (bool), source}].
  Capture safety/SER equipment, registration, navigation, and procedural items. Set
  deliver_to_ipad=true for race-time ACTION items the crew must do at a moment (e.g. nav lights at
  sunset, gate GPS photo, finish procedure / displaying numbers) and give them a trigger.
- division_starts: [{division, gun}] — the division's WARNING/START gun as an ISO local datetime
  "YYYY-MM-DDTHH:MM" (the race venue's local time) — ONLY when the SIs/NOR state the per-division
  start times; else an empty list. These drive corrected-time standings.
- rules_profile: {rrs_edition, info_available_to_all_permitted (bool),
  customized_advice_while_underway_prohibited (bool), appendix_wp (bool), tracker_permitted
  (true/false only when the documents say; else null), modifications:[{ref,rule,summary}],
  scoring:{system,method,options:[],decided,ref}}.
- fleet: [{boat, division, cls, owner, sail, orc_gph, rating, source}] — leave empty unless an
  entry list is present (orc_gph / rating null when not given).
- provenance: {sources: [] (leave empty), si_status (are the SIs out yet?), review_status,
  extracted_by}.

CONVENTIONS: coordinates are DECIMAL DEGREES, WGS84 (N/E positive; convert degrees-decimal-minutes,
W/S negative). If a mark's coordinates are NOT explicitly stated in the documents, set lat/lon to
null and coords_source to "needs_review" — never guess coordinates. Cite the source section in each
requirement/modification 'source'/'ref'. Set provenance.review_status to note it is
machine-extracted and needs human review (list what's uncertain). Set extracted_by to
"Lab ingestion (Opus)". If something is unknown, use null (or "") and say so in a note — do not
invent. Output only the JSON object, with exactly the fields asked for."""


def _obj(props: dict) -> dict:
    """A strict object: every property required, nothing extra (structured-outputs rules)."""
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


_S = {"type": "string"}
_B = {"type": "boolean"}
_NUM_OR_NULL = {"type": ["number", "null"]}
_COORDS_SOURCE = {"type": "string", "enum": ["nor", "si", "chart", "orc", "approx", "needs_review",
                                             "si_pending"]}
# The RaceDefinition the Lab reads (shared/race_def.py + what main/obstacles/app.js use), as a JSON
# Schema the API enforces. Inside the documented structured-outputs limits (every property
# required: 0 optional of 24; 12 nullable fields of 16 unions; no free-form maps — division_starts
# is a list here and becomes the Lab's {division: gun} map after parsing). The WHOLE schema still
# compiles too large ("The compiled grammar is too large", measured 2026-11-02 on both models), so
# extraction asks for it in two PARTS, each of which compiles.
RACE_SCHEMA = _obj({
    "race_id": _S, "name": _S, "year": {"type": "integer"}, "organizing_authority": _S,
    "start_date": _S, "start_area": _S, "region": _S, "timezone": _S,
    "divisions": {"type": "array", "items": _obj({
        "id": _S, "name": _S, "course_ref": _S, "boat_type": _S})},
    "courses": {"type": "array", "items": _obj({
        "id": _S, "name": _S, "applies_to_divisions": {"type": "array", "items": _S},
        "distance_nm": _NUM_OR_NULL, "note": _S,
        "start": _obj({"type": _S, "ref": _S, "lat": _NUM_OR_NULL, "lon": _NUM_OR_NULL,
                       "coords_source": _COORDS_SOURCE}),
        "marks": {"type": "array", "items": _obj({
            "seq": {"type": "integer"}, "name": _S,
            "type": {"type": "string", "enum": ["start", "waypoint", "gate", "island", "buoy",
                                                "finish"]},
            "rounding": {"type": "string", "enum": ["port", "starboard", "gate", "none"]},
            "lat": _NUM_OR_NULL, "lon": _NUM_OR_NULL, "lat2": _NUM_OR_NULL, "lon2": _NUM_OR_NULL,
            "coords_source": _COORDS_SOURCE, "note": _S})},
        "finish": _obj({"type": _S, "points": {"type": "array", "items": _obj({
            "name": _S, "lat": _NUM_OR_NULL, "lon": _NUM_OR_NULL})},
            "crossing": _S, "coords_source": _COORDS_SOURCE, "note": _S}),
    })},
    "zones": {"type": "array", "items": _obj({
        "name": _S, "type": {"type": "string", "enum": ["exclusion", "tss", "hazard"]},
        "geometry": _obj({"coordinates": {      # [] = not given (→ null after parsing)
            "type": "array", "items": {"type": "array", "items": {
                "type": "array", "items": {"type": "number"}}}}}),
        "note": _S})},
    "requirements": {"type": "array", "items": _obj({
        "id": _S,
        "category": {"type": "string", "enum": ["safety", "structural", "crew_safety",
                                                "navigation", "communications", "registration",
                                                "procedure", "reporting", "environmental",
                                                "rules"]},
        "phase": {"type": "string", "enum": ["pre_entry", "pre_start", "start", "in_race",
                                             "at_gate", "at_finish", "post_race"]},
        "text": _S,
        "trigger_type": {"type": "string", "enum": ["none", "time", "event", "location"]},
        "trigger_detail": _S, "deliver_to_ipad": _B, "critical": _B, "source": _S})},
    "division_starts": {"type": "array", "items": _obj({"division": _S, "gun": _S})},
    "rules_profile": _obj({
        "rrs_edition": _S, "info_available_to_all_permitted": _B,
        "customized_advice_while_underway_prohibited": _B, "appendix_wp": _B,
        "tracker_permitted": {"type": ["boolean", "null"]},
        "modifications": {"type": "array", "items": _obj({"ref": _S, "rule": _S, "summary": _S})},
        "scoring": _obj({"system": _S, "method": _S, "options": {"type": "array", "items": _S},
                         "decided": _S, "ref": _S})}),
    "fleet": {"type": "array", "items": _obj({
        "boat": _S, "division": _S, "cls": _S, "owner": _S, "sail": _S,
        "orc_gph": _NUM_OR_NULL, "rating": _NUM_OR_NULL, "source": _S})},
    "provenance": _obj({
        "sources": {"type": "array", "items": _obj({"label": _S, "url": _S, "retrieved": _S})},
        "si_status": _S, "review_status": _S, "extracted_by": _S}),
})

# Two requests over the same documents, run in parallel; each part's schema compiles on its own
# (course+header: 9 nullable fields; checklist+rules+zones+fleet+provenance: 3).
PARTS = {
    "course": ["race_id", "name", "year", "organizing_authority", "start_date", "start_area",
               "region", "timezone", "divisions", "courses", "division_starts"],
    "rules": ["requirements", "rules_profile", "zones", "fleet", "provenance"],
}


def part_schema(keys: list) -> dict:
    return _obj({k: RACE_SCHEMA["properties"][k] for k in keys})


_SYSTEM = ("You convert sailing race documents (NOR / SI / SER / amendments / entry lists) into a "
           "single structured RaceDefinition JSON object for a race-strategy tool. Be precise and "
           "comprehensive; never invent coordinates or facts. Where an amendment changes the NOR "
           "or SI, use the amended version.")


def _as_doc(d) -> dict:
    if isinstance(d, dict):
        return {"label": d.get("label") or "document", "url": d.get("url", ""),
                "text": d.get("text") or "", "pdf": d.get("pdf"), "pages": d.get("pages") or 0,
                "note": d.get("note") or ""}
    label, text = d                       # legacy (label, text) pairs
    return {"label": label, "url": "", "text": text or "", "pdf": None, "pages": 0, "note": ""}


def _fit_text(lengths: list[int], budget: int) -> int:
    """The per-document cap that fits all text into `budget` chars, shared fairly: short docs keep
    everything, the long ones are cut to the same length."""
    if sum(lengths) <= budget:
        return max(lengths, default=0)
    lo, hi = 0, max(lengths)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(n, mid) for n in lengths) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_content(docs, notes: list) -> list[dict]:
    """The user-turn content blocks for the model: each PDF as a PDF (within the page/byte budget),
    everything else as its text, never cut without a note. Raises IngestError when nothing is
    readable."""
    docs = [_as_doc(d) for d in docs]
    notes.extend(d["note"] for d in docs if d["note"])
    pages = nbytes = 0
    as_pdf = set()
    for i, d in enumerate(docs):
        if not d["pdf"]:
            continue
        if pages + d["pages"] <= PDF_MAX_PAGES and nbytes + len(d["pdf"]) <= PDF_MAX_BYTES:
            as_pdf.add(i)
            pages += d["pages"]
            nbytes += len(d["pdf"])
        else:
            notes.append(f"{d['label']} is too big to read as a PDF ({d['pages']} pages, "
                         f"{len(d['pdf']) / 1e6:.1f} MB). Only its plain text was read, so "
                         f"tables and scanned pages may be missing. Check it by hand.")
    text_idx = [i for i in range(len(docs)) if i not in as_pdf]
    cap = _fit_text([len(docs[i]["text"].strip()) for i in text_idx], MAX_DOC_CHARS)
    blocks, readable = [], bool(as_pdf)
    for i, d in enumerate(docs):
        head = f"===== DOCUMENT: {d['label']} ====="
        if i in as_pdf:
            blocks.append({"type": "text", "text": head})
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(d["pdf"]).decode("ascii")}})
            continue
        text = d["text"].strip()
        if not text:
            notes.append(f"{d['label']} had no readable text"
                         + (" (a scanned PDF?)" if d["pdf"] else "")
                         + ". Nothing from it is in the draft. Check it by hand.")
            continue
        readable = True              # a short amendment ("start postponed to 1100") is real content
        if d["pdf"] and len(text) < MIN_TEXT_CHARS * max(1, d["pages"]):
            # a PDF going as text with next to no text layer: likely a scan (sent as a PDF when
            # it fits the budget — then this branch isn't reached)
            notes.append(f"{d['label']} has very little text (a scanned PDF?). Little from it may "
                         f"be in the draft. Check it by hand.")
        if len(text) > cap:
            pct = round(100 * cap / len(text))
            notes.append(f"{d['label']} is too long: only its first {pct}% was read. Check the "
                         f"rest by hand, or read it on its own.")
            text = text[:cap]
        blocks.append({"type": "text", "text": f"{head}\n{text}"})
    if not readable:
        raise IngestError("None of the documents had readable text. If they're scans, upload "
                          "the PDF files themselves.")
    blocks.append({"type": "text", "text": SCHEMA_BRIEF})
    return blocks


def _text_only(docs) -> list:
    """The same documents with the PDF bytes dropped (the model rejected a PDF)."""
    return [dict(_as_doc(d), pdf=None, note="") for d in docs]


def _finish(defn: dict) -> dict:
    """Schema shape → the Lab's stored shape: division_starts list → {division: gun} (omitted when
    the SIs give none, as before)."""
    ds = defn.get("division_starts")
    if isinstance(ds, list):
        m = {str(x.get("division")): x.get("gun") for x in ds
             if isinstance(x, dict) and x.get("division") and x.get("gun")}
        if m:
            defn["division_starts"] = m
        else:
            defn.pop("division_starts", None)
    elif not ds:
        defn.pop("division_starts", None)
    for z in defn.get("zones") or []:
        g = z.get("geometry") if isinstance(z, dict) else None
        if isinstance(g, dict) and not g.get("coordinates"):
            z["geometry"] = None
    return defn


def _plain_llm_error(e) -> IngestError:
    from . import llm as lab_llm
    if isinstance(e, lab_llm.Refused):
        return IngestError("The AI declined to read these documents. Try again; if it keeps "
                           "happening, read the documents one at a time.")
    if isinstance(e, lab_llm.Truncated):
        return IngestError("The documents made a reply too long to finish. Read the NOR and the "
                           "SI separately (one at a time).")
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    if "credit balance" in str(e).lower():
        return IngestError("The Lab's AI account is out of credit. Top it up (Anthropic console, "
                           "Plans & Billing), then try again.")
    if name == "AuthenticationError" or status == 401:
        return IngestError("The Lab's AI key was rejected. The API key needs fixing (server side).")
    if name in ("RateLimitError", "OverloadedError") or status in (429, 529) or (
            isinstance(status, int) and status >= 500):
        return IngestError("The AI service is busy. Try again in a few minutes.")
    if name in ("APITimeoutError", "APIConnectionError"):
        return IngestError("Lost the connection to the AI service. Try again.")
    return IngestError(f"Reading the documents failed ({name}: {str(e)[:200]}). Try again.")


def _extract_part(name: str, content: list, usage) -> tuple[dict, str]:
    """One part of the definition (see PARTS) → (dict, model). A reply that isn't valid JSON (only
    possible when the API dropped the schema) is asked for once more, saying what was wrong."""
    from . import llm as lab_llm
    keys = PARTS[name]
    ask = (f"{SCHEMA_BRIEF}\n\nThis request fills ONLY these fields: {', '.join(keys)}. "
           f"A separate request extracts the other fields from the same documents.")
    content = content + [{"type": "text", "text": ask}]
    err = None
    for _attempt in range(2):
        text, model = lab_llm.complete_json(_SYSTEM, content, part_schema(keys),
                                            max_tokens=EXTRACT_MAX_TOKENS, usage=usage)
        try:
            part = _parse_json(text)
            if not isinstance(part, dict):
                raise ValueError("not a JSON object")
            return part, model
        except ValueError as e:          # json.JSONDecodeError is a ValueError
            err = e
            content = content[:-1] + [{"type": "text", "text": (
                f"{ask}\n\nYour previous answer was not valid JSON ({e}). Return only the JSON "
                f"object, valid JSON, nothing else.")}]
    raise IngestError(f"The AI's reply wasn't valid JSON twice ({err}). Try again.")


def _extract_parts(content: list, usage) -> tuple[dict, str]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(PARTS)) as pool:
        futs = {n: pool.submit(_extract_part, n, content, usage) for n in PARTS}
        results = {n: f.result() for n, f in futs.items()}   # the first failure raises
    merged = {}
    for name in PARTS:
        part = results[name][0]
        merged.update({k: v for k, v in part.items() if k in PARTS[name]} or part)
    return merged, results["course"][1]


def extract_race_definition(docs, retrieved: str = "", notes: list | None = None,
                            usage: dict | None = None) -> dict:
    """docs = doc dicts (load_url/load_pdf) or legacy [(label, text)]; returns a draft
    RaceDefinition dict. `notes` collects plain review notes (trimmed/empty/oversized docs);
    `usage` collects model + token counts. Raises IngestError with a plain message."""
    from . import llm as lab_llm
    if not API_KEY:
        raise IngestError("The Lab has no AI key set (ANTHROPIC_API_KEY), so it can't read "
                          "documents. That needs fixing on the server.")
    notes = [] if notes is None else notes
    content = build_content(docs, notes)
    try:
        try:
            defn, model = _extract_parts(content, usage)
        except lab_llm.BadDocument:
            if not any(b.get("type") == "document" for b in content):
                raise
            notes.append("The AI couldn't open a PDF as a PDF, so only the documents' plain text "
                         "was read. Tables and scanned pages may be missing.")
            defn, model = _extract_parts(build_content(_text_only(docs), notes), usage)
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001 — every model failure becomes a plain message
        raise _plain_llm_error(e) from e
    prov = defn.get("provenance")
    if isinstance(prov, dict):
        prov["extracted_by"] = f"Lab ingestion ({model})"
    return _finish(defn)


def geocode(q: str) -> list[dict]:
    """Look up a place name → candidate coordinates (OpenStreetMap Nominatim). For the human to
    confirm in the Course & Marks review — we never auto-apply a geocode to a mark."""
    from urllib.parse import urlencode
    url = "https://nominatim.openstreetmap.org/search?" + urlencode(
        {"q": q, "format": "json", "limit": 5})
    _ctype, raw = fetch(url)
    data = json.loads(raw.decode("utf-8", "ignore"))
    return [{"display_name": d.get("display_name"),
             "lat": round(float(d["lat"]), 6), "lon": round(float(d["lon"]), 6)}
            for d in data if d.get("lat") and d.get("lon")]


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).strip().rstrip("`").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("model did not return JSON")
    return json.loads(text[s:e + 1])
