"""Race ingest robustness: every way reading race documents can fail ends in a plain message,
and nothing is dropped without saying so. No network or API key: a fake SDK client stands in for
Claude and a fake fetch for the web.
Run (make ci): python3 vps/lab/test_extract_robust.py (workdir /srv in the lab image)"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import urllib.error
from types import SimpleNamespace

os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

import anthropic  # noqa: E402
import httpx  # noqa: E402

from app import extract, llm  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


GOOD = json.dumps({"race_id": "sorc-2027", "name": "SORC", "year": 2027,
                   "division_starts": [{"division": "ORC A", "gun": "2027-02-12T10:00"}],
                   "provenance": {"sources": [], "si_status": "", "review_status": "",
                                  "extracted_by": ""}})
NOR = "NOTICE OF RACE. 1 RULES. The regatta will be governed by the rules. " * 10


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def reply(text=GOOD, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


def bad_request(msg):
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(msg, response=resp, body=None)


RULES = json.dumps({"requirements": [{"id": "lights", "category": "navigation",
                                        "phase": "in_race", "text": "Nav lights at sunset"}],
                     "zones": [{"name": "TSS", "type": "tss", "geometry": {"coordinates": []},
                                "note": ""}],
                     "provenance": {"sources": [], "si_status": "", "review_status": "",
                                    "extracted_by": ""}})


def part_of(kw):
    return "course" if "fields: race_id" in kw["messages"][0]["content"][-1]["text"] else "rules"


def fake(course, rules=None):
    """Scripts per part — one item per API call: a reply() or an exception to raise. Returns
    {part: [the calls' kwargs]}."""
    script = {"course": course, "rules": rules or [reply(RULES)] * 4}
    calls = {"course": [], "rules": []}

    def stream(**kw):
        part = part_of(kw)
        calls[part].append(kw)
        item = script[part][len(calls[part]) - 1]
        if isinstance(item, Exception):
            raise item
        return _Stream(item)
    llm._client = lambda: SimpleNamespace(messages=SimpleNamespace(stream=stream))
    return calls


def run(docs, **kw):
    """→ (definition or None, error text or "", notes)."""
    notes = []
    try:
        return extract.extract_race_definition(docs, "2026-11-02", notes, **kw), "", notes
    except extract.IngestError as e:
        return None, str(e), notes


def blank_pdf(pages=1):
    """A scan-like PDF: pages with a drawing and no text layer."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for _ in range(pages):
        c.rect(100, 100, 300, 300, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


extract.API_KEY = "test"
llm.MODEL_CHAIN = ["model-a", "model-b"]

print("structured outputs + streaming: the schema goes out, the reply comes back parsed")
calls = fake([reply()])
usage = {}
d, err, notes = run([("nor.pdf", NOR)], usage=usage)
for part in ("course", "rules"):
    fmt = (calls[part][0].get("extra_body") or {}).get("output_config", {}).get("format", {})
    check(f"{part} part sent with its json_schema", fmt.get("type") == "json_schema"
          and list(fmt["schema"]["properties"]) == extract.PARTS[part])
check("the two parts cover the whole schema",
      sorted(extract.PARTS["course"] + extract.PARTS["rules"]) == sorted(extract.RACE_SCHEMA["properties"]))
check("parsed and merged", d and d["race_id"] == "sorc-2027" and d["requirements"][0]["id"] == "lights"
      and not err)
check("an empty zone geometry becomes null", d["zones"][0]["geometry"] is None)
check("division_starts list → the Lab's {division: gun} map",
      d["division_starts"] == {"ORC A": "2027-02-12T10:00"})
check("provenance names the model that read it", d["provenance"]["extracted_by"] == "Lab ingestion (model-a)")
check("usage recorded per call", len(usage["calls"]) == 2
      and usage["calls"][0]["input_tokens"] == 1000 and usage["calls"][0]["model"] == "model-a"
      and all(c["structured"] for c in usage["calls"]))

print("schema rules: every object strict, within the unions limit")


def walk(s, out):
    if isinstance(s, dict):
        if s.get("type") == "object" or (isinstance(s.get("type"), list) and "object" in s["type"]):
            out["objects"] += 1
            if s.get("additionalProperties") is not False or set(s["required"]) != set(s["properties"]):
                out["loose"] += 1
        if isinstance(s.get("type"), list) or "anyOf" in s:
            out["unions"] += 1
        for v in s.values():
            walk(v, out)
    elif isinstance(s, list):
        for v in s:
            walk(v, out)


cnt = {"objects": 0, "loose": 0, "unions": 0}
walk(extract.RACE_SCHEMA, cnt)
check(f"no loose objects ({cnt['objects']} objects)", cnt["loose"] == 0)
check(f"unions within 16 ({cnt['unions']})", cnt["unions"] <= 16)

print("the API rejects the schema: same model again without it, JSON check still applies")
calls = fake([bad_request("The compiled grammar is too large, which would cause performance issues. "
                          "Simplify your tool schemas or reduce the number of strict tools."),
              reply()])
d, err, _ = run([("nor.pdf", NOR)])
check("second call has no schema", len(calls["course"]) == 2
      and "extra_body" not in calls["course"][1] and calls["course"][1]["model"] == "model-a")
check("still parsed", d and d["name"] == "SORC")

print("refusal: the next model tries; all refusing → a plain message")
calls = fake([reply("", stop="refusal"), reply()])
d, err, _ = run([("nor.pdf", NOR)])
check("model-b answered after model-a refused", d and calls["course"][1]["model"] == "model-b")
calls = fake([reply("", stop="refusal"), reply("", stop="refusal")])
d, err, _ = run([("nor.pdf", NOR)])
check("plain refusal message", d is None and "declined" in err and "Try again" in err)
check("refusal text is not parsed as JSON (no JSON retry)", len(calls["course"]) == 2)

print("max_tokens: stop, say what to do, don't try the next model")
calls = fake([reply('{"race_id": "x", "na', stop="max_tokens"), reply()])
d, err, _ = run([("nor.pdf", NOR)])
check("plain message", d is None and "too long" in err and "separately" in err)
check("one call only", len(calls["course"]) == 1)

print("service errors become plain messages")
calls = fake([anthropic.RateLimitError("rate", response=httpx.Response(
    429, request=httpx.Request("POST", "https://x")), body=None)] * 2)
d, err, _ = run([("nor.pdf", NOR)])
check("busy → try again later", "busy" in err and "few minutes" in err)

print("scanned PDF: sent as a PDF, not dropped")
scan = blank_pdf(2)
doc = extract.load_pdf("scan.pdf", scan)
check("pypdf finds no text in it", len(doc["text"].strip()) == 0 and doc["pages"] == 2)
calls = fake([reply()])
d, err, notes = run([doc])
blocks = calls["course"][0]["messages"][0]["content"]
docblk = [b for b in blocks if b["type"] == "document"]
check("one document block, base64 PDF", len(docblk) == 1
      and docblk[0]["source"]["media_type"] == "application/pdf"
      and docblk[0]["source"]["data"].startswith("JVBER"))   # base64 of "%PDF"
check("labelled and before the instructions", blocks[0]["text"].endswith("scan.pdf =====")
      and blocks[-1]["text"].startswith(extract.SCHEMA_BRIEF))
check("no 'unreadable' note", d and not notes)

print("scanned PDF over the PDF budget: it can't be read → a plain error, not an empty draft")
saved = extract.PDF_MAX_PAGES
extract.PDF_MAX_PAGES = 1
calls = fake([reply()])
d, err, notes = run([doc])
check("plain error, no API call", d is None and "readable text" in err
      and not calls["course"] and not calls["rules"])
check("the note says it was too big", any("scan.pdf is too big" in n for n in notes))
extract.PDF_MAX_PAGES = saved

print("restricted PDF (empty open password): read, and sent as an unlocked PDF")


def locked_pdf(user_pw):
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=StandardEncryption(user_pw, ownerPassword="owner",
                                                      canPrint=0, canCopy=0))
    c.drawString(100, 700, "AMENDMENT 1: The start is postponed to 1100.")
    c.showPage()
    c.save()
    return buf.getvalue()


import pypdf  # noqa: E402

rdoc = extract.load_pdf("amendment.pdf", locked_pdf(""))
check("its text is read", "postponed to 1100" in rdoc["text"] and not rdoc["note"])
check("the copy sent is not encrypted", not pypdf.PdfReader(io.BytesIO(rdoc["pdf"])).is_encrypted)
calls = fake([reply()])
d, err, notes = run([rdoc])
check("sent as a document block, no notes", d and not notes and any(
    b["type"] == "document" for b in calls["course"][0]["messages"][0]["content"]))
try:
    extract.load_pdf("locked.pdf", locked_pdf("secret"))
    err = ""
except extract.IngestError as e:
    err = str(e)
check("a real open password is refused plainly", err == "locked.pdf is password-protected. "
      "Upload an unlocked copy.")

print("restricted PDF whose unlocked copy fails, then pypdf fails: a doc, not a TypeError")


class _BrokenReader:
    is_encrypted = True

    def decrypt(self, pw):
        return 1

    @property
    def pages(self):
        raise ValueError("broken xref")


saved_reader, saved_unlock = extract._pdf_reader, extract._unlocked_copy
extract._pdf_reader = lambda raw: _BrokenReader()
extract._unlocked_copy = lambda reader, label: (None, "copy failed")
try:
    rdoc = extract.load_pdf("odd.pdf", PDF_ODD := blank_pdf(2))
    got = ""
except Exception as e:  # noqa: BLE001
    rdoc, got = None, type(e).__name__
extract._pdf_reader, extract._unlocked_copy = saved_reader, saved_unlock
check(f"no crash ({got or 'ok'})", rdoc is not None)
check("original bytes kept, pages counted the crude way", rdoc and rdoc["pdf"] == PDF_ODD
      and rdoc["pages"] == 2)

print("a short amendment as text counts as content")
calls = fake([reply()])
d, err, notes = run([("amendment-1.html", "Amendment 1: Start postponed to 1100")])
check("read, no error, no note", d and not err and not notes)
check("its text was sent", "Start postponed to 1100" in json.dumps(calls["course"][0]["messages"]))

print("out of API credit: says so")
calls = fake([bad_request("Your credit balance is too low to access the Anthropic API.")] * 2)
d, err, _ = run([("nor.pdf", NOR)])
check("plain credit message", "out of credit" in err)

print("the API rejects a PDF: resent as text, and the draft says so")
pdfdoc = dict(extract.load_pdf("scan.pdf", scan), text=NOR)
calls = fake([bad_request("messages.0.content.1: The PDF specified could not be processed"),
              reply()])
d, err, notes = run([pdfdoc])
check("second call has no document block", len(calls["course"]) == 2 and not any(
    b["type"] == "document" for b in calls["course"][1]["messages"][0]["content"]))
check("parsed, with a note", d and any("couldn't open a PDF" in n for n in notes))

print("oversized text: trimmed fairly and the draft says how much")
saved = extract.MAX_DOC_CHARS
extract.MAX_DOC_CHARS = 3000
small, big = "S" * 1000, "B" * 9000
calls = fake([reply()])
d, err, notes = run([("si.pdf", small), ("entries.html", big)])
sent = " ".join(b.get("text", "") for b in calls["course"][0]["messages"][0]["content"])
check("the short doc is sent whole", "S" * 1000 in sent)
check("the long doc is cut to fit", sent.count("B") == 2000)
check("a note names it with the share read", any("entries.html is too long" in n and "22%" in n
                                                 for n in notes))
check("no note for the short one", not any("si.pdf" in n for n in notes))
extract.MAX_DOC_CHARS = saved

print("one bad URL: the others go ahead, the draft names the one skipped")
from app import main  # noqa: E402

PDF = blank_pdf(1)
fetched = []


def fake_fetch(url, timeout=30):
    fetched.append(url)
    if "gone" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    if "flaky" in url and fetched.count(url) == 1:
        raise urllib.error.URLError(TimeoutError("timed out"))
    return "application/pdf", PDF


extract.fetch = fake_fetch
extract.time.sleep = lambda s: None
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/nor.pdf", "https://x.org/gone.pdf"]}))
w = r.get("warnings", []) if isinstance(r, dict) else []
check("draft built from the good one", isinstance(r, dict) and r["definition"]["name"] == "SORC")
check("first warning names the skipped doc, plainly",
      w and w[0].startswith("Ingest: Couldn't download gone.pdf: the link is broken (404 not found).")
      and "WITHOUT it" in w[0])
inotes = r["definition"]["provenance"].get("ingest_notes") or []
check("the note is kept IN the definition, unacknowledged", len(inotes) == 1
      and inotes[0]["text"].startswith("Couldn't download gone.pdf") and inotes[0]["acknowledged"] is False)

print("the note survives a save and stays listed until dismissed")
defn = r["definition"]
saved = asyncio.run(main.save_race({"definition": defn, "draft": True}))
check("save keeps listing it", saved["warnings"][0].startswith("Ingest: Couldn't download gone.pdf"))
stored = main.store.get_race(saved["race_id"]) or {}
check("stored on disk with the definition",
      (stored.get("provenance") or {}).get("ingest_notes") == inotes)
v = main.validate(saved["race_id"])
check("a later validation (reopen) still lists it", isinstance(v, dict)
      and any(x.startswith("Ingest: Couldn't download gone.pdf") for x in v["warnings"]))
defn["provenance"]["ingest_notes"][0]["acknowledged"] = True
saved = asyncio.run(main.save_race({"definition": defn}))
check("dismissed → no longer listed", not any(x.startswith("Ingest:") for x in saved["warnings"]))
check("only the good doc is a source", [s["label"] for s in r["definition"]["provenance"]["sources"]]
      == ["nor.pdf"])
check("a 404 is not retried", fetched.count("https://x.org/gone.pdf") == 1)

print("an embedded iframe that won't load: the page still goes, and the draft says what's missing")


def iframe_fetch(url, timeout=30):
    if "entries.cfm" in url:
        raise urllib.error.URLError(TimeoutError("timed out"))
    return "text/html", (b"<html><body>" + b"Race page. Notice of Race details. " * 20
                         + b'<iframe src="https://cf.x.org/entries.cfm"></iframe></body></html>')


extract.fetch = iframe_fetch
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/current-entries/"]}))
inotes = [n["text"] for n in r["definition"]["provenance"]["ingest_notes"]] if isinstance(r, dict) else []
check("draft built from the page", isinstance(r, dict) and r["definition"]["name"] == "SORC")
check("persisted note names the embedded page", any(n.startswith(
    "Couldn't load the embedded entries.cfm on current-entries: the site didn't answer")
    for n in inotes))
check("and it's listed for review", any(w.startswith("Ingest: Couldn't load the embedded entries.cfm")
                                        for w in r["warnings"]))
extract.fetch = fake_fetch

print("a scanned PDF embedded in a page goes to the model as a PDF, like a direct link")
PAGE_TXT = b"Race page. Notice of Race details and the course. " * 10
SCAN = blank_pdf(3)
ROUTES = {}


def route_fetch(url, timeout=30):
    for key, val in ROUTES.items():
        if key in url:
            if isinstance(val, Exception):
                raise val
            return val
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def page(*embeds, text=PAGE_TXT):
    return "text/html", (b"<html><body>" + text + b"".join(embeds) + b"</body></html>")


extract.fetch = route_fetch
ROUTES.clear()
ROUTES.update({"si.pdf": ("application/pdf", SCAN),
               "/race/": page(b'<iframe src="/docs/si.pdf"></iframe>', text=b"")})
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/race/"]}))
blocks = calls["course"][0]["messages"][0]["content"] if calls["course"] else []
docs_sent = [b for b in blocks if b["type"] == "document"]
check("the embedded scan is a document block", len(docs_sent) == 1
      and base64.standard_b64decode(docs_sent[0]["source"]["data"]) == SCAN)
check("labelled as embedded", any(b.get("text", "").endswith("si.pdf (embedded in race) =====")
                                  for b in blocks))
check("listed as a source", isinstance(r, dict) and any(
    s["url"] == "https://x.org/docs/si.pdf" for s in r["definition"]["provenance"]["sources"]))
check("no notes (the wrapper page's empty text isn't a loss)", isinstance(r, dict)
      and not r["definition"]["provenance"]["ingest_notes"])

print("...and over the PDF budget it falls back to text, with notes")
saved_pages = extract.PDF_MAX_PAGES
extract.PDF_MAX_PAGES = 1
ROUTES["/race/"] = page(b'<iframe src="/docs/si.pdf"></iframe>')
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/race/"]}))
inotes = [n["text"] for n in r["definition"]["provenance"]["ingest_notes"]] if isinstance(r, dict) else []
check("note: too big to read as a PDF", any("si.pdf (embedded in race) is too big" in n for n in inotes))
check("note: no readable text (scan)", any("si.pdf (embedded in race) had no readable text" in n
                                           for n in inotes))
extract.PDF_MAX_PAGES = saved_pages

print("nested: race page → viewer → si.pdf; the SI still goes as a PDF, deeper links are named")
ROUTES.clear()
ROUTES.update({"si.pdf": ("application/pdf", SCAN),
               "/viewer": page(b'<iframe src="/docs/si.pdf"></iframe>', text=b"PDF viewer"),
               "/race/": page(b'<iframe src="/viewer"></iframe>')})
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/race/"]}))
blocks = calls["course"][0]["messages"][0]["content"] if calls["course"] else []
check("the SI behind the viewer is a document block", any(
    b["type"] == "document" and base64.standard_b64decode(b["source"]["data"]) == SCAN
    for b in blocks))
check("listed as a source", isinstance(r, dict) and any(
    s["url"] == "https://x.org/docs/si.pdf" for s in r["definition"]["provenance"]["sources"]))
ROUTES.update({"/viewer": page(b'<iframe src="/deeper"></iframe>', text=b"PDF viewer"),
               "/deeper": page(b'<iframe src="/docs/si.pdf"></iframe>', text=b"inner")})
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/race/"]}))
inotes = " | ".join(n["text"] for n in r["definition"]["provenance"]["ingest_notes"]) \
    if isinstance(r, dict) else ""
check("past the depth limit, the skipped link is named in a note",
      "The embedded deeper on viewer embeds more pages that weren't opened (too deep): si.pdf"
      in inotes)

print("audit gaps: everything a page can't give us becomes a persisted note")
ROUTES.clear()
ROUTES.update({
    "form.docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  b"PK\x03\x04\x00\x00binary"),
    "/many/": page(*[f'<iframe src="/e{i}.html"></iframe>'.encode() for i in range(5)]),
    "/e": ("text/html", b"<p>" + b"entry " * 50 + b"</p>"),
    "/spa/": page(text=b"You need to enable JavaScript to run this app."),
    "/blank/": page(b'<iframe src="/app/list.html"></iframe>'),
    "list.html": ("text/html", b"<html><script>render()</script></html>"),
    "/withimg/": page(b'<embed src="/pics/map.png">'),
    "map.png": ("image/png", b"\x89PNG\x00\x00"),
    "/notpdf/": page(b'<object data="/docs/fake.pdf"></object>'),
    "fake.pdf": ("application/pdf", b"<html>error page</html>"),
    "/nor.pdf": ("application/pdf", PDF),
})
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": [
    "https://x.org/nor.pdf", "https://x.org/form.docx", "https://x.org/many/",
    "https://x.org/spa/", "https://x.org/blank/", "https://x.org/withimg/",
    "https://x.org/notpdf/"]}))
inotes = " | ".join(n["text"] for n in r["definition"]["provenance"]["ingest_notes"]) \
    if isinstance(r, dict) else ""
check("a Word file is skipped by name", "form.docx is a application/vnd.openxmlformats" in inotes)
check("embeds past the cap are named", "many embeds 5 pages; only the first 3 were read "
      "(skipped: e3.html, e4.html)" in inotes)
check("a JavaScript-only page is flagged", "spa has very little text" in inotes)
check("an empty embedded page is flagged", "The embedded list.html on blank had no readable text"
      in inotes)
check("an embedded image is flagged", "The embedded map.png on withimg is a image/png file" in inotes)
check("an embedded 'PDF' that isn't one is flagged",
      "Couldn't read the embedded fake.pdf on notpdf: fake.pdf (embedded in notpdf) isn't a PDF"
      in inotes)
extract.fetch = fake_fetch

print("a transient failure is retried once")
fetched.clear()
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/flaky.pdf"]}))
check("fetched twice, then fine", fetched.count("https://x.org/flaky.pdf") == 2
      and isinstance(r, dict) and r["definition"]["name"] == "SORC")

print("every URL fails: a plain 502 naming the doc, no AI call")
calls = fake([reply()])
r = asyncio.run(main.ingest_urls({"urls": ["https://x.org/gone.pdf"]}))
body = json.loads(r.body) if hasattr(r, "body") else {}
check("502 with the plain reason", getattr(r, "status_code", 0) == 502
      and body.get("detail", "").startswith("Couldn't download gone.pdf: the link is broken"))
check("no AI call", not calls["course"] and not calls["rules"])

print("upload: a non-PDF file is skipped by name")


class _Up:
    def __init__(self, name, raw):
        self.filename, self._raw = name, raw

    async def read(self):
        return self._raw


calls = fake([reply()])
r = asyncio.run(main.ingest_upload([_Up("nor.pdf", PDF), _Up("photo.jpg", b"\xff\xd8\xff")]))
check("draft from the PDF, note for the other", isinstance(r, dict)
      and r["warnings"][0].startswith("Ingest: photo.jpg isn't a PDF file"))

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
