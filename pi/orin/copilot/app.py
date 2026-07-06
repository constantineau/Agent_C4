"""FastAPI service for the onboard copilot (Tier 2). Runs on the Orin.

Endpoints:
  GET  /health    — reachability of the engine + LLM, playbook + model status
  GET  /tools     — the bounded tool surface the LLM is allowed (introspection)
  POST /brief      — produce a DecisionBrief for the current situation (+ optional question)
  POST /narrate    — proactive crew callouts + a coach line for what's newly worth showing (PUSH)
  POST /narrate/reset — clear the per-route show-once dedup (a race / course change)
  GET  /coach      — the proactive AUTO-COACH state: latest coach line + active callouts + history,
                     produced by a background timer (the copilot volunteers coaching on a cadence)
  GET  /adherence — playbook-adherence tile (on-plan / branch-trigger-fired; deterministic)
  GET  /snapshot  — raw gathered engine facts (debug / "show me what you saw")

There is intentionally no endpoint that takes an action — the copilot is read-only and advisory.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import coach as coach_mod
from . import config, copilot, dashboard_brief, playbook as playbook_mod, tools
from .engine_client import EngineClient
from .llm import LLMClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The proactive auto-coach timer runs for the life of the service (no-op if COPILOT_COACH=false).
    await coach_mod.start()
    try:
        yield
    finally:
        await coach_mod.stop()


app = FastAPI(title="Agent_C4 Onboard Copilot", version="0.1.0", lifespan=lifespan)
# The iPad may hit this directly over boat-local Wi-Fi.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class BriefRequest(BaseModel):
    question: str = ""
    route: str | None = None
    hoisted: str | None = None
    use_llm: bool | None = None


class DashboardRequest(BaseModel):
    tiles: list[dict] = []


class DetailRequest(BaseModel):
    domain: str
    question: str = ""
    tiles: list[dict] = []


class NarrateRequest(BaseModel):
    route: str | None = None
    hoisted: str | None = None
    use_llm: bool | None = None


@app.get("/health")
def health():
    engine = EngineClient()
    pb = playbook_mod.load()
    eng_ok = engine.reachable()
    llm_ok = LLMClient().reachable() if config.USE_LLM else False
    return {
        "status": "ok",
        "service": "onboard-copilot",
        "engine": {"url": engine.base_url, "reachable": eng_ok},
        "llm": {"url": config.LLM_BASE_URL, "model": config.LLM_MODEL,
                "enabled": config.USE_LLM, "reachable": llm_ok},
        "playbook": {"loaded": pb.loaded, "race_id": pb.race_id,
                     "variants": len(pb.variants),
                     "signed": pb.signed, "signature_ok": pb.signature_ok,
                     "sail_inventory": pb.boat_model.get("sail_inventory", [])},
        # Honest degraded modes so the crew knows what they're getting.
        "mode": ("llm" if (eng_ok and llm_ok) else
                 "deterministic" if eng_ok else "engine-unreachable"),
        "coach": {"enabled": coach_mod.COACH.enabled, "running": coach_mod.COACH.running(),
                  "interval_s": coach_mod.COACH.interval, "ticks": coach_mod.COACH.ticks,
                  "last_error": coach_mod.COACH.last_error},
    }


@app.get("/tools")
def list_tools():
    """The complete, closed set of capabilities the LLM has — nothing else is callable."""
    return {"tools": [t["function"]["name"] for t in tools.TOOL_SPECS], "specs": tools.TOOL_SPECS}


@app.post("/brief")
def brief(req: BriefRequest):
    return copilot.make_brief(question=req.question, route=req.route,
                              hoisted=req.hoisted, use_llm=req.use_llm)


@app.post("/strategy")
def strategy(req: NarrateRequest):
    """In-race STRATEGY SYNTHESIS: the LLM phrases the engine's deterministic cross-signal digest
    (forecast-vs-actual + fleet position + route-deviation + wind shift → concordance) into a
    crew-facing assessment and enriches the recommendation's rationale by matching the picture
    against the playbook's frozen conditions. It never changes the engine's recommendation (descope
    2026-07-06). Falls back to the deterministic digest on any LLM trouble."""
    return copilot.strategy_brief(route=req.route, hoisted=req.hoisted, use_llm=req.use_llm)


@app.post("/narrate")
def narrate(req: NarrateRequest):
    """Proactive crew callouts (PUSH) + a coach line for what's newly worth showing. Stateful per
    route (show-once dedup) so the iPad can poll it; `active` is the full set for the banner,
    `spoken` is the LLM-phrased top of the NEW callouts (deterministic fallback)."""
    return copilot.make_narration(route=req.route, hoisted=req.hoisted, use_llm=req.use_llm)


@app.post("/narrate/reset")
def narrate_reset(req: NarrateRequest):
    """Clear the per-route show-once dedup state (a race / course change)."""
    return copilot.reset_narration(route=req.route)


@app.post("/dashboard")
def dashboard(req: DashboardRequest):
    """LLM commentary + grounded status nudges for the crew dashboard grid. The dashboard sends
    its current tiles; the LLM interprets them. Returns mode 'deterministic' on any LLM failure
    so the dashboard keeps its own engine-read commentary."""
    return dashboard_brief.make(req.tiles)


@app.post("/detail")
def detail(req: DetailRequest):
    """Streamed scoped explanation of one tile (the tap-to-detail deep-dive). Streams plain-text
    deltas token-by-token; empty stream if the LLM is unavailable (dashboard keeps its WHY)."""
    return StreamingResponse(dashboard_brief.detail_stream(req.domain, req.question, req.tiles),
                             media_type="text/plain")


@app.get("/coach")
def coach():
    """The proactive auto-coach state: the latest coach line + the active callouts + a short rolling
    history, produced by the on-Orin timer (no recompute here — cheap to poll). Honest about whether
    the timer is running and when it last ticked. This is the canonical PROACTIVE surface; POST
    /narrate is the on-demand/debug equivalent (don't poll both for the same route — they share the
    show-once dedup)."""
    return coach_mod.COACH.state()


@app.get("/adherence")
def adherence(route: str | None = None):
    """Playbook-adherence tile payload: are we on the frozen gameplan and has a branch trigger
    fired? Deterministic (no LLM); na when no playbook is aboard. The crew dashboard polls this for
    the PLAYBOOK-ADHERENCE tile."""
    return copilot.make_adherence(route=route)


@app.get("/snapshot")
def snapshot(route: str | None = None, hoisted: str | None = None):
    """The raw engine facts a brief would be built from — for debugging grounding."""
    return copilot.gather(EngineClient(), route or config.DEFAULT_ROUTE, hoisted)
