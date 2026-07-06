"""Opus teacher — a strong (but not infallible) reference candidate for sailors to rank.

Mirrors the rest of the repo (summarizer/extract/agent): the `anthropic` SDK, `claude-opus-4-8`.
Best-effort — returns None on a missing key / SDK / any API error, so candidate generation simply
skips the Opus origin and the pipeline never blocks. Opus is NOT treated as ground truth here: it's
one candidate in the ranking, and sailors are free to rank it below a base-model sample. (Opus's
job as ground-truth teacher is in Track A — reliability/format — not tactical taste.)
"""
import json

from . import config


def _prompt(system_prompt: str, seed: str) -> tuple[str, str]:
    sys = system_prompt + (
        "\n\nReturn ONLY a JSON object, no prose, shaped exactly:\n"
        '{"assessment":"<1-2 sentences>",'
        '"recommendation":{"action":"<what to do>","vs_playbook":"on-plan|departs|no-plan",'
        '"rationale":"<why, from the facts>","grounded_in":["<signal tool(s)>"],'
        '"urgency":"now|soon|monitor","confidence":"high|med|low"}}')
    return sys, seed


def generate(system_prompt: str, seed: str) -> dict | None:
    """Ask Opus for {assessment, recommendation}. None on any trouble."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        sys, user = _prompt(system_prompt, seed)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=config.TEACHER_MAX_TOKENS,
            system=sys,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:  # noqa: BLE001 — best-effort teacher; never crash the pipeline
        print(f"  [opus] skipped: {e}")
        return None

    # Extract the JSON object (Opus may wrap it).
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "recommendation" not in obj:
        return None
    return obj
