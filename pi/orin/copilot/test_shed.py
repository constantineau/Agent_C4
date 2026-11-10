"""Load shed level 1 pauses narration (#248): the copilot makes no LLM call while the Orin's guard
says so, and says so honestly in /health.

Pins:
  * level 1+ → LLMClient.chat / chat_stream raise NarrationPaused (an LLMUnavailable, so every
    caller takes its deterministic fallback) and never reach Ollama;
  * level 0, a stale state file (dead guard) or no file at all → the LLM is called as normal;
  * /health reports mode "deterministic" and the plain reason while narration is paused.

Pure: urlopen is stubbed, no network.
    cd pi/orin && python3 -m copilot.test_shed
"""
import io
import json
import os
import sys
import tempfile
import time

from shed import guard

from . import app as app_mod
from . import llm as llm_mod

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


calls = []


def fake_urlopen(req, timeout=None):
    url = req if isinstance(req, str) else req.full_url
    calls.append(url)
    if url.endswith("/models"):
        return _Resp(b'{"data": []}')
    if json.loads(req.data).get("stream"):
        return _Resp(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n')
    return _Resp(json.dumps({"choices": [{"message": {"role": "assistant",
                                                      "content": "ok"}}]}).encode())


def set_level(path, level, cause="memory", age=0.0):
    guard.write_state({"level": level, "cause": cause, "updated": time.time() - age,
                       "paused": guard.PAUSED_AT.get(level, [])}, path)


def try_chat():
    calls.clear()
    try:
        llm_mod.LLMClient(base_url="http://127.0.0.1:11434/v1").chat(
            [{"role": "user", "content": "hello"}])
        return "called"
    except llm_mod.NarrationPaused as exc:
        return f"paused:{exc}"
    except llm_mod.LLMUnavailable as exc:
        return f"unavailable:{exc}"


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "state.json")
    os.environ["SHED_STATE"] = path
    orig_open = llm_mod.urllib.request.urlopen
    llm_mod.urllib.request.urlopen = fake_urlopen
    try:
        print("narration pause (level 1+)")
        check("no state file → LLM called", try_chat() == "called" and calls)
        set_level(path, 0)
        check("level 0 → LLM called", try_chat() == "called" and calls)
        set_level(path, 1)
        r = try_chat()
        check("level 1 → refused before any request", r.startswith("paused:") and not calls)
        check("…with the plain reason", r == "paused:Narration paused: Orin low on memory.")
        check("NarrationPaused is an LLMUnavailable (callers fall back)",
              issubclass(llm_mod.NarrationPaused, llm_mod.LLMUnavailable))
        set_level(path, 3)
        calls.clear()
        try:
            list(llm_mod.LLMClient().chat_stream([{"role": "user", "content": "x"}]))
            streamed = "called"
        except llm_mod.NarrationPaused:
            streamed = "paused"
        check("level 3 → the streamed deep-dive is refused too", streamed == "paused" and not calls)
        set_level(path, 2, age=120)
        check("stale state (guard dead) → LLM called as normal", try_chat() == "called")

        print("/health")

        class Eng:
            base_url = "http://10.10.10.1:8200"

            def reachable(self):
                return True

        class LLM:
            def reachable(self):
                return True

        o_e, o_l, o_use = app_mod.EngineClient, app_mod.LLMClient, app_mod.config.USE_LLM
        app_mod.EngineClient, app_mod.LLMClient, app_mod.config.USE_LLM = Eng, LLM, True
        try:
            set_level(path, 0)
            h = app_mod.health()
            check("level 0 → mode llm", h["mode"] == "llm" and h["shed"]["level"] == 0)
            set_level(path, 1, cause="heat")
            h = app_mod.health()
            check("level 1 → mode deterministic", h["mode"] == "deterministic")
            check("…and the reason in plain words",
                  h["shed"]["detail"] == "Narration paused: Orin running hot."
                  and h["shed"]["guard"] == "on")
        finally:
            app_mod.EngineClient, app_mod.LLMClient, app_mod.config.USE_LLM = o_e, o_l, o_use
    finally:
        llm_mod.urllib.request.urlopen = orig_open
        os.environ.pop("SHED_STATE", None)
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
