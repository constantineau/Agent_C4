"""#48.1: the no-LLM chat fallback must not leak gated advice while racing (RRS 41).

The old fallback refused only on a keyword list, which missed fleet/rival/corrected/handicap,
recap/debrief/summary and "tack", so those questions reached get_fleet(), make_debrief() and
get_navigator() while racing. The same fallback also runs after any LLM error. Now every gated call
in the fallback is checked right before it runs.

Every tool is stubbed: a gated one records that it was reached. No DB, no network, no LLM.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_chat_race_gate.py
"""
import sys

from app import agent, race_mode, summarizer, tools

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


reached = []
audited = []


def gated_stub(name):
    def f(*a, **k):
        reached.append(name)
        return {"available": False, "note": f"{name} ran"}
    return f


for name in race_mode.GATED_TOOLS:
    setattr(tools, name, gated_stub(name))
summarizer.make_summary = gated_stub("make_summary")
summarizer.make_debrief = gated_stub("make_debrief")

tools.get_alerts = lambda *a, **k: {"count": 0, "alerts": []}
tools.get_ais_targets = lambda *a, **k: {"count": 0, "targets": []}
tools.fetch_forecast = lambda *a, **k: {"available": False, "note": "forecast stub"}
tools.get_sources = lambda *a, **k: {"count": 0, "sources": []}
tools.get_strip = lambda *a, **k: {"available": True, "tws": 12, "twa": 45, "stw": 6.1, "sog": 6.0,
                                   "heading": 200, "heel": 18, "data_age_seconds": 2}
race_mode.audit_refusal = lambda channel, **d: audited.append((channel, d))


def set_mode(mode):
    race_mode._cache.clear()
    if mode:
        race_mode._cache["mode"] = mode


# Questions that the old keyword list let through to a gated call.
LEAKS = [
    "who are we racing?",
    "how are the rivals doing",
    "are we ahead on corrected time",
    "handicap standings",
    "give me a recap",
    "debrief the last hour",
    "summarize the session",
    "how did we do",
    "should we tack now",
    "can we beat Windigo",
    "who else is on the water",
]
# Plus the ones the keyword list already caught, to keep them caught.
OLD_CAUGHT = ["which side is favoured?", "when do we peel to the a3", "eta to the finish",
              "how tired is the helm", "are we on target", "best route home"]
ALLOWED = ["any alerts?", "ais traffic near us", "forecast?", "which sensor sources are up",
           "how windy is it"]

print("racing — every gated question refused, no gated call reached:")
set_mode("race")
for q in LEAKS + OLD_CAUGHT:
    reached.clear()
    out = agent._fallback(q)
    check(f"{q!r} → refusal, nothing gated ran", out == race_mode.REFUSAL and not reached)

print("racing — unknown mode is treated as racing (fail-closed):")
set_mode(None)
race_mode.pool = None  # any DB read raises → fail-closed default
race_mode.DEFAULT_MODE = "race"
reached.clear()
check("'who are we racing?' with no mode set → refusal",
      agent._fallback("who are we racing?") == race_mode.REFUSAL and not reached)

print("racing — safety, own data and the public forecast still answered:")
set_mode("race")
for q in ALLOWED:
    reached.clear()
    out = agent._fallback(q)
    check(f"{q!r} → answered ({out[:40]!r})", out != race_mode.REFUSAL and not reached)

print("racing — refusals are audited:")
audited.clear()
agent._fallback("give me a recap")
check("a refusal writes an audit row on the chat_fallback channel",
      audited and audited[0][0] == "chat_fallback")

print("racing — the LLM-error path falls back through the same gate:")


def boom(message, history):
    raise RuntimeError("api down")


agent.API_KEY, agent._claude_loop = "test-key", boom
reached.clear()
out = agent.answer("who are we racing?")
check("LLM error + fleet question → refusal, get_fleet not reached",
      race_mode.REFUSAL in out and not reached)
agent.API_KEY = ""

print("practice — gated questions reach their tools:")
set_mode("practice")
for q, tool in [("who are we racing?", "get_fleet"), ("give me a recap", "make_debrief"),
                ("summarize", "make_summary"), ("should we tack now", "get_navigator"),
                ("which side is favoured?", "get_tactics"), ("are we on target", "get_polar_target")]:
    reached.clear()
    agent._fallback(q)
    check(f"{q!r} → {tool}", reached == [tool])

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
