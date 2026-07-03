# SR33 onboard copilot — decision-support layer (Phase 9.4, Tier 2)

The **SR33 copilot service** — the next 9.4 increment after the runtime bring-up. It turns the
Tier-1 engine's facts (and, later, the frozen playbook) into **bounded, grounded decision
support** via the local LLM. Runs **on the Orin**, talks to the Pi-4 engine over boat-local
Wi-Fi and to the LLM over localhost. RRS-41-safe: never phones the cloud (the real line — an off-boat
round-trip is the violation). Onboard it may originate strategy; the engine still does the math and it
grounds every claim in engine facts + the playbook — reliability discipline for a 7B, not RRS-41 limits.

Two surfaces, same guardrails: the **decision-support / tool-calling layer** (PULL — the crew asks,
`POST /brief`) and the **crew-facing narration layer** (PUSH — the copilot surfaces callouts on its own,
`POST /narrate`). The point is the *structure and the guardrails*, proven on real hardware.

```
  Pi 4 (Tier 1)                    Orin Nano (Tier 2)
  engine :8200  ──facts (read-only)──►  copilot :8300  ──OpenAI /v1──►  Ollama :11434
  (does ALL the math)                   (interprets, grounds)          (qwen2.5:7b-instruct-q4_K_M)
```

> **Endpoint reality:** the older `pi/orin/` runbook targets MLC on `:9000`; the unit actually
> runs **Ollama on `:11434`** (same OpenAI `/v1` contract). The copilot only sees `/v1`, so the
> runtime stays swappable — the defaults here just match what runs.

## The guardrails (why this is the "decision-support layer")

The bounded structure enforces the locked design rules **structurally, not just by prompt**:

1. **The engine does the math.** The LLM has no calculator and no data access except a closed set
   of read-only engine-fact tools (`tools.py`). If it wants a number it must ask the engine.
2. **No ungrounded content.** Every `factor` / `recommendation` carries `grounded_in` (the tool(s)
   it rests on). `brief.validate()` **drops** anything that cites nothing real — so a
   recommendation that isn't backed by an engine fact (or a playbook variant) cannot survive.
3. **The engine owns the caveats.** Staleness / forecast-is-a-model / playbook-status caveats are
   computed from the facts (`brief.structural_caveats`), not authored by the model (which once
   wrote "the forecast indicates…" without ever fetching a forecast).
4. **Advisory, never sole authority.** Every brief carries the standing `disclaimer` and a
   first-class `confidence`.
5. **Always produces a brief.** If the LLM is off, unreachable, slow, or its output fails
   validation, the service returns the **deterministic** brief built from the same facts. The LLM
   interprets and prioritizes on top; it can never exceed the facts.
6. **Playbook as a strong prior.** A frozen playbook (Lab-2 output) loads via `PLAYBOOK_PATH`; the
   copilot leans on its pre-authored variants but may depart from them onboard (legal), flagging when
   it does. Absent → it says so and reasons from live engine facts alone.

## Crew-facing narration — the PUSH surface (`narrate.py`, `POST /narrate`)

The brief is PULL (the crew asks). Narration is **PUSH**: a deterministic callout engine watches
the gathered engine facts + the frozen playbook and surfaces the few things worth **showing right
now** — a **closing-traffic collision warning** (safety, top priority — the nearest closing AIS
contact inside the CPA/TCPA guard, shown "now" inside ~0.5 nm / 12 min, "soon" inside ~1.5 nm / 30
min; always legal in-race = own receiver + own math), a **timed mark-rounding prep** (escalating ~15
/ 10 / 5-min heads-up, with the leg-after homework: the maneuver, the TWA once round, and the sail to
stage for the next leg), a **playbook branch trigger** firing, the two **Lab-3 branch triggers** —
**route-deviation** (off the frozen variant's optimal track: XTE / behind-plan-pace, from `/deviation`)
and **forecast-drift** (the common forecast has veered/backed since the plan was frozen, from `/drift`),
each shown only at the engine's fuzzy watch/act — a **handicap rival** (a roster boat within the
corrected-time band, or one projected AHEAD of us on corrected — grounded in `get_fleet`,
confidence-gated so a fuzzy/aged match stays quiet), an upcoming **sail change-down**, a **helm
rotation**, **stale instruments**. The LLM only *phrases* the top one or two into a calm coach line;
the deterministic callout text is the always-on fallback.

Same guardrails as a brief: every callout is **grounded** in a real engine fact and/or a playbook
variant (ungrounded ones are dropped) and the engine does the math — reliability discipline. It may
originate strategy onboard; the in-race-legal posture is simply that it all runs on the boat's own gear
(the pre-authored homework + the engine's own numbers). State is a tiny per-route **show-once** dedup (raise-slow / clear-fast, like the cloud
alerting loop): `POST /narrate` returns `active` (every confirmed callout — the banner set,
priority-sorted) and `new` (the callouts that just crossed their confirmation threshold this poll —
what's worth showing), so the iPad can poll it every ~15 s and only surface what's genuinely new.
`POST /narrate/reset` clears the dedup on a race / course change. The rounding callout reads
`navigator.next_rounding` — the geometry of the leg *after* the next mark, computed by the engine.

## Playbook adherence — the dashboard tile (`adherence.py`, `GET /adherence`)

The data source for the crew dashboard's **PLAYBOOK-ADHERENCE** tile (`docs/COPILOT_DASHBOARD.md`):
"are we sailing the frozen homework, and has a branch trigger fired?" Deterministic (no LLM) — it
compares the loaded Lab-2 playbook (the `recommended` start variant + each variant's `what_flips_it`
trigger, keyed by first-beat side) against the engine's tactical read (`get_tactics`: persistent vs
oscillating, favored side). States: **ok** = on plan (oscillating, or a persistent shift confirms
the recommended side); **watch** = an oscillating lean toward a non-recommended side (early warning);
**act** = a persistent shift now favors a DIFFERENT variant → the playbook's branch says switch (the
tile names the variant + surfaces its trigger). Returns a ready-made dashboard tile object (status/
value/sub/why/consider/clears/based/rows), `na` when no playbook is aboard. Same posture as the
brief/narration, this deterministic tile just compares the pre-authored variants against the tactical
read (onboard is legal in-race either way). The dashboard polls `GET /adherence` on its own ~8 s
cadence (the copilot does the engine round-trip).

## Files

| File | What |
|---|---|
| `config.py` | env config (engine URL, LLM `/v1` URL + model, timeouts, playbook path, port) |
| `engine_client.py` | read-only client for the Pi engine fact endpoints (stdlib) |
| `tools.py` | the **bounded tool surface** — OpenAI function specs + dispatch (the only LLM capabilities) |
| `playbook.py` | loads a frozen playbook bundle (Lab-2); digest for the prompt + signature verify |
| `adherence.py` | deterministic **playbook-adherence** for the dashboard tile (on-plan / branch-fired) |
| `brief.py` | the `DecisionBrief` shape, the grounding `validate()`, grounded `structural_caveats()`, and the deterministic builder |
| `llm.py` | minimal OpenAI `/v1` chat client with tool-calling (stdlib) |
| `copilot.py` | orchestration: gather facts → bounded tool loop → validate → fallback; `make_narration()` |
| `narrate.py` | the **PUSH** callout engine: grounded callouts (rounding/playbook/sail/fatigue/data) + show-once dedup + LLM phrasing with deterministic fallback |
| `app.py` | FastAPI service (`/health`, `/tools`, `POST /brief`, `POST /narrate`, `POST /narrate/reset`, `GET /adherence`, `/snapshot`) |
| `bench_copilot.py` | exit test for the layer (deterministic + `--llm`); pure stdlib |
| `requirements.txt` | only `fastapi`+`uvicorn` (the logic is stdlib) |

## Run

```bash
# on the Orin (co-located with Ollama):
cd pi/orin
pip install -r copilot/requirements.txt
ENGINE_URL=http://<pi-ip>:8200 python3 -m uvicorn copilot.app:app --host 0.0.0.0 --port 8300
curl -s localhost:8300/health
curl -s -X POST localhost:8300/brief -H 'Content-Type: application/json' \
     -d '{"question":"What should the crew focus on right now?"}'
# proactive callouts + a coach line (poll this every ~15 s on the iPad):
curl -s -X POST localhost:8300/narrate -H 'Content-Type: application/json' -d '{}'
# playbook-adherence tile (deterministic; needs PLAYBOOK_PATH set to a frozen Lab-2 bundle):
curl -s localhost:8300/adherence
```

Env: `ENGINE_URL` (Pi engine), `LLM_BASE_URL` (default `http://127.0.0.1:11434/v1`), `LLM_MODEL`,
`LLM_TIMEOUT`, `MAX_TOOL_ROUNDS`, `PLAYBOOK_PATH`, `COPILOT_ROUTE`, `COPILOT_PORT`,
`COPILOT_USE_LLM` (false → always deterministic). Systemd unit:
`pi/systemd/sr33-orin-copilot.service`.

## Bench / exit test

```bash
cd pi/orin
python3 -m copilot.bench_copilot              # deterministic (engine only)
python3 -m copilot.bench_copilot --llm        # full bounded tool-loop against the LLM
```

The exit test asserts: a brief is always produced; every factor/rec is grounded in a tool the run
used; the disclaimer is present; the validator drops a poisoned ungrounded item. It also runs a
**pure narration-logic test** (no engine/LLM needed) on a synthetic snapshot: the rounding /
layline / shift / fatigue callouts trip and are all grounded; **raise-slow** (a persistence-gated
callout isn't shown until its second poll); **clear-fast / show-once** (a shown callout isn't
re-surfaced, and `active` empties the moment a callout's condition goes away); priority sorting
(`rotate_now` first); and the deterministic coach line. Then it audits narration against the live
engine (grounding + honest mode). **Bench-verified on the real Orin** (qwen2.5:7b @ :11434 over a
Tailscale SSH forward from the dev VM, with the Pi engine on :8200): deterministic path green; the
LLM path runs the tool loop (the model calls `get_forecast` on demand), returns a grounded brief in
~45 s warm; and the graceful fallback fires when the LLM is cold/slow (first-token model load is
~2 min) or returns ungrounded JSON. The narration pure-logic test is green (11/11); against a live
engine with a mark 24 min out + not on a layline it correctly shows *nothing* (honest `mode:none`),
and it produces the staged rounding call once the mark is inside the 15-min window.

### Known v1 limits

- **Latency:** the 7B is bandwidth-bound (~12 tok/s) and the first request after idle pays a ~2 min
  model load. A brief is ~45 s warm. `LLM_TIMEOUT` defaults to 120 s → a cold first call falls back
  to deterministic; raise it, or pre-warm the model (a trivial `/v1` ping) on service start.
- **Small-model interpretation wobble:** qwen-7B sometimes mislabels which quantity is which in the
  prose (e.g. calls a TWS number "speed"). Numbers are always *grounded* in real fetched facts, and
  narration phrases pre-computed grounded callout text (so a wobble can't invent a number), but the
  prose can still be slightly off — tune the narration system prompt against real-race transcripts.
- **Narration is stateful per process.** The show-once dedup lives in `narrate._STATE` (one boat,
  one process — same as the cloud alerting loop); restart the service or `POST /narrate/reset` on a
  race / course change so callouts re-surface from scratch.
