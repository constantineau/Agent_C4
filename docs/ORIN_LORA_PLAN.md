# Orin LLM LoRA — fine-tuning plan (pilot)

> **SUPERSEDED 2026-07-06 by `docs/MATCHER_LORA_PLAN.md`** — the reliability objective below
> (JSON / tool-loop) is absorbed into the condition-matcher training task, whose eval gates it
> directly. Kept for the mechanics it validated (QLoRA → merge → GGUF q4 → `ollama create`).

Fine-tune the onboard copilot LLM (`qwen2.5:7b-instruct-q4_K_M`, Ollama on the Orin Nano) to make the
**bounded tool-loop brief** reliable. Decisions locked 2026-06-29:

| Decision | Choice |
|---|---|
| **Objective** | Brief **JSON reliability** + **tool-calling reliability** (not phrasing) |
| **Train hardware** | Rent a cloud GPU (1× A100-40G / L4 / 4090), QLoRA r=16–32 |
| **Teacher / data** | Distill from **Opus**, every target filtered through our grounding validator |
| **Scope** | **Focused pilot** — one task, ~200–500 examples, full loop incl. q4 deploy + A/B on the Orin |

## Why this pilot
The copilot LLM narrates the engine's reads and matches conditions against the playbook (it does NOT
originate strategy — descope 2026-07-06, docs/PLAYBOOK_V2.md §7); THIS pilot targets its
**phrasing/structuring reliability** — the engine does all math and the safety is
*structural* (`brief.validate()` grounding + deterministic fallback). The path that actually fails today
is the bounded tool loop: the 7B sometimes returns unparseable JSON, ungrounded factors, or doesn't stop
calling tools → it falls back to the deterministic brief. That failure is **measurable** (parse / schema /
grounding / loop-termination rates) and improving it proves the *whole* pipeline — including the hardest
part, surviving q4 quantization on real hardware. We do **not** touch the guardrails: LoRA lowers the
fallback *rate*, it never replaces the safety net.

Non-goals: speed (still ~12 tok/s, bandwidth-bound), play-matching quality (a later training pass per
docs/PLAYBOOK_V2.md §7 — this pilot targets reliability first), coach-line phrasing style (a later pass).

## Pipeline (end to end)
```
snapshots (archive + synthetic) ──► Opus teacher ──► validate() filter ──► Qwen tool-use chat format
   ──► QLoRA on rented GPU (base Qwen2.5-7B-Instruct) ──► merge ──► GGUF q4_K_M ──► ollama create on Orin
   ──► EVAL AT q4 (base vs LoRA): parse / schema / grounding / termination / latency
```
Train distribution **must equal** inference distribution: build every example from the EXACT copilot
`_system_prompt` + facts digest + tool schema (`pi/orin/copilot/copilot.py`, `tools.py`).

## 1. Data — Opus distillation, validator-filtered
Target a clean, controllable trajectory rather than raw teacher trajectory capture.

1. **Input snapshots** (the `gather()`-shaped facts + the crew question the brief sees):
   - *Real*: engine snapshots from the Phase-2 full-res archive / bench replays (realistic distributions).
   - *Synthetic*: a generator that builds diverse snapshots across the space — conditions/navigator/
     tactics/sail/fatigue/ais/fleet, playbook present|absent, degraded|stale|missing-tools, and the
     adversarial cases (no own fix, conflicting sources). Coverage is the point.
   - Pilot size ≈ 300–500 snapshots.
2. **Gold targets (teacher = Opus, the same model the Lab briefings use):** Opus authors the high-quality
   grounded final JSON (`situation/factors/recommendations/caveats/confidence`, each with correct
   `grounded_in`). The surrounding **tool-call turns are synthesized deterministically** from the snapshot
   (we already know which tools the facts came from) — so we teach the *exact* loop we want (optionally
   call this tool, then STOP and emit THIS valid grounded JSON) without inheriting Opus's tool-calling
   quirks. Most examples are "facts already gathered → emit JSON immediately" (clean termination); a
   minority insert one extra call (e.g. `get_forecast`) to teach call-then-stop.
3. **Validator filter:** every target JSON runs through `brief.validate()`. Only targets that PASS (every
   factor/rec grounded in a tool actually present, disclaimer present, schema valid) enter the set →
   training can only *reinforce* grounding, never loosen it.
4. **Format & split:** Qwen2.5 ChatML tool-use format; loss **masked to assistant turns only** (tool calls
   + final JSON), system/facts/tool-results masked. 80/20 train/eval, eval weighted toward hard cases.

Artifacts: `dataset.train.jsonl`, `dataset.eval.jsonl`.

## 2. Training — QLoRA on a rented GPU
- **Base:** `Qwen/Qwen2.5-7B-Instruct` (bf16) — same lineage as the deployed q4.
- **Method:** QLoRA (4-bit NF4 base + LoRA), **Unsloth** on 1× A100-40G or L4 (~16–24 GB, a few hours).
- **Start hyperparams:** r=16–32, α=2r, dropout 0.05; target = q,k,v,o,gate,up,down; lr ~2e-4 cosine;
  2–3 epochs; seq len covering the full prompt+tools (~2–4k). Modest rank — the pilot set is small.
- **Output:** LoRA adapter + merged fp16.

## 3. Deploy to the Orin — the make-or-break step
1. Merge LoRA → fp16 safetensors.
2. `convert_hf_to_gguf.py` → GGUF, then quantize **q4_K_M** (match the deployed format/memory budget).
3. `ollama create c4-copilot-7b -f Modelfile` (FROM the gguf; **same chat template + tool format** —
   verify byte-for-byte, this is where tool-use models break). Point `LLM_MODEL` at it; restart
   `sr33-orin-copilot.service`.
4. **Evaluate AT q4 on the Orin** — quantization can erode tool-format adherence; fp16 numbers don't
   count. If q4 regresses, test q5_K_M (may still fit 8 GB) before widening scope.

## 4. Evaluation (base vs LoRA, BOTH at q4)
Extend `bench_copilot --llm` + `eval_dashboard.py` into a scored harness over `dataset.eval.jsonl`:
- **Parse rate** — parseable JSON returned.
- **Schema-valid rate** — conforms to the brief schema.
- **Grounding pass rate** — every factor/rec grounded (`validate()`).
- **Loop termination** — stops cleanly; tool-calls well-formed; no runaway.
- **Fallback rate** — % that fell back to deterministic (should drop).
- **Latency** — tok/s + end-to-end (must be ≈ unchanged).
- *(secondary)* **Faithfulness** — Opus-as-judge LoRA vs base (reliability is the goal, so secondary).

**Pilot success bar:** measure the **baseline first** (base Qwen q4 on the Orin over the eval set), then
require the LoRA to raise grounded-valid-JSON success by a meaningful margin (set the threshold from the
baseline) with no latency regression and guardrails intact.

## 5. Risks & mitigations
- *q4 erodes gains* → eval at q4, not fp16; fall back to q5_K_M if needed.
- *Small set over-fits* → modest rank + dropout + held-out eval + early stop.
- *Train≠inference format* → reuse the exact copilot prompt/tool schema/digest to build examples.
- *Teacher leaks ungrounded content* → validator filter on every target.
- *Forgetting general ability* → LoRA (not full FT), few epochs, spot-check general instructions.

## 6. Proposed repo layout (`pi/orin/training/`)
```
gen_snapshots.py    synthetic + archive-derived gather()-shaped snapshots
gen_targets.py      Opus teacher → grounded JSON, validate()-filtered → JSONL
format_dataset.py   → Qwen2.5 tool-use chat format + train/eval split
train_qlora.py      Unsloth QLoRA (runs on the rented GPU)
to_gguf.sh          merge → convert_hf_to_gguf → quantize q4_K_M
Modelfile           Ollama model definition (chat template + params)
eval_brief.py       scored eval (parse/schema/ground/term/latency), base vs LoRA at q4
README.md           the runbook
```

## 7. Phases
- **Phase 0 (NO GPU — can start now):** build `gen_snapshots` + `gen_targets` (needs `ANTHROPIC_API_KEY`)
  + `format_dataset` + `eval_brief`; generate the dataset; **measure the q4 baseline on the Orin** → sets
  the success bar.
- **Phase 1 (GPU):** rent the box, run QLoRA → adapter + merged fp16.
- **Phase 2 (Orin):** GGUF q4 + `ollama create` + eval at q4 + A/B vs baseline.
- **Phase 3:** iterate (coverage/hyperparams) or scale to the multi-task adapter (brief + narrate +
  dashboard).

RRS 41: all of this is **pre-race homework** — and it changes nothing about compliance. Onboard, the
model is the boat's own gear (legal in-race); it narrates + condition-matches only (origination is
descoped by product choice, not by rule — docs/PLAYBOOK_V2.md §7). The pilot just makes it more
reliable, and grounding survives as reliability discipline, not an RRS-41 limit.
