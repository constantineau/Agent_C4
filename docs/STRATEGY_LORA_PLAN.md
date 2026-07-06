# Strategy/Tactics LoRA — judgment fine-tuning plan (human-ranked, expandable)

Improve the onboard copilot's **tactical judgment and calibration** — *which call to make* and how
confidently — by preference-tuning `qwen2.5:7b-instruct-q4_K_M` on **expert-sailor-ranked candidate
briefs**. This is the sibling of [`ORIN_LORA_PLAN.md`](ORIN_LORA_PLAN.md) and **stacks on top of it**:
that plan makes the brief *reliable* (valid JSON, grounded, terminates); this plan makes it *good*.

Decisions locked 2026-07-06 (see the SESSION HANDOFF at the bottom):

| Decision | Choice |
|---|---|
| **Objective** | Tactical **judgment** (hold vs switch vs depart-playbook + when) + **confidence/urgency calibration**. Reliability is Track A. Voice is out of scope. |
| **Teacher / data** | **Human expert sailors** rank candidate briefs. (Opus can't teach tactical taste it doesn't reliably have — it teaches *format*, in Track A.) |
| **Labeling mode** | **Rank candidate briefs** (pick best / order / flag bad) → preference pairs. Sailors never author JSON. |
| **Method** | **SFT-then-DPO**: Track-A reliability SFT is the base; a thin **DPO** pass on the ranked pairs moves judgment. |
| **Volume** | Starts at **a few hundred** ranked snapshots; the pipeline is **built to scale** to the RM-driven flywheel (see §6). |

## Why two stacked tracks, not one

The three things a tuned model could get better at want **different teachers**, and human labels are the
scarce resource:

- **Reliability (JSON/tool-loop)** — Track A, [`ORIN_LORA_PLAN.md`](ORIN_LORA_PLAN.md). Opus-distilled
  SFT, `brief.validate()`-filtered. **No human labels** — don't spend a single sailor-hour on syntax.
- **Judgment + calibration** — Track B, this doc. Human-ranked DPO **on top of** Track A.

`SFT-then-DPO` is the standard recipe and it's exactly right here: Opus locks the format, the humans move
the taste. Every scarce human label goes to judgment; none is wasted on format. **Order matters** — build
Track A first so DPO has a reliable base to preference-shape (and so DPO can't silently break the format;
see the regression gate in §5).

## What a LoRA can and cannot move here (read this first)

Today the **7B does not compute the tactical picture** — the deterministic Tier-1 engine does
(`vps/agent/app/strategy.py::get_strategy_signals`: concordance, per-signal `picture[]`, the numbers).
The onboard model's job in `pi/orin/copilot/copilot.py::strategy_brief` is narrow:

1. phrase the **assessment** (do the signals AGREE → consolidate, or FIGHT → hold and watch), and
2. originate **one recommendation** — including *departing* the playbook (`vs_playbook:"departs"`) —
   grounded ONLY in the signal tools that support it (`get_strategy`/`get_selector`/`get_tactics`/
   `get_drift`/`get_deviation`/`get_fleet`), schema-constrained to `_STRATEGY_SCHEMA`.

So a LoRA can move **the recommendation call** and **calibration**, and the assessment phrasing. It
**cannot** fix a wrong *picture* — that lives in the deterministic engine. This is why the labeling
resource is dual-purpose: when sailors consistently prefer a candidate that departs the engine's
concordance call, that is a **labeled bug report against `strategy.py`** — arguably the highest-value
output of the whole exercise (see §4). Keep the engine and the LLM as separate improvement surfaces.

## 1. The data flywheel (this is most of the "refactor")

```
Real archived race states (Lab-4 learning archive + bench replays)  ─┐
Synthetic snapshots (rare/adversarial coverage)                     ─┴─► snapshot corpus
        │
        ▼  gen_candidates.py — N=3–4 DIVERSE briefs per snapshot
   (base@temp-sampled · Opus · deterministic Tier-1 digest · rule-perturbed)
        │
        ▼  LABELING APP  ◄── expert sailors rank + flag calibration
        │
        ├──► DPO preference pairs   (best vs each worse; §3)
        ├──► held-out EXPERT EVAL set (~25%, NEVER trained on; §5)
        └──► engine-audit signal     (preferred-but-off-concordance → strategy.py; §4)
```

Two things make or break label quality:

- **Snapshots must render human-readably.** A sailor cannot rank raw engine JSON. The app shows the
  situation as tactician text (reuse `copilot._facts_digest`) + a small course/wind/boat schematic (reuse
  the console `plot.js` primitives) + the candidate cards. Garbage rendering → garbage labels.
- **Fold calibration into the rubric.** For each candidate the sailor also marks confidence/urgency as
  *right / too-high / too-low*. A substantively-best-but-miscalibrated brief loses its pair — that is how
  "calibration" becomes a trainable signal rather than a separate project.

### Snapshots
- **Real:** the Lab-4 learning archive (`vps/lab/app/learning.py`, `lab_learning` volume) already stores
  real race states + tracks; derive `gather()`-shaped snapshots (the exact set `copilot.gather` fetches:
  `get_conditions/navigator/tactics/sail_advice/fatigue/forecast/route/ais/fleet/deviation/drift/
  strategy`) at decision moments. Realistic distributions are the point.
- **Synthetic:** a generator (share Track A's `gen_snapshots.py`) covering the space — playbook
  present|absent, signals concordant|split, fleet threat|clear, off-book-worthy vs plain-hold — **weighted
  toward the hard cases** (signals fight, a rival ahead on corrected, drift vs on-water shift disagree),
  since that's where judgment separates from the deterministic default.

### Candidates (`gen_candidates.py`)
Per snapshot, generate **N=3–4 diverse** candidate briefs so there's something real to rank:
- the **base 7B** at a raise-temperature (2 samples for diversity),
- **Opus** (a strong-but-not-infallible reference — sailors may still rank it below a base sample),
- the **deterministic Tier-1** digest verbatim (`get_strategy` → the "no-LLM" answer; anchors the ranking),
- a **rule-perturbed** variant (flip the recommendation / mis-set urgency) to guarantee a clear "worst".

Candidate generation must be **reproducible** (`snapshot_id → deterministic candidate set`, cached) so a
snapshot labeled today is still valid when you retrain months later. Every candidate passes
`brief.validate()` before it's shown — we're ranking *judgment*, not format (Track A owns format).

## 2. Labeling app — multi-labeler from day one

Build it as a **multi-labeler service**, not a single-user tool, because the whole expandability story
(§6) runs through it. Minimal FastAPI + single-page ranker, labels to sqlite/jsonl, hosted behind nginx on
the shared Lab VM (same pattern as `lab.racertracer.net`, shared password). Requirements:

- **Labeler accounts** + a queue that **assigns** snapshots (so two sailors don't both do the easy ones).
- **Deliberate overlap** — ~15–20% of snapshots double-labeled — to measure **inter-rater agreement**
  (the pilot gate, §7) and to weight reliable sailors more as the pool grows.
- **Gold-trap snapshots** (a few with a known-correct answer) to score labeler reliability.
- **Human-readable rendering** (above) + the candidate cards + a per-candidate calibration flag.
- **Append-only, versioned label store** keyed by `(snapshot_id, candidate_id, labeler_id, rank,
  calibration_flags, ts)` — captures **full rankings**, not collapsed winner/loser, because the reward
  model (§6) trains on the richer signal. Same schema works at 300 or 300k labels.

## 3. Preference dataset + DPO training

- **Pairs:** each ranked snapshot of N candidates → best-vs-each-worse pairs (~2–3 per snapshot). With
  ~350 labeled snapshots and ~90 held out (§5), ~260 ranked × ~2.5 ≈ **~650 DPO pairs** — a legitimate
  thin DPO set for a 7B at low rank. A miscalibrated-but-otherwise-best candidate is demoted per §1 so
  calibration rides in the same pairs.
- **Base:** the **Track-A merged fp16** (reliability SFT already applied), same lineage as the deployed q4.
- **Method:** QLoRA + **DPO** (or IPO/ORPO if DPO is unstable on the small set), Unsloth on a rented GPU.
- **Gentle, to protect the reliability base:** r=16, α=2r, target q,k,v,o,gate,up,down; **low β≈0.1**;
  **1–2 epochs**; LR ~5e-6 (DPO wants a much smaller LR than SFT); early-stop on the held-out reward
  margin. The small set + strong base means over-shaping is the risk, not under-fitting.
- **Reliability anchor:** keep a little Track-A SFT replay mixed in (or at minimum run the §5 mechanical
  regression gate after DPO) — DPO can quietly erode JSON/format adherence.

Artifacts: `pref.train.jsonl`, `pref.eval.jsonl` (the held-out expert set is separate, §5).

## 4. Engine-audit side output (don't skip — high ROI)

Because the picture is deterministic, every ranking is also a datapoint on the **engine**. Log, per
snapshot: did the human-preferred candidate **agree** with `strategy.get_selector`'s HOLD/SWITCH/OFF-SCRIPT
and the concordance lean? A systematic disagreement (experts keep preferring a departure the engine's
concordance vote didn't flag) is a **tuning/bug signal for `strategy.py`** (`_concordance`,
`_recommendation`, the Schmitt bands) that no LoRA can fix. Feed these back as `strategy.py` issues. At the
low-volume tier this may beat the LoRA gain outright.

## 5. Evaluation — mechanical gate + expert blind A/B

Two gates, both **at q4 on the real Orin** (fp16 numbers don't count — quantization can erode gains):

1. **Mechanical regression (must not regress).** Run `pi/orin/copilot/bench_copilot.py` +
   `vps/agent/test_strategy.py`: parse rate, schema-valid, grounding pass (`brief.validate()`), loop
   termination, fallback rate, latency (~12 tok/s, must be unchanged). DPO must not break what Track A
   fixed — this is the safety gate.
2. **Expert blind A/B (the real success metric).** Over the **held-out ~25%** of labeled snapshots (never
   trained on), show sailors base-q4 vs tuned-q4 briefs **blind** and have them rate/pick. Ship only if
   tuned wins on judgment **with no reliability regression and no latency hit**. Reuse the labeling app in
   an A/B mode.

Measure the **base-q4 baseline first** to set the bar (mirrors Track A's discipline).

## 6. Expandability — the RM-driven flywheel (build the seams now)

Scaling labels must NOT mean scaling effort linearly. The lever is a **reward model (RM)**: once there are
enough rankings, train a small RM (on the 7B or smaller) that learned the sailors' preferences; it then
**scores unlimited candidate briefs automatically** → best-of-N distillation into SFT, or RLAIF. Human
labels stop being training data and become the thing that *trains the scorer*.

**The one design rule:** every artifact is RM-ready from day one — reproducible `snapshot_id`s,
deterministic candidate sets, and a preference store capturing **full rankings + calibration flags +
labeler identity** (§2). Build that now and every later tier is free.

| Labels | Unlocks | Method |
|---|---|---|
| **~300–500** | Calibration + modest judgment gain; the engine audit (§4); the eval set | Thin **DPO** on Track A |
| **~1–2k** | Robust DPO; **RM v1 viable**; start best-of-N filtering | DPO + first reward model |
| **~5k+** | **RM-driven flywheel** — humans label only hard cases, RM scores the rest, SFT-judgment corpus via best-of-N | Active-learning + RM + SFT-judgment |

Two more seams to build in Phase 0 so the higher tiers need no rework:
- **Pluggable sampling module** (`sampling.py`): random now; swap in **active learning** later (surface
  snapshots where candidates are closest / the model is least certain) so at scale sailors label the
  *informative* cases, not 5,000 easy ones.
- **Continuous loop:** after first deploy, log real in-race briefs, let `sampling.py` flag the low-confidence
  / off-book ones into the labeling queue, retrain on a cadence — sailor effort compounds.

**Where returns bend (set expectations):** more labels help most on *which call to make* (widest solution
space). *Calibration* largely saturates in the low thousands. *Reliability* wants no human labels at all.
"Expandability" = build the RM-ready flywheel so that IF the Tier-1 judgment gains look promising, you can
pour labels into that one axis without touching anything else.

## 7. Phases

- **Phase 0 (no GPU — start now):** build `pi/orin/training/` additions — `gen_candidates.py`,
  `labeling/` (the multi-labeler app + store + rubric), `sampling.py`, `eval_judgment.py`; wire real
  snapshots from the Lab-4 archive. **Run a 30–50 snapshot PILOT with 2–3 sailors** → validate the UI and,
  critically, **inter-rater agreement** (if good sailors don't agree, the rubric is wrong — fix it before
  scaling; no DPO fixes a bad rubric). Measure the base-q4 baseline on the Orin.
- **Phase 1 (labeling push + Track A):** the full labeling run (a few hundred, scalable). In parallel,
  execute Track A reliability SFT (independent of human labels).
- **Phase 2 (GPU):** Track-A SFT → DPO judgment on top (gentle, §3). Merge → GGUF q4_K_M → `ollama create`
  on the Orin (the deploy path in `ORIN_LORA_PLAN.md` §3 / `pi/orin/DEPLOYMENT.md`; no Ollama `ADAPTER` —
  merge-then-quantize).
- **Phase 3 (Orin):** eval at q4 — mechanical regression gate + expert blind A/B (§5). Ship or iterate.
  Feed the engine-audit findings (§4) back into `strategy.py`.
- **Phase 4+ (expand):** if the pilot showed strong agreement and Tier-1 gains look good, scale labeling
  toward ~1–2k → train **RM v1** → best-of-N; then the active-learning + RM flywheel (§6).

## 8. Risks & mitigations
- *DPO erodes reliability* → Track-A SFT base + reliability replay + the mechanical regression gate (§5);
  low β, 1–2 epochs, small LR.
- *Rubric ambiguity → low inter-rater agreement* → the Phase-0 pilot gate + overlap + gold traps (§2, §7).
- *Small set over-shapes* → gentle DPO + held-out expert eval + early stop.
- *q4 erodes gains* → eval at q4 on the Orin, not fp16; q5_K_M fallback if it fits 8 GB.
- *Labels can't fix the picture* → that's the engine's job; route the signal to `strategy.py` (§4).
- *Modest gain at low volume* → set expectations (§6); the eval harness + engine audit are certain ROI
  regardless.

## 9. Proposed repo layout (extends `pi/orin/training/`)
```
gen_candidates.py     snapshot_id → N diverse briefs (reproducible, validate()-filtered)
sampling.py           pluggable snapshot selection (random now → active-learning later)
labeling/
  app.py              FastAPI multi-labeler ranker (accounts, queue, overlap, gold traps)
  render.py           snapshot → human-readable situation + course/wind schematic
  store.py            append-only versioned preference store (rankings + calib + labeler id)
make_pairs.py         rankings → DPO preference pairs (+ calibration demotion)
train_dpo.py          QLoRA + DPO on the Track-A base (rented GPU)
rm_train.py           (Tier 2+) reward model from the full rankings
eval_judgment.py      expert blind A/B harness + engine-audit report, base vs tuned at q4
README.md             the runbook
```

RRS 41: all pre-race homework, changes nothing about compliance. Onboard the model is the boat's own gear
(legal in-race) and MAY originate strategy; this track just makes its calls better and better-calibrated.
The frozen playbook stays a strong prior it may depart from (flagged), not a cage.

---

## ▶▶ SESSION HANDOFF — 2026-07-06 (plan authored, nothing built yet)
- **Status:** design doc only. No code, no `pi/orin/training/` additions yet. This is the judgment/DPO
  sibling to the (also-not-yet-executed) reliability plan in `ORIN_LORA_PLAN.md`.
- **Locked with the user:** objective = *which call to make* + *confidence/urgency calibration* (+ fold in
  reliability via Track A); labeling = **rank candidate briefs** (→ DPO); volume = a few hundred to start
  but **design for expandability** → the RM-driven flywheel (§6). Voice/phrasing is out of scope.
- **Key architectural fact to preserve:** the picture is deterministic (`strategy.py`); the 7B only
  phrases + picks the recommendation (`copilot.strategy_brief`). So the LoRA moves the call + calibration,
  and the rankings double as an audit of `strategy.py` (§4).
- **Next step (recommended):** Phase 0 — scaffold `gen_candidates.py` + the labeling app + rubric, wire
  real snapshots from the Lab-4 archive, then run the 30–50 snapshot PILOT with 2–3 sailors and check
  inter-rater agreement before scaling. Lock the rubric before writing much labeling UI.
- **Open choices deferred to Phase 0:** exact N and candidate mix; DPO vs IPO/ORPO; where to host the
  labeling app (shared Lab VM nginx, like `lab.racertracer.net`); how much Track-A SFT replay to mix into
  the DPO run.
