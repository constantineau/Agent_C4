"""Frontier-LLM helper for the Lab — a MODEL CHAIN with fallback.

EVERY Lab frontier call routes through here (synthesis, briefing, race-doc extraction, entry-list
extraction, the debrief critique): primary **Fable 5.1** (`claude-fable-5-1`), falling back to
**Opus 5** (`claude-opus-5`) if Fable errors, refuses or is unavailable (user decision 2026-07-06 —
docs/PLAYBOOK_V2.md §4; models moved up from claude-fable-5 / claude-opus-4-8 on 2026-11-02).
`ANTHROPIC_MODEL_CHAIN` overrides the chain (comma-separated, tried in order); the old
single-model `ANTHROPIC_MODEL` env is no longer consulted.

These models think on every request (Fable always; Opus 5 by default), and `max_tokens` caps the
thinking AND the answer, so a call never gets less than MIN_MAX_TOKENS. No sampling parameters,
prefill or forced tool_choice are used here (sampling and prefill 400 on both models; forced
tool_choice 400s on Fable 5.1). A refusal
(stop_reason "refusal", HTTP 200) falls through to the next model; its partial text is never
returned as an answer.

Callers keep their own deterministic no-LLM fallback below this chain: `complete()` raising means
"no frontier model answered", and the Lab never depends on one.
"""
import logging
import os

log = logging.getLogger("lab.llm")
DEFAULT_CHAIN = "claude-fable-5-1,claude-opus-5"
MODEL_CHAIN = [m.strip() for m in
               os.environ.get("ANTHROPIC_MODEL_CHAIN", DEFAULT_CHAIN).split(",") if m.strip()]
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MIN_MAX_TOKENS = 16000     # room for thinking + a short answer (judge used to ask for 1200)


class Truncated(RuntimeError):
    """The completion hit max_tokens — the fix is a bigger output budget or smaller input, not
    another model, so the chain does NOT fall through on this."""


def complete(system: str, user: str, max_tokens: int = 4000) -> tuple[str, str]:
    """One system+user completion through the chain. Returns (text, model_used) from the FIRST
    model that answers with non-empty text; raises Truncated on a max_tokens stop, else the last
    error when the whole chain fails (callers treat that like any LLM failure → their
    deterministic fallback). Streamed, so a long answer never waits on one idle HTTP read."""
    client = _client()
    max_tokens = max(max_tokens, MIN_MAX_TOKENS)
    last_err = None
    for model in MODEL_CHAIN:
        try:
            with client.messages.stream(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                resp = stream.get_final_message()
            stop = getattr(resp, "stop_reason", "")
            if stop == "max_tokens":
                raise Truncated(f"{model}: output hit max_tokens={max_tokens}")
            if stop == "refusal":       # partial text before a mid-stream refusal is not an answer
                last_err = RuntimeError(f"{model}: refused")
                continue
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if txt:
                return txt, model
            last_err = RuntimeError(f"{model}: empty completion")
        except Truncated:
            raise
        except Exception as e:      # try the next model in the chain
            last_err = e
    raise last_err if last_err else RuntimeError("empty model chain")


class Refused(RuntimeError):
    """Every model in the chain stopped with stop_reason "refusal"."""


class BadDocument(RuntimeError):
    """The API rejected an attached PDF (400). The caller can resend the documents as text."""


def _client():
    """The SDK client. A seam for tests (they swap in a fake)."""
    import anthropic
    return anthropic.Anthropic(api_key=API_KEY)


def _add_usage(usage, model, msg, seconds, structured):
    if usage is None:
        return
    u = getattr(msg, "usage", None)
    usage.setdefault("calls", []).append({
        "model": model, "seconds": round(seconds, 1), "structured": structured,
        "stop_reason": getattr(msg, "stop_reason", None),
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0})


def complete_json(system: str, content: list, schema: dict | None, max_tokens: int = 64000,
                  usage: dict | None = None) -> tuple[str, str]:
    """One long, structured completion through the chain → (json_text, model_used).

    - STREAMED (get_final_message): a long reply never sits on one idle HTTP read.
    - `schema` is sent as structured outputs (output_config.format), so the reply is schema-valid
      JSON by construction. If a model rejects the schema itself (400 "schema ..."), that model is
      asked again without it; the caller's JSON check + retry then covers it.
    - stop_reason "refusal" → the next model; all refused → Refused. "max_tokens" → Truncated
      (a bigger budget or less input is the fix, not another model).
    - A 400 about the attached PDF → BadDocument (the caller resends as text).
    Other errors fall through the chain; the last one is raised.
    `content` is the user turn's content blocks. `usage`, if given, collects per-call tokens.
    output_config goes through extra_body: the pinned SDK (0.42) predates the named parameter."""
    import time
    import anthropic
    client = _client()
    last_err, refused = None, []
    for model in MODEL_CHAIN:
        use_schema = schema is not None
        for _ in range(2):                       # second pass only when the schema was rejected
            kw = {"model": model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": content}]}
            if use_schema:
                kw["extra_body"] = {"output_config": {"format": {"type": "json_schema",
                                                                 "schema": schema}}}
            t0 = time.monotonic()
            try:
                with client.messages.stream(**kw) as stream:
                    msg = stream.get_final_message()
            except anthropic.BadRequestError as e:
                text = str(getattr(e, "message", "") or e).lower()
                if use_schema and ("schema" in text or "grammar" in text):
                    log.warning("lab llm: %s rejected the output schema (%s); asking without it",
                                model, str(e)[:200])
                    use_schema, last_err = False, e
                    continue
                if "pdf" in text or "document" in text:
                    raise BadDocument(str(e)) from e
                last_err = e
                break
            except Exception as e:  # noqa: BLE001 — try the next model in the chain
                last_err = e
                break
            _add_usage(usage, model, msg, time.monotonic() - t0, use_schema)
            stop = getattr(msg, "stop_reason", "")
            if stop == "max_tokens":
                raise Truncated(f"{model}: output hit max_tokens={max_tokens}")
            if stop == "refusal":
                refused.append(model)
                last_err = Refused(f"{model} refused")
                break
            txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
            if txt:
                return txt, model
            last_err = RuntimeError(f"{model}: empty completion")
            break
    if refused and len(refused) == len(MODEL_CHAIN):
        raise Refused(f"refused by {', '.join(refused)}")
    raise last_err if last_err else RuntimeError("empty model chain")
