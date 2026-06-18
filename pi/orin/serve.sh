#!/usr/bin/env bash
# Launch the onboard LLM as an OpenAI-compatible API server on the Jetson Orin Nano (Tier 2, 9.4).
# Wraps `jetson-containers run ... sudonim serve ...` so the boat only edits MODEL/PORT.
# Run ON THE ORIN (not the Pi, not the dev VM). See pi/orin/SETUP.md §7.
set -euo pipefail

# ── config (override via env or /etc/sr33/orin.env) ───────────────────────────
# MODEL: the MLC-quantized model id. CONFIRM the verbatim string on-unit (SETUP.md §5 / models.md).
# The dusty-nv pre-quantized builds are q4f16_ft MLC repos on HuggingFace.
MODEL="${MODEL:-dusty-nv/Qwen2.5-7B-Instruct-q4f16_ft-MLC}"   # ⚠️ confirm on-unit before trusting
QUANT="${QUANT:-q4f16_ft}"
PORT="${PORT:-9000}"
NAME="${NAME:-sr33-orin-llm}"
EXTRA_ARGS="${EXTRA_ARGS:-}"   # e.g. --max-batch-size 1, context flags — see `sudonim serve --help`

command -v jetson-containers >/dev/null || {
  echo "jetson-containers not on PATH — run bash jetson-containers/install.sh (SETUP.md §4)" >&2
  exit 1
}

echo ">> serving $MODEL ($QUANT) on :$PORT  [container $NAME]"
echo ">> OpenAI-compatible endpoint will be http://0.0.0.0:$PORT/v1"

# Stop a previous instance if present (idempotent restart).
docker rm -f "$NAME" >/dev/null 2>&1 || true

# `--name`/`-d`/`-p` are docker flags consumed by `jetson-containers run`; everything after the
# autotag image is the command run inside the MLC container.
exec jetson-containers run -d --name "$NAME" --restart unless-stopped \
  -p "${PORT}:${PORT}" \
  "$(autotag mlc)" \
  sudonim serve \
    --model "$MODEL" \
    --quantization "$QUANT" \
    --host 0.0.0.0 --port "$PORT" \
    ${EXTRA_ARGS}
