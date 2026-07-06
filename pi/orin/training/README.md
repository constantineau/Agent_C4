# Strategy/Tactics LoRA — Phase-0 data + labeling pipeline

Track B of the onboard-LLM fine-tune (design: [`docs/STRATEGY_LORA_PLAN.md`](../../../docs/STRATEGY_LORA_PLAN.md)):
improve the copilot's tactical **judgment + calibration** by preference-tuning the Orin 7B on
**expert-sailor-ranked candidate briefs**. This package is the Phase-0 flywheel —
**snapshots → candidates → labeling app → preference pairs** — plus the eval/engine-audit scaffold.

It does **not** train (that's Phase 2, on a rented GPU) and it never touches the deployed copilot.
Run everything as modules **from `pi/orin/`** (the copilot is a sibling package):

```bash
cd pi/orin
pip install -r training/requirements.txt          # only needed for the labeling app + Opus teacher
```

## The unit that gets ranked

A sailor ranks a **strategy brief** whose *picture + concordance are FIXED* (the engine's
deterministic facts from `vps/agent/app/strategy.py`) and whose **assessment + recommendation** vary
between candidates — exactly the judgment call a LoRA can move. The picture is the engine's job; the
sailors' rankings therefore *also* audit `strategy.py` (see the engine-audit below).

## Pipeline

```
gen_snapshots  →  gen_candidates  →  labeling.app (sailors rank)  →  make_pairs  →  train (Phase 2)
                                            │                            │
                                            └──────────  eval_judgment  ─┘   (agreement · engine audit · calibration)
```

### 1. Build the snapshot corpus
```bash
python3 -m training.gen_snapshots            # synthetic, hard-case-weighted (offline; the pilot corpus)
python3 -m training.gen_snapshots --from-engine --append   # optional: append a live /strategy digest
```
→ `training/data/snapshots.jsonl`. Synthetic digests replicate the engine's own pure helpers (copied
into `synth.py`), so they're what the real Tier-1 engine *would* produce. Real decision-state capture
(logging `/strategy` in-race) is a future add — see `gen_snapshots.build_from_engine`.

### 2. Generate diverse candidates
```bash
python3 -m training.gen_candidates
```
→ `training/data/candidates.jsonl`. Per snapshot: `deterministic` (the engine answer, the anchor),
`perturbed` (a rule-flipped worse call), `base` (the deployed 7B at raised temp — needs a reachable
`LLM_BASE_URL`), `opus` (needs `ANTHROPIC_API_KEY`). deterministic + perturbed are fully offline; base
+ opus are best-effort (skipped, never blocking). The prompt/schema MATCH `copilot.strategy_brief`, so
what's ranked is what the model actually emits in-race.

### 3. Label (the sailors' job)
```bash
python3 -m training.labeling.app             # → http://127.0.0.1:8400  (team password: label-dev)
```
Sailors log in with their name + the shared password, then rank the candidates best→worst and flag
each one's confidence/urgency calibration. Candidates are served **blind** (no origin) and shuffled
per labeler to kill position/brand bias. The queue gives full single coverage, then ~`OVERLAP_FRAC`
double coverage for inter-rater agreement. **Host it behind nginx on the shared Lab VM** (like
`lab.racertracer.net`) for the real labeling push; set a real `TRAIN_LABEL_PASSWORD`.

### 4. Build preference pairs
```bash
python3 -m training.make_pairs
```
→ `training/data/pref.jsonl` (DPO best-vs-worse pairs, carrying the exact copilot prompt so
train == inference) + `training/data/eval_holdout.txt`. A deterministic ~`EVAL_HOLDOUT_FRAC` of
snapshots (by content hash) **never** become pairs — they're the blind-A/B eval set. Calibration
demotion: a candidate flagged `too_high`/`too_low` loses its pair to an adjacent `right`-flagged one.

### 5. Eval + engine audit (the pilot gate)
```bash
python3 -m training.eval_judgment
```
→ `training/data/eval_report.json`. Three reads:
- **Inter-rater agreement** — top-1 agreement + mean Kendall tau over double-labeled snapshots.
  **The pilot gate: aim for top-1 ≳ 0.6 before scaling. Low agreement = fix the rubric, not the model.**
- **Engine audit** — how often the sailors' consensus best ≠ the deterministic candidate, by scenario
  tag → a labeled tuning/bug signal for `strategy.py` (Plan §4).
- **Calibration** — the too_high/too_low distribution overall + by origin.

### 6. Notes → proposals (the free-text signal)
```bash
python3 -m training.notes_review            # LLM-cluster labeler notes → proposals; print the digest
python3 -m training.notes_review --list     # stored proposals + status
python3 -m training.notes_review --accept|--dismiss <id>
```
`make_pairs` uses only the ranking order + calibration — the free-text NOTES were write-only. This
reads every note (with its snapshot context), asks Opus (best-effort; deterministic fallback) to
CLUSTER them into concrete PROPOSALS (situation content / rubric / engine), and stores them `open` for
a human to accept/dismiss. **Propose-only — mutates nothing** (a person implements the accepted ones),
mirroring the Lab-4 learning loop. → `training/data/note_proposals.jsonl`.

### Smoke test (no engine / LLM / key)
```bash
python3 -m training.smoke
```
Runs the whole flywheel on a throwaway DB with two simulated labelers and asserts it all connects.

## Hosting — `lab.racertracer.net/training/` (live)

The ranker is deployed as a sub-menu on the Lab domain (shared VM). The labeling web app needs **no
`copilot` at runtime** (only the offline generation scripts do), so it runs as its own lightweight
standing service and is proxied under `/training/`:

- **Service:** `pi/systemd/c4-labeling.service` runs `python3 -m training.labeling.app` from the repo
  working copy, bound to `127.0.0.1:8400`, password `CAN100` (same as the Lab). Reboot-persistent.
  Install: `sudo cp pi/systemd/c4-labeling.service /etc/systemd/system/ && sudo systemctl daemon-reload
  && sudo systemctl enable --now c4-labeling`. Data + labels live under `pi/orin/training/data/`
  (on the host FS — easy to back up; **don't delete `data/labels.sqlite`**, it's the sailors' work).
- **nginx:** a self-contained block on the `lab.racertracer.net` vhost
  (`/etc/nginx/sites-available/lab`, NOT in the repo): `location /training/ { proxy_pass
  http://127.0.0.1:8400/; ... }` (trailing slash strips the prefix) + `location = /training { return
  301 /training/; }`. The app uses relative URLs + `<base href="./">` so it works under the subpath.
- **Lab nav:** a `Labeling ↗` link in `vps/lab/web/index.html` (baked into the Lab image — a nav
  change needs `docker compose -f compose.dev.yml build lab && up -d lab`).

To refresh the corpus the sailors see (e.g. after a rubric/scenario change), regenerate on the host and
restart the service: `python3 -m training.gen_snapshots && python3 -m training.gen_candidates &&
sudo systemctl restart c4-labeling`.

## The pilot protocol (Plan §7)

1. Generate ~30–50 snapshots (`gen_snapshots`; trim the corpus or lower `TRAIN_SYNTH_RANDOM_N`).
2. Generate candidates (turn on `base`/`opus` if you want ≥3 real options to rank).
3. Get **2–3 sailors** to each label the set through the app.
4. Run `eval_judgment`. **Check inter-rater agreement first.** If good sailors don't agree, revise the
   rubric / the candidate presentation and re-pilot — before any scaling or DPO.
5. Only once agreement holds: scale labeling, then Phase 1/2 (Track-A reliability SFT → DPO).

## Files

| File | Role |
|---|---|
| `config.py` | env-overridable paths + knobs (all offline-sane defaults) |
| `schema.py` | canonical snapshot/candidate shapes, content-hash IDs, grounding filter |
| `synth.py` | synthetic digests across the judgment space (engine helpers copied from `strategy.py`) |
| `gen_snapshots.py` | build the snapshot corpus (synthetic + optional live engine) |
| `gen_candidates.py` | N diverse blind candidates per snapshot |
| `teacher.py` | optional Opus candidate (anthropic SDK, best-effort) |
| `sampling.py` | the queue policy + the **active-learning seam** (Plan §6) |
| `labeling/store.py` | append-only sqlite preference store (full rankings + calibration) |
| `labeling/render.py` | blind, human-readable snapshot/candidate payloads |
| `labeling/app.py` | the FastAPI multi-labeler ranker |
| `labeling/static/` | the vanilla-JS ranker UI |
| `make_pairs.py` | rankings → DPO pairs + held-out split |
| `eval_judgment.py` | inter-rater agreement + engine audit + calibration |
| `smoke.py` | end-to-end offline self-test |

## Config knobs (env)

`TRAIN_DATA_DIR` · `TRAIN_SYNTH_RANDOM_N` · `TRAIN_CAND_ORIGINS` · `TRAIN_BASE_SAMPLES` /
`TRAIN_BASE_TEMP` · `ANTHROPIC_MODEL` / `ANTHROPIC_API_KEY` · `TRAIN_LABEL_PORT` /
`TRAIN_LABEL_PASSWORD` · `TRAIN_OVERLAP_FRAC` · `TRAIN_EVAL_HOLDOUT_FRAC` · `LLM_BASE_URL` /
`LLM_MODEL` (from the copilot, for the base path).
