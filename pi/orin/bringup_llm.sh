#!/usr/bin/env bash
# ── Turnkey GPU-inference bring-up for the onboard LLM (Phase 9.4, Tier 2) ────────────────────────
# RUN ON THE ORIN (aarch64, JetPack 7.2 / L4T R39.2.0 / CUDA 13.2). Do NOT run on the Pi or the VM.
#
# Why this exists: on JP7.2/R39 the stock Ollama install runs the 7B 100% on CPU (~5 tok/s, ~4x under
# the ~20 tok/s milestone) because its bundled CUDA runtime + Jetson detection only know R35/R36 →
# silent CPU fallback (ollama/ollama#9503, dusty-nv/jetson-containers#1661). This script automates the
# exact diagnose→fix→pivot flow from pi/orin/BRINGUP_STATE.md:
#   1. confirm we're on the Orin + pin MAXN SUPER + clocks
#   2. capture Ollama's real GPU-init failure line (OLLAMA_DEBUG)
#   3. if "Unable to load cudart" → try the cheap LD_LIBRARY_PATH systemd override, re-test GPU
#   4. else (or if that didn't move it to GPU) → build llama.cpp from source against CUDA 13.2 and
#      serve the SAME qwen q4 GGUF over OpenAI /v1 on :9000 (the milestone contract)
#   5. verify with smoke_api.py (coherent offline answer + effective tok/s) and a tegrastats GR3D peek
#
# Idempotent: re-running re-uses an existing llama.cpp checkout/build and the already-pulled GGUF.
#
#   bash pi/orin/bringup_llm.sh                  # full auto: diagnose → fix → pivot → verify
#   bash pi/orin/bringup_llm.sh --diagnose-only  # just print Ollama's GPU-init failure line + exit
#   bash pi/orin/bringup_llm.sh --llamacpp       # skip Ollama, go straight to the llama.cpp build
#   MODEL_TAG=... PORT=... CUDA_ARCH=... GGUF=... bash pi/orin/bringup_llm.sh
set -euo pipefail

# ── config (env-overridable) ─────────────────────────────────────────────────────────────────────
MODEL_TAG="${MODEL_TAG:-qwen2.5:7b-instruct-q4_K_M}"   # the Ollama tag we pulled; also the alias
PORT="${PORT:-9000}"                                   # OpenAI /v1 port (matches the copilot contract)
CUDA_ARCH="${CUDA_ARCH:-87}"                            # Orin Nano = sm_87
CTX="${CTX:-4096}"                                      # context window (fits 7B-q4 in 8GB)
LLAMA_DIR="${LLAMA_DIR:-$HOME/llama.cpp}"
GGUF="${GGUF:-}"                                        # explicit GGUF path; else reuse Ollama's blob
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="auto"
case "${1:-}" in
  --diagnose-only) MODE="diagnose" ;;
  --llamacpp)      MODE="llamacpp" ;;
  --help|-h) grep -E '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
esac

c() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }   # section header
ok() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# ── 0. guard: must be the Orin ─────────────────────────────────────────────────────────────────────
c "0. environment check"
if [ "$(uname -m)" != "aarch64" ] || ! command -v tegrastats >/dev/null 2>&1; then
  echo "This must run ON THE ORIN (aarch64 + tegrastats). Detected: $(uname -m) on $(hostname)." >&2
  echo "The dev VM can't reach the GPU — clone the repo on the Orin and run it there." >&2
  exit 1
fi
ok "on the Orin: $(uname -m), $(uname -r)"

# ── 1. power: MAXN SUPER + clocks (safe, runtime, reversible — NOT the bricking bootloader hack) ───
c "1. power mode → MAXN SUPER + pinned clocks"
sudo nvpmodel -m 2 >/dev/null 2>&1 || warn "nvpmodel -m 2 failed (already set / different id?) — check 'sudo nvpmodel -q'"
sudo jetson_clocks >/dev/null 2>&1 || warn "jetson_clocks failed (non-fatal)"
sudo nvpmodel -q 2>/dev/null | sed 's/^/   /' || true

# ── 2. locate CUDA toolkit (needed for the llama.cpp build + the cudart override) ──────────────────
NVCC="$CUDA_HOME/bin/nvcc"; [ -x "$NVCC" ] || NVCC="$(command -v nvcc || true)"
CUDART="$(find /usr -name 'libcudart.so*' 2>/dev/null | head -1 || true)"
[ -n "$CUDART" ] && CUDART_DIR="$(dirname "$CUDART")" || CUDART_DIR="$CUDA_HOME/lib64"

# ── helper: is Ollama actually running the model on the GPU? (PROCESSOR column in `ollama ps`) ─────
ollama_on_gpu() {
  ollama run "$MODEL_TAG" "hi" >/dev/null 2>&1 || true   # ensure it's loaded
  local ps; ps="$(ollama ps 2>/dev/null || true)"
  printf '%s\n' "$ps" | sed 's/^/   ollama ps: /'
  # data row shows e.g. "100% CPU" or "100% GPU" or a split — succeed only if GPU appears
  printf '%s\n' "$ps" | tail -n +2 | grep -qi 'gpu'
}

# ── 2b. capture Ollama's GPU-init failure line ─────────────────────────────────────────────────────
BRANCH="llamacpp"
if [ "$MODE" != "llamacpp" ] && command -v ollama >/dev/null 2>&1; then
  c "2. diagnose Ollama GPU init (OLLAMA_DEBUG)"
  sudo systemctl stop ollama >/dev/null 2>&1 || true
  pkill -f 'ollama serve' >/dev/null 2>&1 || true
  : > /tmp/ollama_debug.log
  OLLAMA_DEBUG=1 nohup ollama serve >/tmp/ollama_debug.log 2>&1 &
  OLLAMA_PID=$!
  sleep 6
  kill "$OLLAMA_PID" >/dev/null 2>&1 || true
  echo "   --- relevant lines from /tmp/ollama_debug.log ---"
  grep -iE 'cuda|cudart|compatible|tegra|jetson|library|gpu' /tmp/ollama_debug.log | tail -25 | sed 's/^/   /' \
    || warn "no GPU-related lines found — inspect /tmp/ollama_debug.log by hand"

  if grep -qi 'unable to load cudart' /tmp/ollama_debug.log; then
    BRANCH="cudart"; warn "diagnosis: cudart not found → try the LD_LIBRARY_PATH override"
  elif grep -qi 'no compatible gpus' /tmp/ollama_debug.log; then
    BRANCH="llamacpp"; warn "diagnosis: 'no compatible GPUs' (R39 unknown to Ollama) → llama.cpp pivot"
  else
    BRANCH="cudart"; warn "diagnosis: inconclusive — try the cheap override first, fall back to llama.cpp"
  fi
fi
[ "$MODE" = "diagnose" ] && { ok "diagnose-only: branch would be '$BRANCH'. Stopping."; exit 0; }

# ── 3. cheap fix: point Ollama's loader at the system cudart, restart, re-test GPU ─────────────────
if [ "$BRANCH" = "cudart" ] && command -v ollama >/dev/null 2>&1; then
  c "3. cudart fix: systemd LD_LIBRARY_PATH override"
  if [ -z "$CUDART" ]; then
    warn "no libcudart.so found under /usr — skipping override, going to llama.cpp"
  else
    ok "found cudart: $CUDART"
    sudo mkdir -p /etc/systemd/system/ollama.service.d
    printf '[Service]\nEnvironment="LD_LIBRARY_PATH=%s:%s/targets/aarch64-linux/lib"\n' \
      "$CUDART_DIR" "$CUDA_HOME" | sudo tee /etc/systemd/system/ollama.service.d/cuda.conf >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl restart ollama
    sleep 4
    if ollama_on_gpu; then
      c "✅ MILESTONE PATH: Ollama is now on the GPU"
      ok "Ollama serving OpenAI /v1 on :11434 (GPU)."
      python3 "$HERE/smoke_api.py" --base-url "http://localhost:11434/v1" --model "$MODEL_TAG" || \
        warn "smoke test non-zero — inspect output above"
      echo
      ok "DONE via Ollama-GPU. Endpoint: http://localhost:11434/v1  (note: NOT :$PORT)."
      echo "   Next: update serve.sh/systemd to use Ollama on :11434, run bench, then update the docs."
      exit 0
    fi
    warn "still on CPU after the override → pivoting to llama.cpp"
  fi
fi

# ── 4. pivot: build llama.cpp from source against CUDA 13.2 and serve /v1 on :PORT ─────────────────
c "4. build llama.cpp (CUDA on, sm_$CUDA_ARCH)"
[ -n "$NVCC" ] || { echo "nvcc not found (looked in $CUDA_HOME/bin + PATH). Install the CUDA toolkit." >&2; exit 1; }
ok "nvcc: $NVCC"
export PATH="$CUDA_HOME/bin:$PATH"

sudo apt-get update -y >/dev/null
sudo apt-get install -y build-essential cmake libcurl4-openssl-dev git >/dev/null
ok "build deps present"

if [ -d "$LLAMA_DIR/.git" ]; then
  ok "re-using $LLAMA_DIR"; git -C "$LLAMA_DIR" pull --ff-only >/dev/null 2>&1 || warn "git pull skipped"
else
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
fi
cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH"
cmake --build "$LLAMA_DIR/build" --config Release -j"$(nproc)" --target llama-server
SERVER_BIN="$LLAMA_DIR/build/bin/llama-server"
[ -x "$SERVER_BIN" ] || { echo "build produced no llama-server at $SERVER_BIN" >&2; exit 1; }
ok "built $SERVER_BIN"

# ── 4b. locate the GGUF (reuse Ollama's already-pulled blob = no re-download) ──────────────────────
c "4b. locate the qwen q4 GGUF"
if [ -z "$GGUF" ] && command -v ollama >/dev/null 2>&1; then
  GGUF="$(ollama show --modelfile "$MODEL_TAG" 2>/dev/null | awk '/^FROM /{print $2; exit}')"
fi
if [ -z "$GGUF" ] || [ ! -f "$GGUF" ]; then
  echo "Couldn't resolve a GGUF for $MODEL_TAG." >&2
  echo "Either: \`ollama pull $MODEL_TAG\` first (then re-run), or pass GGUF=/path/to/qwen2.5-7b-instruct-q4_k_m.gguf" >&2
  echo "(HF: bartowski/Qwen2.5-7B-Instruct-GGUF → Qwen2.5-7B-Instruct-Q4_K_M.gguf)" >&2
  exit 1
fi
ok "GGUF: $GGUF ($(du -h "$GGUF" | cut -f1))"

# Free the GPU/RAM that the CPU-bound Ollama would otherwise hold.
sudo systemctl stop ollama >/dev/null 2>&1 || true

# ── 4c. serve ─────────────────────────────────────────────────────────────────────────────────────
c "4c. serve llama-server on :$PORT (all layers on GPU)"
pkill -f 'llama-server' >/dev/null 2>&1 || true
nohup "$SERVER_BIN" -m "$GGUF" -ngl 99 -c "$CTX" --host 0.0.0.0 --port "$PORT" \
  --alias "$MODEL_TAG" > /tmp/llama-server.log 2>&1 &
ok "launched (logs: /tmp/llama-server.log)"
printf "   waiting for /health"
for _ in $(seq 1 90); do
  if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then echo " ready"; break; fi
  printf "."; sleep 2
done
curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1 || {
  echo; echo "server didn't come up — tail of /tmp/llama-server.log:" >&2; tail -30 /tmp/llama-server.log >&2; exit 1; }

# ── 5. verify: tegrastats GR3D peek + the smoke/exit test ──────────────────────────────────────────
c "5. verify GPU is busy + run the smoke/exit test"
( tegrastats --interval 500 & TS=$!; sleep 1; \
  curl -s "http://localhost:$PORT/v1/chat/completions" -H 'Content-Type: application/json' \
    -d '{"model":"'"$MODEL_TAG"'","messages":[{"role":"user","content":"Say hello in one short sentence."}],"max_tokens":40}' >/dev/null; \
  sleep 1; kill $TS 2>/dev/null ) 2>/dev/null | grep -m1 -o 'GR3D_FREQ[^ ]*' | sed 's/^/   GPU load sample: /' \
  || warn "couldn't sample tegrastats GR3D (non-fatal) — eyeball 'tegrastats' during a request"

python3 "$HERE/smoke_api.py" --base-url "http://localhost:$PORT/v1" --model "$MODEL_TAG" || \
  warn "smoke test returned non-zero — review the answer/tok-s above"

c "DONE"
ok "llama-server live on http://0.0.0.0:$PORT/v1  (model alias: $MODEL_TAG)"
cat <<EOF
   Next steps (separate, not done here):
     1. If tok/s ≥ ~20 at MAXN SUPER, the runtime-bring-up MILESTONE is met.
     2. Persist it: add a systemd unit that runs this serve line on boot (adapt
        pi/systemd/sr33-orin-llm.service from the MLC wrapper to llama-server) so it autostarts.
     3. Update SETUP.md / serve.sh / bench.sh / models.md to the 7.2 + llama.cpp reality and
        delete pi/orin/BRINGUP_STATE.md.
     4. Then the next 9.4 increment: the SR33 copilot service feeding engine facts + the playbook.
EOF
