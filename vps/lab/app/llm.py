"""Frontier-LLM helper for the Lab — a MODEL CHAIN with fallback.

EVERY Lab frontier call routes through here (synthesis, briefing, race-doc extraction, entry-list
extraction, the debrief critique): primary **Fable** (`claude-fable-5`), falling back to **Opus**
(`claude-opus-4-8`) if Fable errors or is unavailable (user decision 2026-07-06 —
docs/PLAYBOOK_V2.md §4). `ANTHROPIC_MODEL_CHAIN` overrides the chain (comma-separated, tried in
order); the old single-model `ANTHROPIC_MODEL` env is no longer consulted.

Callers keep their own deterministic no-LLM fallback below this chain: `complete()` raising means
"no frontier model answered", and the Lab never depends on one.
"""
import os

DEFAULT_CHAIN = "claude-fable-5,claude-opus-4-8"
MODEL_CHAIN = [m.strip() for m in
               os.environ.get("ANTHROPIC_MODEL_CHAIN", DEFAULT_CHAIN).split(",") if m.strip()]
API_KEY = os.environ.get("ANTHROPIC_API_KEY")


class Truncated(RuntimeError):
    """The completion hit max_tokens — the fix is a bigger output budget or smaller input, not
    another model, so the chain does NOT fall through on this."""


def complete(system: str, user: str, max_tokens: int = 4000) -> tuple[str, str]:
    """One system+user completion through the chain. Returns (text, model_used) from the FIRST
    model that answers with non-empty text; raises Truncated on a max_tokens stop, else the last
    error when the whole chain fails (callers treat that like any LLM failure → their
    deterministic fallback)."""
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)
    last_err = None
    for model in MODEL_CHAIN:
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            if getattr(resp, "stop_reason", "") == "max_tokens":
                raise Truncated(f"{model}: output hit max_tokens={max_tokens}")
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if txt:
                return txt, model
            last_err = RuntimeError(f"{model}: empty completion")
        except Truncated:
            raise
        except Exception as e:      # try the next model in the chain
            last_err = e
    raise last_err if last_err else RuntimeError("empty model chain")
