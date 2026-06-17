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
import urllib.request

import pypdf

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MAX_DOC_CHARS = int(os.environ.get("LAB_MAX_DOC_CHARS", "300000"))
_UA = {"User-Agent": "Mozilla/5.0 (C4 Performance Lab race-doc ingest)"}


def fetch(url: str):
    """GET a URL → (content_type, raw_bytes). HTTP errors raise."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return (r.headers.get("Content-Type", "") or "").lower(), r.read()


def pdf_text(raw: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(raw))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def text_from_url(url: str) -> tuple[str, str]:
    """(label, extracted_text) for a URL — PDF or HTML."""
    ctype, raw = fetch(url)
    label = url.rsplit("/", 1)[-1] or url
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return label, pdf_text(raw)
    # HTML: strip tags to plain-ish text
    html = raw.decode("utf-8", "ignore")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return label, re.sub(r"\s+", " ", text)


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
  start_area, region.
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
    import anthropic
    blob = "\n\n".join(f"===== DOCUMENT: {lbl} =====\n{txt}" for lbl, txt in docs)
    blob = blob[:MAX_DOC_CHARS]
    client = anthropic.Anthropic(api_key=API_KEY)
    resp = client.messages.create(
        model=MODEL, max_tokens=32000,
        system="You convert sailing race documents (NOR / SI / SER / entry lists) into a single "
               "structured RaceDefinition JSON object for a race-strategy tool. Be precise and "
               "comprehensive; never invent coordinates or facts.",
        messages=[{"role": "user", "content": f"{SCHEMA_BRIEF}\n\nDOCUMENTS:\n{blob}"}],
    )
    if getattr(resp, "stop_reason", "") == "max_tokens":
        raise RuntimeError("extraction hit the output limit — try fewer/smaller documents at once")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
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
