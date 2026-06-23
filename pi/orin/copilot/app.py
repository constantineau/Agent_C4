"""FastAPI service for the onboard copilot (Tier 2). Runs on the Orin.

Endpoints:
  GET  /health    — reachability of the engine + LLM, playbook + model status
  GET  /tools     — the bounded tool surface the LLM is allowed (introspection)
  POST /brief      — produce a DecisionBrief for the current situation (+ optional question)
  POST /narrate    — proactive crew callouts + a spoken line for what's newly worth saying (PUSH)
  POST /narrate/reset — clear the per-route speak-once dedup (a race / course change)
  GET  /adherence — playbook-adherence tile (on-plan / branch-trigger-fired; deterministic)
  GET  /snapshot  — raw gathered engine facts (debug / "show me what you saw")

There is intentionally no endpoint that takes an action — the copilot is read-only and advisory.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config, copilot, dashboard_brief, playbook as playbook_mod, tools
from .engine_client import EngineClient
from .llm import LLMClient

app = FastAPI(title="Agent_C4 Onboard Copilot", version="0.1.0")
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
    }


@app.get("/tools")
def list_tools():
    """The complete, closed set of capabilities the LLM has — nothing else is callable."""
    return {"tools": [t["function"]["name"] for t in tools.TOOL_SPECS], "specs": tools.TOOL_SPECS}


@app.post("/brief")
def brief(req: BriefRequest):
    return copilot.make_brief(question=req.question, route=req.route,
                              hoisted=req.hoisted, use_llm=req.use_llm)


@app.post("/narrate")
def narrate(req: NarrateRequest):
    """Proactive crew callouts (PUSH) + a spoken line for what's newly worth saying. Stateful per
    route (speak-once dedup) so the iPad can poll it; `active` is the full set for the banner,
    `spoken` is the LLM-phrased top of the NEW callouts (deterministic fallback)."""
    return copilot.make_narration(route=req.route, hoisted=req.hoisted, use_llm=req.use_llm)


@app.post("/narrate/reset")
def narrate_reset(req: NarrateRequest):
    """Clear the per-route speak-once dedup state (a race / course change)."""
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
