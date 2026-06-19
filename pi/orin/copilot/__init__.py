"""SR33 onboard LLM copilot (Tier 2, Phase 9.4) — the decision-support layer.

A thin client that turns the deterministic engine's facts (+ the frozen playbook) into bounded,
grounded decision support via the local LLM. Runs ON THE ORIN, talks to the Pi engine over
boat-local Wi-Fi and to the LLM over localhost. RRS-41-safe: never phones the cloud, never does
the math, never invents strategy outside the engine facts + the playbook.
"""
