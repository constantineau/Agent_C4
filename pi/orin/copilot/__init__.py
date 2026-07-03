"""SR33 onboard LLM copilot (Tier 2, Phase 9.4) — the decision-support layer.

A thin client that turns the deterministic engine's facts (+ the frozen playbook) into bounded,
grounded decision support via the local LLM. Runs ON THE ORIN, talks to the Pi engine over
boat-local Wi-Fi and to the LLM over localhost. RRS-41-safe: never phones the cloud (the real line).
Onboard it may originate strategy; the engine does the math and it grounds every claim in the engine
facts + the playbook — reliability discipline for a 7B, not RRS-41 limits.
"""
