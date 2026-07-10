# Matcher LoRA — fine-tuning the Orin 7B for playbook CONDITION MATCHING

**Status:** step 0 IN PROGRESS (2026-07-10). The eval harness is BUILT — `pi/orin/copilot/eval/`
(§3 generators + §4 metrics/gates + a runner that drives the real production prompt path), exit
test `python3 -m copilot.eval.test_eval` green incl. an oracle lock-step vs `app.matcher`.
Baseline of the stock q4 7B on the real Orin: quality gates FAIL decisively even hinted (armed-set
F1 0.45, top-1 0.55, near-miss FP 30%, n=141 infra-clean; JSON reliability 100% passes) → the
training gate is OPEN. **Decision (user, 2026-07-10): train PRE-race (Jul 18)** — bounded risk
(Tier-1 untouched; rollback = one model-name line). GPU side: `training/matcher_lora/`
(RunPod runbook + QLoRA script); corpus: `python3 -m copilot.eval.gen_train` (seed 1001,
3200 examples; seed 7 = held-out eval, never trained).
(History: replaces the removed judgment/DPO plan — `STRATEGY_LORA_PLAN.md`, deleted; it trained
the 7B to *make calls*, which is descoped — and **absorbs Track A** (`ORIN_LORA_PLAN.md`,
reliability SFT). Depended on Playbook v2 Phases B + D, which shipped 2026-07-08.)

## 1. The task being optimized

The Phase-D Tier-2 matcher (`PLAYBOOK_V2.md` §6): given the engine's deterministic strategy digest
plus the play-library digest, the 7B emits strict JSON —

- a per-play verdict: `match` / `partial` / `no-match`, for every applicable play;
- the ranked ARMED list (which plays' conditions look met right now, best first);
- an explanation per match **quoting the play's own frozen condition language** against the cited
  engine facts;
- a calibrated confidence per verdict;
- grounding: it may cite only `play:<id>` values that exist in the library + engine-fact tools —
  anything else is dropped by the validator.

The failure modes to train against (= the eval axes): **false-positive matches** (a near-miss
narrative that superficially fits), **missed compound matches** (two individually-weak signals that
jointly meet a narrative), **miscalibration** (confident on thin evidence), **grounding violations**
(citing a play id that doesn't exist), and **JSON/schema breakage**. The last two are exactly the old
Track-A reliability objective — one pipeline now covers both.

## 2. Why matching flips the training economics

The deleted judgment plan needed **expert sailors** because "which tactical call is best" is
subjective — rankings were the only ground truth, and they were expensive, slow, and noisy
(inter-rater agreement was itself a gate). **Matching has an objective answer by construction:**

- For predicate-bearing plays, the **Tier-1 deterministic matcher is a free oracle** — it computes
  the true armed set for any scenario.
- Better: generate scenarios **from** the labels. Pick a target verdict vector ("plays 2 and 5 arm,
  play 3 is a near-miss on its sustain window, the rest are cold"), then synthesize an engine digest
  consistent with it. The label is known before the example exists.

So the core corpus is **programmatically labeled SFT data** — thousands of examples at zero labeling
cost, with mechanical evaluation. Expert sailors move from annotators to **auditors**: they review a
~50-example sample of model outputs for face validity instead of ranking thousands of briefs.

## 3. Data generation

1. **Library generator** — synthetic play libraries varied over the Phase-B scenario templates
   (external + internal plays, §3 of `PLAYBOOK_V2.md`): different predicate thresholds, sustain
   windows, applicability legs, narrative phrasings. Train on many libraries so the model learns to
   read *whatever* library is frozen aboard, not memorize one race's plays.
2. **Scenario generator** — engine digests sampled conditioned on a target verdict vector (the
   oracle labels). Reuses the digest shapes from `strategy.get_strategy_signals` so train ==
   inference.
3. **Near-miss generator (the discriminative gold).** Perturb a matching scenario to *just below*
   threshold (14° of a 15° predicate; the sustain window not yet met; the right signal on the wrong
   leg), and author narrative confounders (a scenario that shares surface vocabulary with a play but
   fails its actual condition). Teaching the model to say **no-match, and why** is most of the value.
4. **Compound/narrative cases** — the part predicates can't label. **Fable is the teacher**: it
   authors hard compound scenarios + verdicts; every teacher label is cross-checked against the
   predicate oracle where they overlap, and inconsistent items are dropped (or human-reviewed).
   This is distillation with a deterministic consistency filter — no human labeling loop.
5. **Preference pairs (optional DPO pass)** — machine-generated: the correct verdict/output vs a
   near-miss-fooled or miscalibrated output for the same scenario. No human ranking anywhere.

## 4. Evaluation — mechanical, with a human audit

All measured at **q4_K_M on the real Orin** (quantization survival is part of the claim), on a
held-out split of libraries *and* scenarios (no library seen in training):

| Axis | Metric | Gate (first-cut) |
|---|---|---|
| Armed-set accuracy | precision / recall / F1 vs oracle | F1 ≥ 0.9 |
| Ranking | top-1 armed play correct | ≥ 0.9 |
| Near-miss discrimination | false-positive rate on near-miss set | ≤ 10% |
| Calibration | confidence buckets vs empirical accuracy | monotone, no >20-pt bucket gap |
| Grounding | invented `play:<id>` / uncited claims | ~0 (validator catches, but rate matters) |
| Reliability | JSON parse / schema pass rate | ≥ 98% |

Plus the standing regression suites (`bench_copilot`, `test_strategy`) must not regress, and an
**expert audit**: sailors read ~50 sampled outputs for "would this explanation make sense on deck".

## 5. Training + deploy (unchanged mechanics)

QLoRA (r=16–32) on a rented GPU over base `Qwen2.5-7B-Instruct`; SFT on the §3 corpus (optionally →
DPO on the §3.5 pairs); merge → GGUF `q4_K_M` → `ollama create` on the Orin (merge-then-quantize, no
Ollama ADAPTER) → blind A/B vs base on the held-out eval. Same path the prior plans validated.

## 6. Sequencing — and the gate that comes before any training

Lesson from the deleted system: don't build training infrastructure ahead of the need.

- **Step 0 (after Phase D ships): build the eval harness FIRST and baseline the stock 7B** with the
  Phase-D matcher prompt. The harness is cheap (it's §3's generators + §4's metrics, all needed for
  Phase-D testing anyway — build it as Phase D's test rig, dual-use).
- **Train only if the baseline fails the §4 gates.** A well-prompted 7B with a compact library
  digest may already match adequately; if so, the LoRA is unnecessary and this plan stays dormant.
- If gated in: SFT pilot (~2–5k programmatic examples, one GPU-day class) → re-eval → optional DPO
  pass → deploy.

## 7. What this deliberately does NOT train

- **Tactical judgment / which strategy is right** — descoped; the plays are authored pre-race by the
  frontier model and the recommendation is the engine's.
- **Voice/phrasing style** — out of scope (unchanged from the old plans).
- **The Tier-1 matcher** — it's deterministic code, the oracle; it is never learned.
