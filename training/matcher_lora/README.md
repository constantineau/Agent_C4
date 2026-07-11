# Matcher LoRA — training runbook (RunPod)

The GPU-side of `docs/MATCHER_LORA_PLAN.md` §5. Everything before and after the GPU box is in
`pi/orin/copilot/eval/` (corpus generators, §4 eval). Decision 2026-07-10: train PRE-race —
the stock-7B baseline failed the quality gates decisively (F1 0.50 / top-1 0.60 / near-miss FP
26% even hinted). Bounded risk: the LoRA touches only the Tier-2 narrative layer; the
deterministic Tier-1 matcher is untouched, and rollback is one model-name change.

## AS-BUILT (2026-07-10/11 — this runbook was executed; deltas + results)

- **Ran on:** RunPod Secure Cloud 1× A100-SXM4-80GB, official PyTorch template (torch 2.8/cu128,
  transformers 5.13, peft 0.19). PEP-668 image → use `python3 -m venv --system-site-packages`.
  Training: 3,200 examples × 2 epochs = **59 min**, loss 1.3 → ~0.0. Whole pod bill < $10.
- **Corpus:** seed 1001 (3,200 SFT examples, 35% blind-hint slice); eval = the held-out seed-7
  corpus (240 examples, disjoint libraries), never trained on.
- **Artifacts:** merged→GGUF f16→`llama-quantize q4_K_M` (llama.cpp `convert_hf_to_gguf.py`
  needed `pip install gguf mistral-common`; quantize built CPU-only `-DGGML_CUDA=OFF`). Final
  blob sha256 `3fa29c4f…` (4.68 GB). Adapter + train/gguf logs archived on the dev VM:
  `~/Agent_C4-artifacts-matcher-lora-adapter.tgz` (re-derive everything without retraining).
- **Ollama registration:** don't hand-write TEMPLATE — `ollama show --modelfile
  qwen2.5:7b-instruct-q4_K_M | sed "s|^FROM .*|FROM <gguf>|" > Modelfile` inherits the exact
  chatml template/params, then `ollama create qwen2.5-matcher:7b-q4_K_M -f Modelfile`.
- **Results + deploy decision:** table + known `/brief` forgetting regression in
  `docs/MATCHER_LORA_PLAN.md` (Results section). DEPLOYED to the boat Orin 2026-07-11.
- **Orin ops lesson (cost us two freezes):** strictly SERIALIZE heavy operations on the 8 GB box
  — eval, then the 4.7 GB transfer, then `ollama create`; stop `sr33-orin-copilot` (sudo — plain
  systemctl hits polkit) during long eval runs. The eval runner's retry/`--resume` (12be0e4)
  rides through Ollama OOM restarts.

## 1. Corpus (on the dev VM — no GPU)

```bash
cd ~/Agent_C4/pi/orin
python3 -m copilot.eval.gen_train --libraries 400 --per-lib 8 --seed 1001 \
    --blind-frac 0.35 --out /tmp/matcher_train.jsonl        # ~3200 examples
# NEVER seed 7 — that's the held-out eval corpus.
```

## 2. RunPod pod

- Template: any recent PyTorch CUDA image; 1× A100 80GB (or 40GB — batch 2/accum 8). ~$2/hr,
  the run is ~2-4 h.
- `scp matcher_train.jsonl` + `training/matcher_lora/` to the pod.

```bash
pip install -r requirements.txt
python3 train_qlora.py --data matcher_train.jsonl --out ./out
```

## 3. GGUF + quantize (still on the pod)

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp && pip install -r llama.cpp/requirements.txt
python3 llama.cpp/convert_hf_to_gguf.py out/merged --outfile matcher-f16.gguf --outtype f16
cmake -B llama.cpp/build llama.cpp && cmake --build llama.cpp/build -t llama-quantize -j
llama.cpp/build/bin/llama-quantize matcher-f16.gguf matcher-q4_K_M.gguf q4_K_M
```

`scp matcher-q4_K_M.gguf` to the Orin (`agent-c4@100.70.110.72`), then **stop the pod**.

## 4. Deploy on the Orin (A/B — the stock model stays installed)

```bash
cat > Modelfile <<'EOF'
FROM ./matcher-q4_K_M.gguf
TEMPLATE chatml
EOF
ollama create qwen2.5-matcher:7b-q4_K_M -f Modelfile
```

## 5. Gate before it goes near the race (§4)

Same harness, same held-out corpus, blind A/B vs stock — all on the Orin at q4:

```bash
cd ~/eval-dev/pi/orin
LLM_MODEL=qwen2.5-matcher:7b-q4_K_M ~/copilot-venv/bin/python -m copilot.eval.run_eval \
    --corpus ~/matcher_corpus.jsonl --out ~/tuned_hinted.json
LLM_MODEL=qwen2.5-matcher:7b-q4_K_M ~/copilot-venv/bin/python -m copilot.eval.run_eval \
    --corpus ~/matcher_corpus.jsonl --blind --out ~/tuned_blind.json
ENGINE_URL=http://10.10.10.1:8200 LLM_MODEL=qwen2.5-matcher:7b-q4_K_M \
    ~/copilot-venv/bin/python -m copilot.bench_copilot --llm     # regression: must not regress
```

Deploy = set `LLM_MODEL=qwen2.5-matcher:7b-q4_K_M` in `/etc/sr33/copilot.env` + restart
`sr33-orin-copilot`. **Rollback = revert that one line.** Expert audit (~50 sampled outputs read
by sailors) per §4 before race use.
