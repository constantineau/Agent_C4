# Orin model A/B matrix (Phase 9.4)

> **SUPERSEDED (historical record).** This matrix belongs to the MLC bring-up plan; the unit
> runs **Ollama + `qwen2.5:7b-instruct-q4_K_M`** at a measured **~12 tok/s** (bandwidth-bound;
> quality kept over speed) — see `DEPLOYMENT.md`. The A/B was never run; kept for the research.


Candidate local models for the onboard copilot, with NVIDIA's published Super-mode numbers and a
column to fill in with **measured** results from `bench.sh` on your unit. Runtime = MLC + INT4
(`q4f16_ft`) via jetson-containers (the path behind NVIDIA's numbers; not llama.cpp/Ollama for 7-8B
— see SETUP.md "Known issues").

| Model | Params | NVIDIA decode tok/s (Super) | Mem | Measured decode tok/s | Notes |
|---|---|---|---|---|---|
| **Qwen2.5-7B-Instruct** | 7B | **21.8** | ~4.8 GB | _TODO_ | **primary** — best capability + function-calling at 8GB |
| Llama-3.1-8B-Instruct | 8B | 19.1 | ~4.8 GB | _TODO_ | strong alternative |
| Phi-3.5-mini-instruct | 3.8B | 38.1 | ~2.3 GB | _TODO_ | fast, headroom |
| Llama-3.2-3B-Instruct | 3B | 43.1 | ~2 GB | _TODO_ | fastest; latency-floor fallback |
| Gemma-2-9B | 9B | 9.2 | ~5.5 GB | _TODO_ | too slow — practical ceiling |

**Decision rule (this milestone):** ship the **7B** unless a ~100-token answer exceeds **~6 s** under
sustained thermal load, then fall back to a 3-4B for the latency-critical path. Capability matters
because the copilot must reason over engine facts + the playbook (decision support, not just chat).

## Confirming the exact MLC model id on-unit

The pre-quantized MLC builds (`*-q4f16_ft-MLC`) are hosted on HuggingFace under `dusty-nv`. The
verbatim repo string can change across jetson-containers releases, so **don't trust the placeholder
in `serve.sh`** — confirm it on the live unit:

```bash
# inside the MLC container, list what sudonim/MLC knows, or search HF:
jetson-containers run $(autotag mlc) sudonim serve --help     # see the --model expectations
# the jetson-ai-lab model table is the canonical list:
#   https://www.jetson-ai-lab.com/  (Models → MLC → Orin Nano)
```

For `bench.sh` you can pass the **source** HF id (e.g. `Qwen/Qwen2.5-7B-Instruct`) and MLC quantizes
on first build; for `serve.sh` prefer the **pre-quantized** MLC id so it doesn't recompile each boot.
Gated repos (`meta-llama/*`) need `HUGGINGFACE_TOKEN` set.
