"""FastAPI service for the onboard copilot (Tier 2). Runs on the Orin.

Endpoints:
  GET  /health    — reachability of the engine + LLM, playbook + model status
  GET  /tools     — the bounded tool surface the LLM is allowed (introspection)
  POST /brief      — produce a DecisionBrief for the current situation (+ optional question)
  GET  /snapshot  — raw gathered engine facts (debug / "show me what you saw")

There is intentionally no endpoint that takes an action — the copilot is read-only and advisory.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
                     "variants": len(pb.variants)},
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


@app.post("/dashboard")
def dashboard(req: DashboardRequest):
    """LLM commentary + grounded status nudges for the crew dashboard grid. The dashboard sends
    its current tiles; the LLM interprets them. Returns mode 'deterministic' on any LLM failure
    so the dashboard keeps its own engine-read commentary."""
    return dashboard_brief.make(req.tiles)


@app.get("/snapshot")
def snapshot(route: str | None = None, hoisted: str | None = None):
    """The raw engine facts a brief would be built from — for debugging grounding."""
    return copilot.gather(EngineClient(), route or config.DEFAULT_ROUTE, hoisted)
