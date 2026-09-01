---
status: accepted
---

# The onboard LLM never originates strategy

The Tier-2 copilot (Qwen2.5-7B on the Orin) narrates the engine's facts and
condition-matches the live picture against the pre-authored playbook. It does not
invent tactics. Off-book verdicts are the deterministic engine's call; any copilot
output not `grounded_in` a fact from a read-only engine tool is dropped
(product decision 2026-07-06, `docs/PLAYBOOK_V2.md` §7).

The reason is trust under load: a plausible-sounding hallucination delivered to a
navigator at 2 a.m. in building wind is worse than silence, and there is no way to
verify it in the moment. Grounding is enforced structurally — a closed set of
fact-returning tools, engine-computed caveats, deterministic fallback on any LLM
trouble — rather than by prompting the model to behave.

## Consequences

Anything the copilot can say must first exist as a number or a pre-authored narrative
somewhere in Tier 1 or the playbook. Adding copilot capability therefore usually means
adding an engine signal or a play, not improving the prompt. A fine-tuned matcher model
is in use (`qwen2.5-matcher:7b-q4_K_M`) precisely because condition-matching — not
reasoning — is the job.
