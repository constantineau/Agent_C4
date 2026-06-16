"""Agent_C4 agent service: REST health + tool introspection + WebSocket chat.

The web app opens a WebSocket to /ws and exchanges JSON messages with the shared crew
thread. Each inbound message runs the agent loop (agent.answer) in a threadpool because
the tools use a synchronous DB pool.
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from shared.tool_contracts import AGENT_TOOLS
from .db import pool
from . import agent, tools

API_KEY_PRESENT = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30)
    yield
    pool.close()


app = FastAPI(title="Agent_C4 Agent Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "llm": "live" if API_KEY_PRESENT else "fallback (no API key)"}


@app.get("/tools")
def list_tools():
    """Surface the tool contracts (handy for debugging Phase 0/4)."""
    return {"tools": [t["name"] for t in AGENT_TOOLS]}


@app.get("/conditions")
def conditions():
    """Compact best-value-per-channel for the web instrument strip."""
    return tools.get_strip()


@app.get("/conditions/full")
def conditions_full():
    """All sources per channel (the multi-source view the agent reasons over)."""
    return tools.get_current_conditions()


@app.get("/sources")
def sources():
    return tools.get_sources()


@app.get("/fatigue")
def fatigue():
    """Helm fatigue index (0–100) + components + rotation recommendation."""
    return tools.get_fatigue()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    history: list = []
    await websocket.send_json({"role": "system", "text": "Navigator online."})
    try:
        while True:
            msg = await websocket.receive_text()
            reply = await run_in_threadpool(agent.answer, msg, history)
            history.append({"role": "user", "content": msg})
            history.append({"role": "assistant", "content": reply})
            history[:] = history[-20:]  # cap thread memory
            await websocket.send_json({"role": "assistant", "text": reply})
    except WebSocketDisconnect:
        return
