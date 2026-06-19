# SR33 onboard copilot — decision-support layer (Phase 9.4, Tier 2)

The **SR33 copilot service** — the next 9.4 increment after the runtime bring-up. It turns the
Tier-1 engine's facts (and, later, the frozen playbook) into **bounded, grounded decision
support** via the local LLM. Runs **on the Orin**, talks to the Pi-4 engine over boat-local
Wi-Fi and to the LLM over localhost. RRS-41-safe: never phones the cloud, never does the math,
never invents strategy outside the engine facts + the playbook.

This first increment is the **decision-support / tool-calling layer** (crew-facing narration is
the next increment). The point is the *structure and the guardrails*, proven on real hardware.

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
6. **Playbook-bounded.** A frozen playbook (Lab-2 output) loads via `PLAYBOOK_PATH`; the copilot
   *selects/interprets* its pre-authored variants. Absent → it says so and restricts itself to
   live engine facts.

## Files

| File | What |
|---|---|
| `config.py` | env config (engine URL, LLM `/v1` URL + model, timeouts, playbook path, port) |
| `engine_client.py` | read-only client for the Pi engine fact endpoints (stdlib) |
| `tools.py` | the **bounded tool surface** — OpenAI function specs + dispatch (the only LLM capabilities) |
| `playbook.py` | loads a frozen playbook bundle (Lab-2); thin until Lab-2 lands |
| `brief.py` | the `DecisionBrief` shape, the grounding `validate()`, grounded `structural_caveats()`, and the deterministic builder |
| `llm.py` | minimal OpenAI `/v1` chat client with tool-calling (stdlib) |
| `copilot.py` | orchestration: gather facts → bounded tool loop → validate → fallback |
| `app.py` | FastAPI service (`/health`, `/tools`, `POST /brief`, `/snapshot`) |
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
used; the disclaimer is present; the validator drops a poisoned ungrounded item. **Bench-verified
on the real Orin** (qwen2.5:7b @ :11434 over a Tailscale SSH forward from the dev VM, with the Pi
engine on :8200): deterministic path green; the LLM path runs the tool loop (the model calls
`get_forecast` on demand), returns a grounded brief in ~45 s warm; and the graceful fallback fires
when the LLM is cold/slow (first-token model load is ~2 min) or returns ungrounded JSON.

### Known v1 limits

- **Latency:** the 7B is bandwidth-bound (~12 tok/s) and the first request after idle pays a ~2 min
  model load. A brief is ~45 s warm. `LLM_TIMEOUT` defaults to 120 s → a cold first call falls back
  to deterministic; raise it, or pre-warm the model (a trivial `/v1` ping) on service start.
- **Small-model interpretation wobble:** qwen-7B sometimes mislabels which quantity is which in the
  prose (e.g. calls a TWS number "speed"). Numbers are always *grounded* in real fetched facts;
  tightening the narration is the next (narration) increment.
- **Playbook is a stub** until Lab-2 emits a bundle; the schema in `playbook.py` is the forward
  declaration the copilot is already written against.
