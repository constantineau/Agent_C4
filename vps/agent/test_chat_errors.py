"""Cloud chat error handling and the strip's heading reference (#183-8, #183-9).

#183-9: one tool error used to abort the whole chat turn (dispatch had no try), the error path
showed the raw exception text to the crew, an unguarded fallback could raise out of `answer`,
a `max_tokens` cut-off was passed off as a finished answer, and an exception in the /ws reader
closed the chat with no message.

#183-8: both strips (cloud `tools.get_strip`, onboard `onboard_conditions.get_strip`) put a
magnetic heading in "heading" with no label when true heading was missing. They now say which
(`heading_ref`), and the chat readout names it.

No DB, no network, no real LLM: the anthropic client is a fake module.
Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_chat_errors.py
"""
import asyncio
import os
import sys
import types

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(os.path.dirname(_here))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ONBOARD_LIVE_WS", "false")

from app import agent, onboard_conditions, race_mode, tools  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


race_mode.audit_refusal = lambda *a, **k: None
REAL_STRIP = tools.get_strip          # kept for the heading checks below
race_mode._cache.clear()
race_mode._cache["mode"] = "practice"

# --- #183-9 tools.dispatch -------------------------------------------------------------------
print("dispatch: a failing tool returns an error result instead of raising:")
real_alerts = tools.DISPATCH["get_alerts"]
tools.DISPATCH["get_alerts"] = lambda **k: 1 / 0
try:
    out = tools.dispatch("get_alerts", {})
    raised = False
except Exception:
    out, raised = None, True
check("no exception escapes dispatch", not raised)
check("the result is marked failed", isinstance(out, dict) and out.get("tool_failed"))
check("the error names the tool and type only",
      out and "get_alerts" in out["error"] and "ZeroDivisionError" in out["error"])
check("an unknown tool is marked failed too", tools.dispatch("nope", {}).get("tool_failed"))


# --- a fake anthropic client -----------------------------------------------------------------
class Block:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


SCRIPT = []      # responses to hand out, in order
SEEN = []        # messages lists the model was given


class Messages:
    def create(self, **kw):
        SEEN.append(kw["messages"])
        return SCRIPT.pop(0)


class FakeClient:
    def __init__(self, **kw):
        self.messages = Messages()


sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeClient)
agent.API_KEY = "test-key"

print("LLM loop: a tool that throws becomes an is_error result and the turn goes on:")
SCRIPT[:] = [Resp("tool_use", [Block("tool_use", id="t1", name="get_alerts", input={})]),
             Resp("end_turn", [Block("text", text="All clear apart from the alerts tool.")])]
SEEN.clear()
out = agent.answer("anything wrong?")
check("the model's final answer reaches the crew", out == "All clear apart from the alerts tool.")
res = SEEN[-1][-1]["content"][0] if SEEN else {}
check("the tool result sent back is flagged is_error", res.get("is_error") is True)
check("...and carries the error, not data", "ZeroDivisionError" in res.get("content", ""))
tools.DISPATCH["get_alerts"] = real_alerts

print("LLM loop: a max_tokens cut-off is not passed off as finished:")
SCRIPT[:] = [Resp("max_tokens", [Block("text", text="The wind has shifted right and")])]
out = agent.answer("what's the wind doing?")
check("a cut-off answer says it was cut off", out.startswith("The wind has shifted right and")
      and "cut off" in out)

tools.get_strip = lambda *a, **k: {"available": True, "tws": 12, "twa": 45, "stw": 6.1, "sog": 6.0,
                                   "heading": 330, "heading_ref": "magnetic", "heel": 18,
                                   "data_age_seconds": 2}
SCRIPT[:] = [Resp("max_tokens", [Block("tool_use", id="t2", name="get_strip", input={})])]
out = agent.answer("how windy is it")
check("a cut-off with no text falls back to the direct readout", "TWS 12" in out)

print("error path: no raw exception text to the crew, and a failing fallback is caught:")


def boom(message, history):
    raise RuntimeError("secret-internal-detail at 10.0.0.5")


real_loop = agent._claude_loop
agent._claude_loop = boom
out = agent.answer("how windy is it")
check("the readout still answers", "TWS 12" in out)
check("the exception text is not shown", "secret-internal-detail" not in out and "10.0.0.5" not in out)

real_fallback = agent._fallback
agent._fallback = lambda m: 1 / 0
try:
    out = agent.answer("how windy is it")
    raised = False
except Exception:
    out, raised = None, True
check("LLM error + fallback error: answer() does not raise", not raised)
check("...and says the navigator is unavailable", out and agent.UNAVAILABLE in out)
agent.API_KEY = ""
try:
    out = agent.answer("how windy is it")
    raised = False
except Exception:
    out, raised = None, True
check("no key + fallback error: a plain line, no exception", not raised and out == agent.UNAVAILABLE)
agent._fallback = real_fallback

print("racing: the error path still goes through the race gate:")
race_mode._cache["mode"] = "race"
agent.API_KEY = "test-key"
reached = []
tools.get_fleet = lambda *a, **k: reached.append("get_fleet") or {"available": False}
out = agent.answer("who are we racing?")
check("LLM error + fleet question while racing → refusal, get_fleet not reached",
      race_mode.REFUSAL in out and not reached)
agent._claude_loop = real_loop
agent.API_KEY = ""
race_mode._cache["mode"] = "practice"

# --- #183-8 heading reference ----------------------------------------------------------------
print("heading: the strip names true vs magnetic:")


def chan(v):
    return {"preferred": {"value": v, "age_s": 1.0}, "freshest_age_s": 1.0}


def conds(channels):
    return lambda *a, **k: {"available": True, "as_of": "x", "channels": channels}


tools.get_strip = REAL_STRIP
tools.fatigue.compute_fatigue_index = lambda: {}
onboard_conditions.fatigue.compute_fatigue_index = lambda: {}
race_mode.racing = lambda: False
for name, mod in (("cloud", tools), ("onboard", onboard_conditions)):
    mod.get_current_conditions = conds({"heading_mag": chan(330.0)})
    s = mod.get_strip()
    check(f"{name}: magnetic only → heading 330 named magnetic",
          s["heading"] == 330.0 and s.get("heading_ref") == "magnetic")
    mod.get_current_conditions = conds({"heading_true": chan(338.0), "heading_mag": chan(330.0)})
    s = mod.get_strip()
    check(f"{name}: true wins and is named true", s["heading"] == 338.0 and s.get("heading_ref") == "true")
    mod.get_current_conditions = conds({"sog": chan(6.0)})
    s = mod.get_strip()
    check(f"{name}: no heading → no reference", s["heading"] is None and s.get("heading_ref") is None)

tools.get_strip = lambda *a, **k: {"available": True, "tws": 12, "twa": 45, "stw": 6.1, "sog": 6.0,
                                   "heading": 330, "heading_ref": "magnetic", "heel": 18,
                                   "data_age_seconds": 2}
check("chat readout labels a magnetic heading", "HDG 330°M" in agent._fallback("how windy is it"))

# --- #183-9 the /ws reader -------------------------------------------------------------------
print("/ws: a failed turn says so and the socket stays open:")
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

main.auth.verify_token = lambda tok: True
main.alerts.active_alerts = lambda: []
CALLS = {"n": 0}


def flaky_answer(msg, history):
    CALLS["n"] += 1
    if CALLS["n"] == 1:
        raise RuntimeError("first turn blew up")
    return f"answer to {msg}"


main.agent.answer = flaky_answer
client = TestClient(main.app)                 # no `with` → no lifespan, no DB, no alert loop
with client.websocket_connect("/ws?token=x") as ws:
    ws.receive_json()                         # "Navigator online."
    ws.send_text("q1")
    first = ws.receive_json()
    ws.send_text("q2")
    second = ws.receive_json()
check("the failed turn sends 'Navigator unavailable'",
      first.get("role") == "system" and first.get("text") == agent.UNAVAILABLE)
check("the next question still gets an answer", second.get("text") == "answer to q2")

print("/ws: if the session itself dies, the crew is told before it closes:")


class BrokenQueue(asyncio.Queue):
    def put_nowait(self, item):
        if item.get("role") == "assistant":
            raise RuntimeError("queue broke")
        return super().put_nowait(item)


main.hub.register = lambda: BrokenQueue()
main.hub.unregister = lambda q: None
with client.websocket_connect("/ws?token=x") as ws:
    ws.receive_json()
    ws.send_text("q3")
    last = ws.receive_json()
check("a dying session sends 'Navigator unavailable' first",
      last.get("role") == "system" and last.get("text") == agent.UNAVAILABLE)

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
