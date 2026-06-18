# Orin bring-up — live checkpoint (resume here)

> Scratch/handoff file for the in-progress Phase 9.4 bring-up on the **real unit**. Delete once
> bring-up is done and `SETUP.md`/`README.md` are updated to the 7.2 reality.
> **Last updated 2026-06-18 (end of session 2).** Read this whole file first when resuming.

## Big picture (decided 2026-06-18)
Unit shipped on **JetPack 7.2 / L4T R39.2.0 (CUDA 13.2)**, NOT the JetPack 6.2 / R36.4.x the
`SETUP.md` runbook assumes. Decision: **stay on 7.2**. The model server must expose the boring
**OpenAI `/v1/chat/completions`** contract so the copilot doesn't care what runs underneath — the
runtime is swappable. Model: **`qwen2.5:7b-instruct-q4_K_M`**. Milestone exit test = a coherent,
fully-offline answer at **~20 tok/s (≈5 s / 100 tokens)** over that `/v1` endpoint.

## Hardware reality (confirmed on the unit)
- Orin Nano **8GB**, JetPack **7.2 / L4T R39.2.0** (Jun 1 2026 build).
- **NO onboard storage on this unit** — no SSD, no usable eMMC. Boots from the **microSD** only.
  (An M.2 2280 Key-M NVMe can be added later for faster model loads — optional, not required.)

## ⚠️ Session-1 incident — DO NOT REPEAT (this is the big lesson)
We tried to force MAXN_SUPER via the forum **"super-variant" hack**: `perl` edit of
`/etc/nv_boot_control.conf` → `dpkg-reconfigure nvidia-l4t-bootloader` → `rm /etc/nvpmodel.conf` →
reboot. **That `dpkg-reconfigure` REFLASHES the QSPI bootloader and BRICKED the boot** — continuous
loop (splash → "exiting bootloader" → black → reset). UEFI stayed healthy (Esc → setup worked) but
kernel reset at handoff. **NEVER run that perl/dpkg-reconfigure bootloader hack again.**

**Recovery that worked:** clean reinstall via the **JetPack 7.2 "Jetson ISO" USB installer**
(`jetsoninstaller-r39.2.0-…-arm64.iso`, written to a USB stick with Balena Etcher) → boot Orin →
Esc → Boot Manager → USB → press **Y within 30 s** for the **QSPI capsule update** (this reflashes
correct firmware = un-bricks it) → GRUB "Install Jetson ISO r39.2" → target = **microSD** → install
→ reboot → user setup. Came up clean.

## ✅ Power mode — RESOLVED, no hack needed
After the clean reinstall: `nvpmodel -q` → **`NV power mode: 25W` by default**, and **MAXN SUPER is
now natively available** in the power-mode menu. The capsule update fixed board detection. **Selecting
MAXN SUPER via the nvpmodel GUI dropdown / `sudo nvpmodel -m <id>` is SUPPORTED, runtime, and fully
reversible — it does NOT touch the bootloader. That is totally safe.** Only the perl/dpkg hack bricks.
Currently set to **MAXN SUPER**. Leave it there (free perf once the GPU is actually used).

## ✅ Ollama installed + model pulled
- `curl -fsSL https://ollama.com/install.sh | sh` → installed.
- `ollama pull qwen2.5:7b-instruct-q4_K_M` → done.

## >>> EXACT NEXT STEP (we are HERE — the one remaining blocker) <<<
**⇒ TURNKEY: just run `bash pi/orin/bringup_llm.sh` ON THE ORIN.** It automates the entire
diagnose→fix→pivot→verify flow below (pins MAXN SUPER + clocks → captures Ollama's GPU-init failure
line → tries the cheap `LD_LIBRARY_PATH` cudart override → else builds llama.cpp from source against
CUDA 13.2 and serves `/v1` on :9000 reusing the already-pulled qwen q4 GGUF → tegrastats GR3D peek +
`smoke_api.py` exit test). Idempotent. `--diagnose-only` prints just the failure line; `--llamacpp`
skips straight to the build. **The manual steps below are the same logic, kept for reference / if the
script hits something unexpected.**

**Ollama is running 100% on CPU → ~5 tok/s (4.99 @25W, 5.44 @MAXN SUPER), ~4× under the milestone.**
Confirmed via `ollama ps` → PROCESSOR `100% CPU`; the near-zero gain from the big MAXN-SUPER GPU-clock
bump proves the GPU is idle. **Root cause:** JetPack 7.2 / R39 / CUDA 13.2 is newer than Ollama's
bundled CUDA runtime + Jetson("CudaTegra") detection (covers R35/R36 only) → `Unable to load cudart` /
`no compatible GPUs` → silent CPU fallback. Known open issue (ollama/ollama #9503; dusty-nv
jetson-containers #1661 tracks r39/CUDA-13.2).

**Do this first — get the exact failure line (decides the fix):**
```bash
sudo systemctl stop ollama
OLLAMA_DEBUG=1 ollama serve 2>&1 | grep -iE 'cuda|cudart|compatible|tegra|jetson'   # Ctrl+C after startup lines
# in another terminal: ollama run qwen2.5:7b-instruct-q4_K_M "hi"
```
- **If `Unable to load cudart library`** → GPU is seen, runtime not found. Quick try (keeps Ollama):
  systemd override adding `Environment="LD_LIBRARY_PATH=/usr/local/cuda/lib64"` (find real path:
  `find / -name 'libcudart.so*' 2>/dev/null`), `daemon-reload`, restart, re-check `ollama ps`.
- **If `no compatible GPUs were discovered`** (EXPECTED) → Ollama's Jetson detection doesn't know R39;
  no env var fixes it → **pivot the runtime to `llama.cpp` built from source against CUDA 13.2:**
  ```bash
  sudo apt-get install -y build-essential cmake libcurl4-openssl-dev git
  git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
  cmake -B build -DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=87   # Orin Nano = sm_87
  cmake --build build --config Release -j
  # GGUF: reuse Ollama's blob (~/.ollama/models/blobs/sha256-…) or wget the q4_K_M from HuggingFace
  ./build/bin/llama-server -m <qwen2.5-7b-instruct-q4_k_m.gguf> -ngl 99 --host 0.0.0.0 --port 9000
  ```
  `-ngl 99` offloads all layers to GPU; `llama-server` serves OpenAI `/v1/chat/completions` on :9000
  (the runbook's original copilot port) → milestone contract satisfied. Verify GPU with `tegrastats`
  (GR3D should be busy) and re-benchmark — expect ~20+ tok/s at MAXN SUPER.
- **Last resort only:** reflash to JetPack 6.2 (R36), where Ollama GPU works out-of-box (the original
  `SETUP.md` MLC/Ollama path). A real step back — exhaust the llama.cpp build first.

## Then (remaining bring-up, once GPU inference hits the milestone)
1. Smoke/exit test: point `pi/orin/smoke_api.py` at the live `/v1` (`:11434` if Ollama-GPU works, or
   `:9000` if llama-server) — coherent fully-offline answer at ~5 s / 100 tokens. Watch `tegrastats`.
2. Update `serve.sh` / `bench.sh` / the systemd unit + `SETUP.md`/`README.md`/`models.md` to the actual
   7.2 + (Ollama-or-llama.cpp) reality (the committed docs still describe the JP6.2 + MLC plan), then
   delete this file.
3. Next 9.4 increment (separate): the SR33 copilot service feeding the LLM engine facts + the playbook.
