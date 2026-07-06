"""Phase-0 pipeline configuration — all env-overridable, sane offline defaults.

Nothing here needs a boat, an engine, an LLM, or an API key: the synthetic snapshot + deterministic
+ rule-perturbed candidate paths are fully offline (that's what makes the pilot runnable anywhere).
The base-model (Ollama) and Opus (Anthropic) candidate paths switch on only when reachable / keyed.
"""
import os

# --- data directory (all artifacts live here; gitignored) ------------------------------------
DATA_DIR = os.environ.get("TRAIN_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
SNAPSHOTS = os.path.join(DATA_DIR, "snapshots.jsonl")
CANDIDATES = os.path.join(DATA_DIR, "candidates.jsonl")
PREF_DB = os.environ.get("TRAIN_PREF_DB", os.path.join(DATA_DIR, "labels.sqlite"))
PAIRS = os.path.join(DATA_DIR, "pref.jsonl")
EVAL_REPORT = os.path.join(DATA_DIR, "eval_report.json")

# --- snapshot generation ---------------------------------------------------------------------
# How many random combination snapshots to add on top of the curated hard-case set.
SYNTH_RANDOM_N = int(os.environ.get("TRAIN_SYNTH_RANDOM_N", "40"))
SYNTH_SEED = int(os.environ.get("TRAIN_SYNTH_SEED", "1739"))   # fixed → reproducible corpus

# --- candidate generation --------------------------------------------------------------------
# Origins to attempt per snapshot. deterministic + perturbed are always available (offline);
# base/opus are best-effort (skipped when unreachable / unkeyed) so the pipeline never blocks.
CAND_ORIGINS = os.environ.get("TRAIN_CAND_ORIGINS", "deterministic,perturbed,base,opus").split(",")
BASE_SAMPLES = int(os.environ.get("TRAIN_BASE_SAMPLES", "2"))   # base-model temp samples
BASE_TEMPERATURE = float(os.environ.get("TRAIN_BASE_TEMP", "0.8"))  # RAISED for diversity to rank
# Teacher (Opus) — mirrors the rest of the repo (summarizer/extract/agent).
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TEACHER_MAX_TOKENS = int(os.environ.get("TRAIN_TEACHER_MAX_TOKENS", "1024"))

# --- labeling app ----------------------------------------------------------------------------
LABEL_PORT = int(os.environ.get("TRAIN_LABEL_PORT", "8400"))
# Bind localhost by default — nginx fronts it (lab.racertracer.net/training/). Never expose :8400
# directly on a shared VM.
LABEL_HOST = os.environ.get("TRAIN_LABEL_HOST", "127.0.0.1")
# Shared team password (like the Lab). Set a real one in any hosted deployment.
LABEL_PASSWORD = os.environ.get("TRAIN_LABEL_PASSWORD", "label-dev")
# Fraction of snapshots double-labeled to measure inter-rater agreement (the pilot gate).
OVERLAP_FRAC = float(os.environ.get("TRAIN_OVERLAP_FRAC", "0.2"))
# Held-out fraction reserved for the expert blind A/B eval — NEVER turned into training pairs.
EVAL_HOLDOUT_FRAC = float(os.environ.get("TRAIN_EVAL_HOLDOUT_FRAC", "0.25"))

# --- signal tools the recommendation may be grounded in (mirrors copilot.strategy_brief) -----
# A candidate recommendation must cite only these, or its grounding is flagged.
SIGNAL_TOOLS = ("get_strategy", "get_selector", "get_tactics", "get_drift", "get_deviation",
                "get_fleet")
