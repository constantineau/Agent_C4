#!/usr/bin/env bash
# Benchmark one model on the Orin via MLC and print prefill/decode tok/s.
# Run ON THE ORIN. Used to A/B Qwen2.5-7B vs faster 3-4B models (SETUP.md §5-6).
#
#   bash pi/orin/bench.sh                                  # default: Qwen2.5-7B
#   bash pi/orin/bench.sh meta-llama/Llama-3.2-3B-Instruct # A/B a smaller model
#
# Notes:
#  - MLC's own benchmark prints e.g. "prefill_rate 632.8 tokens/sec, decode_rate 46.9 tokens/sec".
#  - Decode rate is the number that matters for answer latency on the boat.
#  - Re-run `sudo jetson_clocks` first or your numbers will be low (clocks don't persist).
#  - First run downloads + compiles the model (slow); subsequent runs are cached.
set -euo pipefail

MODEL="${1:-Qwen/Qwen2.5-7B-Instruct}"    # source HF id; MLC quantizes to q4f16_ft on first build
HF_TOKEN="${HUGGINGFACE_TOKEN:-}"          # needed for gated repos (e.g. meta-llama/*)

command -v jetson-containers >/dev/null || {
  echo "jetson-containers not on PATH (SETUP.md §4)" >&2; exit 1; }

echo ">> Power mode (want MAXN_SUPER):"
sudo nvpmodel -q || true
echo ">> Pinning clocks for a steady measurement…"
sudo jetson_clocks || true

echo ">> Benchmarking $MODEL via MLC (INT4 / q4f16_ft)…"
# dusty-nv's MLC package ships benchmark.sh inside the container; jetson-containers' top-level
# sweep is packages/llm/mlc/benchmarks.sh. We invoke the single-model form:
jetson-containers run \
  ${HF_TOKEN:+-e HUGGINGFACE_TOKEN=$HF_TOKEN} \
  "$(autotag mlc)" \
  bash -lc "cd /opt/mlc-llm 2>/dev/null || cd /opt/mlc; \
            ./benchmark.sh '$MODEL' || \
            echo '⚠️  benchmark.sh path/name differs in this image — run \`ls\` in the MLC container and adjust (SETUP.md §5).'"

echo ">> Done. Record prefill_rate / decode_rate in pi/orin/models.md."
