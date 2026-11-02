"""Ingest as a background job (POST → job_id, poll status) + the Lab model chain (Fable 5.1 →
Opus 5). No network or API key: fake fetch + a fake SDK client.
Run (make ci): python3 vps/lab/test_ingest_job.py (workdir /srv in the lab image)"""
import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
from types import SimpleNamespace

os.environ["INGESTED_DIR"] = tempfile.mkdtemp()
os.environ.pop("ANTHROPIC_MODEL_CHAIN", None)

from app import extract, llm, main  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


COURSE = json.dumps({"race_id": "sorc-2027", "name": "SORC", "year": 2027, "courses": [],
                     "divisions": [], "division_starts": []})
RULES = json.dumps({"requirements": [], "rules_profile": {"tracker_permitted": None},
                    "provenance": {"sources": [], "si_status": "", "review_status": "",
                                   "extracted_by": ""}})


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def msg(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=None)


GATE = threading.Event()
GATE.set()
SENT = []


def stream(**kw):
    SENT.append(kw)
    GATE.wait(30)
    last = kw["messages"][0]["content"]
    last = last[-1]["text"] if isinstance(last, list) else last
    return _Stream(msg(COURSE if "fields: race_id" in last else RULES))


llm._client = lambda: SimpleNamespace(messages=SimpleNamespace(stream=stream))
extract.API_KEY = "test"


def fake_fetch(url, timeout=30):
    if "gone" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "NOTICE OF RACE " * 5)
    c.showPage()
    c.save()
    return "application/pdf", buf.getvalue()


extract.fetch = fake_fetch
_sleep = time.sleep                     # extract.time IS the time module: keep a real sleep
extract.time.sleep = lambda s: None     # no retry back-off in fetch_doc


def poll(job_id, limit=60):
    for _ in range(limit * 10):
        st = asyncio.run(main.ingest_run_status(job_id))
        if st.get("state") != "running":
            return st
        _sleep(0.1)
    return st


print("URL ingest as a job: POST returns a job id, status ends with the draft")
r = asyncio.run(main.ingest_run({"urls": ["https://x.org/nor.pdf"]}))
check("started with a job id", isinstance(r, dict) and r.get("ok") and len(r.get("job_id", "")) == 12)
st = poll(r["job_id"])
check("done with the draft", st.get("state") == "done"
      and st["result"]["definition"]["name"] == "SORC" and "warnings" in st["result"])
prog = " | ".join(st.get("progress") or [])
check("plain progress lines", "Reading documents… (1 of 1) nor.pdf" in prog
      and "Building the course and the checklist" in prog
      and "Course done." in prog and "Checklist done." in prog)

print("upload as a job")


class _Up:
    def __init__(self, name, raw):
        self.filename, self._raw = name, raw

    async def read(self):
        return self._raw


r = asyncio.run(main.ingest_run_upload([_Up("si.pdf", fake_fetch("x")[1])]))
st = poll(r["job_id"])
check("done with the draft", st.get("state") == "done" and st["result"]["definition"]["race_id"]
      == "sorc-2027" and st["result"]["definition"]["provenance"]["sources"][0]["url"] == "(uploaded)")

print("a failure inside the job comes back as the plain sentence")
r = asyncio.run(main.ingest_run({"urls": ["https://x.org/gone.pdf"]}))
st = poll(r["job_id"])
check("result.detail is the plain message", st.get("state") == "done" and st["result"].get(
    "detail", "").startswith("Couldn't download gone.pdf: the link is broken (404 not found)."))

print("a Lab restart mid-job: the id is unknown → 'interrupted', not a spinner")
st = asyncio.run(main.ingest_run_status("deadbeef0000"))
check("plain interrupted message", st == {"state": "error", "job_id": "deadbeef0000",
                                          "error": "Ingest was interrupted. Run it again."})

print("one ingest at a time: a second start is refused plainly")
GATE.clear()
r1 = asyncio.run(main.ingest_run({"urls": ["https://x.org/nor.pdf"]}))
_sleep(0.3)
r2 = asyncio.run(main.ingest_run({"urls": ["https://x.org/nor.pdf"]}))
body = json.loads(r2.body) if hasattr(r2, "body") else {}
check("409 + plain busy message", getattr(r2, "status_code", 0) == 409
      and body.get("detail", "").startswith("Another ingest is already running"))
check("the first job is still the one reported", asyncio.run(
    main.ingest_run_status(r1["job_id"])).get("state") == "running")
GATE.set()
poll(r1["job_id"])

print("model chain: Fable 5.1 then Opus 5, for every Lab LLM call")
check("default chain", llm.DEFAULT_CHAIN == "claude-fable-5-1,claude-opus-5"
      and llm.MODEL_CHAIN == ["claude-fable-5-1", "claude-opus-5"])
compose = "/repo/compose.dev.yml"
if os.path.exists(compose):
    check("compose default matches", "${ANTHROPIC_MODEL_CHAIN:-claude-fable-5-1,claude-opus-5}"
          in open(compose).read())
else:
    print("  [SKIP] compose.dev.yml not mounted")

print("complete(): streamed, room for thinking, no params these models reject")
SENT.clear()
txt, model = llm.complete("sys", "Race facts", max_tokens=1200)     # the judge's old budget
kw = SENT[0]
check("first model is Fable 5.1", model == "claude-fable-5-1" and kw["model"] == "claude-fable-5-1")
check("max_tokens floor for thinking + answer", kw["max_tokens"] >= 16000)
check("no temperature / top_p / tool_choice / thinking config / prefill",
      not ({"temperature", "top_p", "top_k", "tool_choice", "thinking"} & set(kw))
      and [m["role"] for m in kw["messages"]] == ["user"])

print("complete(): a refusal (even with partial text) falls through to Opus 5")
script = [msg('{"partial": ', stop="refusal"), msg('{"ok": true}')]
SENT.clear()
llm._client = lambda: SimpleNamespace(messages=SimpleNamespace(
    stream=lambda **kw: (SENT.append(kw), _Stream(script[len(SENT) - 1]))[1]))
txt, model = llm.complete("sys", "x")
check("Opus 5 answered, the partial text was not returned", model == "claude-opus-5"
      and txt == '{"ok": true}' and SENT[1]["model"] == "claude-opus-5")

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
