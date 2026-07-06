"""Frontier-LLM helper for the Lab — a MODEL CHAIN with fallback.

The playbook synthesis + routing briefing use the strongest available frontier model: primary
**Fable** (`claude-fable-5`), falling back to **Opus** (`claude-opus-4-8`) if Fable errors or is
unavailable (user decision 2026-07-06 — docs/PLAYBOOK_V2.md §4). `ANTHROPIC_MODEL_CHAIN` overrides
the chain (comma-separated, tried in order). The plain `ANTHROPIC_MODEL` env is NOT consulted here —
it stays the single-model knob for the other Lab call sites (extract/judge) until they migrate.

Callers keep their own deterministic no-LLM fallback below this chain: `complete()` raising means
"no frontier model answered", and the Lab never depends on one.
"""
import os

DEFAULT_CHAIN = "claude-fable-5,claude-opus-4-8"
MODEL_CHAIN = [m.strip() for m in
               os.environ.get("ANTHROPIC_MODEL_CHAIN", DEFAULT_CHAIN).split(",") if m.strip()]
API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def complete(system: str, user: str, max_tokens: int = 4000) -> tuple[str, str]:
    """One system+user completion through the chain. Returns (text, model_used) from the FIRST
    model that answers with non-empty text; raises the last error when the whole chain fails
    (callers treat that like any LLM failure → their deterministic fallback)."""
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)
    last_err = None
    for model in MODEL_CHAIN:
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if txt:
                return txt, model
            last_err = RuntimeError(f"{model}: empty completion")
        except Exception as e:      # try the next model in the chain
            last_err = e
    raise last_err if last_err else RuntimeError("empty model chain")
