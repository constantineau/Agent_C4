"""Race-document ingestion → RaceDefinition (Lab-0 slice 2).

Dual input: a race URL (discover candidate PDFs), a pasted direct PDF link, or an uploaded PDF.
Document text is pulled with pypdf, then a frontier model (Opus) extracts a structured
RaceDefinition matching `shared/race_def.py`. The result is always a DRAFT — it goes to the
human-review step in the Lab before it's saved/activated (a wrong waypoint is dangerous).
"""
import io
import json
import os
import re
import time
import urllib.request

import pypdf

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MAX_DOC_CHARS = int(os.environ.get("LAB_MAX_DOC_CHARS", "300000"))
IFRAME_TIMEOUT = int(os.environ.get("LAB_IFRAME_TIMEOUT", "60"))   # embedded entry apps can be slow
_UA = {"User-Agent": "Mozilla/5.0 (C4 Performance Lab race-doc ingest)"}


def fetch(url: str, timeout: int = 30):
    """GET a URL → (content_type, raw_bytes). HTTP errors raise."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (r.headers.get("Content-Type", "") or "").lower(), r.read()


def pdf_text(raw: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(raw))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


_IFRAME_SRC = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
# non-content iframes to skip (ads/analytics/social) so we don't waste fetches or add noise
_IFRAME_SKIP = ("google.com", "googletagmanager", "doubleclick", "facebook.", "youtube.",
                "gstatic", "recaptcha", "/gtag", "analytics", "hotjar", "twitter.")


def text_from_url(url: str, follow_iframes: bool = True) -> tuple[str, str]:
    """(label, extracted_text) for a URL — PDF or HTML. HTML content embedded one level deep in a
    content `<iframe>` is followed and appended (bounded): regatta sites commonly serve the entry
    list / results from a separate app inside an iframe (e.g. bycmack's `current-entries` embeds
    `cf.bycmack.com/entries.cfm`), so the visible list is absent from the outer page's own HTML."""
    ctype, raw = fetch(url)
    label = url.rsplit("/", 1)[-1] or url
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return label, pdf_text(raw)
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
    return label, text


_PDF_LINK = re.compile(r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>',
                       re.I | re.S)


def discover_pdfs(url: str) -> list[dict]:
    """Scrape a race page for candidate document PDF links (auto-find)."""
    ctype, raw = fetch(url)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return [{"label": url.rsplit("/", 1)[-1], "url": url}]
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


SCHEMA_BRIEF = """Produce ONE JSON object = a RaceDefinition. Output ONLY the JSON (no prose, no
code fences). Fields:
- race_id (kebab-case from name+year), name, year (int), organizing_authority, start_date (ISO),
  start_area, region, timezone (the IANA tz name of the start venue, e.g. "America/Detroit" — INFER
  it from the venue / start area location; this is the one field you may derive from geography rather
  than the documents. Use "" only if the venue location is genuinely unknown).
- divisions: [{id, name, course_ref (->course id), boat_type}].
- courses: [{id, name, applies_to_divisions:[div ids], distance_nm (or null), note,
  start:{type, ref, coords_source},
  marks:[{seq, name, type in [start,waypoint,gate,island,buoy,finish], rounding in
    [port,starboard,gate,none], lat, lon, lat2, lon2 (gates only), coords_source in
    [nor,si,chart,orc,approx,needs_review,si_pending], note}],
  finish:{type, points:[{name,lat,lon}], crossing, coords_source, note}}].
- zones: [{name, type in [exclusion,tss,hazard], geometry (or null), note}].
- requirements: COMPREHENSIVE checklist [{id (kebab), category in [safety,structural,crew_safety,
  navigation,communications,registration,procedure,reporting,environmental,rules], phase in
  [pre_entry,pre_start,start,in_race,at_gate,at_finish,post_race], text, trigger_type in
  [none,time,event,location], trigger_detail, deliver_to_ipad (bool), critical (bool), source}].
  Capture safety/SER equipment, registration, navigation, and procedural items. Set
  deliver_to_ipad=true for race-time ACTION items the crew must do at a moment (e.g. nav lights at
  sunset, gate GPS photo, finish procedure / displaying numbers) and give them a trigger.
- division_starts: {division id or name -> the division's WARNING/START gun as an ISO local
  datetime "YYYY-MM-DDTHH:MM" (the race venue's local time)} — ONLY when the SIs/NOR state the
  per-division start times; omit entirely when not stated. These drive corrected-time standings.
- rules_profile: {rrs_edition, info_available_to_all_permitted (bool),
  customized_advice_while_underway_prohibited (bool), appendix_wp (bool),
  tracker_permitted (true/false/null), modifications:[{ref,rule,summary}],
  scoring:{system,method,options:[],decided,ref}}.
- fleet: [] (leave empty unless an entry list is present).
- provenance: {sources:[{label,url,retrieved}], si_status, review_status, extracted_by}.

CONVENTIONS: coordinates are DECIMAL DEGREES, WGS84 (N/E positive; convert degrees-decimal-minutes,
W/S negative). If a mark's coordinates are NOT explicitly stated in the documents, set lat/lon to
null and coords_source to "needs_review" — never guess coordinates. Cite the source section in each
requirement/modification 'source'/'ref'. Set provenance.review_status to note it is
machine-extracted and needs human review (list what's uncertain). Set extracted_by to
"Lab ingestion (Opus)". If something is unknown, use null and say so in a note — do not invent."""


def extract_race_definition(docs: list[tuple[str, str]], retrieved: str = "") -> dict:
    """docs = [(label, text)]; returns a draft RaceDefinition dict. Raises on no API key / parse."""
    if not API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set in the Lab service")
    from . import llm as lab_llm
    blob = "\n\n".join(f"===== DOCUMENT: {lbl} =====\n{txt}" for lbl, txt in docs)
    blob = blob[:MAX_DOC_CHARS]
    try:
        text, _model = lab_llm.complete(
            "You convert sailing race documents (NOR / SI / SER / entry lists) into a single "
            "structured RaceDefinition JSON object for a race-strategy tool. Be precise and "
            "comprehensive; never invent coordinates or facts.",
            f"{SCHEMA_BRIEF}\n\nDOCUMENTS:\n{blob}", max_tokens=32000)
    except lab_llm.Truncated:
        raise RuntimeError("extraction hit the output limit — try fewer/smaller documents at once")
    return _parse_json(text)


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
