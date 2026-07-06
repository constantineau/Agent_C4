# Onboard LLM copilot — Orin Nano (Tier 2, Phase 9.4)

The **optional onboard conversational LLM** of the three-tier architecture (see
`docs/RRS41_COMPLIANCE.md` and `docs/ONBOARD_ENGINE_SCOPING.md` §3). A **Jetson Orin Nano 8GB
(Super)** dedicated to LLM inference, separate from the Pi 4 that runs the deterministic engine
(Tier 1, `pi/engine`). They talk over boat-local Wi-Fi.

> **Status: runtime up + decision-support layer built.** The runtime/model bring-up shipped (the
> Orin is a turnkey headless appliance — **Ollama serving `qwen2.5:7b-instruct-q4_K_M` on `:11434`**,
> from-source `cuda_v13`@sm_87 GPU build, ~12 tok/s, reboot-verified). The **SR33 copilot service**
> now exists in **[`copilot/`](copilot/)** — its first increment, the **decision-support /
> tool-calling layer** (bounded read-only engine tools + grounding guardrails + deterministic
> fallback). Crew-facing narration is the next increment. See `copilot/README.md`.
>
> **Endpoint note:** the runtime files below (`SETUP.md`/`serve.sh`/`smoke_api.py`) still describe the
> originally-planned **MLC on `:9000`**; the unit actually runs **Ollama on `:11434`** (same OpenAI
> `/v1` contract — see the Orin bring-up notes). The copilot defaults to `:11434`.

## Why a separate box, and why it's legal in-race

Locked decisions: the **Pi 4 owns the deterministic engine** (routing/tactics/sails/nav/fatigue —
plain physics, no LLM); the **Orin owns the LLM**. Under RRS 41 the boat's own computer reasoning over
its own sensors + pre-loaded homework + common public data is *not* "outside help" — so the copilot is
legal while racing **as long as it never phones the cloud mid-race** (that off-boat round-trip is the
real RRS-41 line). It does NOT originate strategy (a product choice locked 2026-07-06, not a legal
limit — see `docs/PLAYBOOK_V2.md` §7): it interprets the engine's numbers and MATCHES the live
situation against the pre-loaded playbook's plays. The engine does the math; the pre-race frontier
playbook is the strategy library it points into.

## The inference-API contract

The copilot service (next increment) will be a thin client of a **stable, boring contract**: the
**OpenAI `/v1/chat/completions`** endpoint that MLC serves locally. Keeping the model server behind
that contract means we can swap models (Qwen2.5-7B ↔ a 3-4B) or runtimes without touching the copilot.

```
  Pi 4 (Tier 1)                 Orin Nano (Tier 2)
  engine :8200  ──facts──►  copilot svc ──/v1/chat/completions──►  MLC server :9000
  (deterministic)            (next increment)                       (Qwen2.5-7B INT4, this milestone)
       ▲                                                                   │
       └──────────────── iPad over boat-local Wi-Fi ◄─────────────────────┘
```

## Files here

| File | What it does | Run on |
|---|---|---|
| `SETUP.md` | The bring-up runbook: flash JetPack 6.2 → Super mode → jetson-containers → MLC → benchmark → serve → autostart | the Orin |
| `serve.sh` | Launch the OpenAI-compatible MLC server (`MODEL`/`PORT` vars); idempotent restart | the Orin |
| `bench.sh` | Benchmark one model's prefill/decode tok/s via MLC (A/B the 7B vs a 3-4B) | the Orin |
| `smoke_api.py` | Hit the `/v1` endpoint with a "narrate these facts" prompt; print answer + latency + tok/s; pass/fail. **Exit test for this milestone.** Pure stdlib. | the Orin or any LAN host |
| `models.md` | A/B matrix (NVIDIA numbers + a column for your measured results) + how to confirm the exact MLC model id | reference |
| `../systemd/sr33-orin-llm.service` | Appliance autostart (re-applies Super mode + clocks, then `serve.sh`) | the Orin |

## Runtime choice (decided)

**MLC + INT4 (`q4f16_ft`) via jetson-containers** — the path behind NVIDIA's own numbers (Qwen2.5-7B
= 21.8 tok/s on Super). **Not** llama.cpp/Ollama for the 7B: a CUDA memory-allocator regression in the
JetPack R36.4.x line broke >1B models under llama.cpp. (Ollama is fine for a quick 3B A/B only.)

## Quick start (on a flashed Orin in Super mode)

```bash
git clone https://github.com/dusty-nv/jetson-containers && bash jetson-containers/install.sh
bash pi/orin/bench.sh                       # benchmark Qwen2.5-7B (record in models.md)
MODEL=<confirmed-MLC-id> bash pi/orin/serve.sh
python3 pi/orin/smoke_api.py --base-url http://localhost:9000/v1
```

See `SETUP.md` for the full flashing + firmware + cooling + autostart steps. Nothing here has been run
on real hardware yet — commands are marked ✅ (confirmed against NVIDIA/dusty-nv docs) or ⚠️ (confirm
the exact id/flag on the live unit).
