"""Strategy/tactics LoRA — Phase-0 data + labeling pipeline (docs/STRATEGY_LORA_PLAN.md).

Track B of the onboard-LLM fine-tune: improve the copilot's tactical JUDGMENT + calibration by
preference-tuning on expert-sailor-RANKED candidate briefs. This package is the Phase-0 flywheel —
snapshots → candidates → labeling app → preference pairs — plus the eval/engine-audit scaffold. It
does NOT train (that's Phase 2, on a rented GPU) and it never touches the deployed copilot.

Run everything as modules from `pi/orin/` (the copilot is a sibling package), e.g.:
    python3 -m training.gen_snapshots
    python3 -m training.gen_candidates
    python3 -m training.smoke
so `from copilot.llm import LLMClient` resolves the same way `copilot.bench_copilot` does.
"""
__all__ = ["schema", "config"]
