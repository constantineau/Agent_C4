"""Race-mode gate (app/race_mode.py): RRS 41, no off-boat tactical advice while racing.

Covers the pure decisions: fail-closed when the mode can't be read, which tools are withheld while
racing and allowed in practice, the no-LLM keyword fallback, and that a bad mode is refused before
anything is written. The DB writes themselves (`set_mode`, the audit rows) need Postgres and are not
covered here. No DB or network: the pool is never opened, which is the "unreadable" case.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_race_mode.py
"""
import importlib
import os

from app import race_mode as RM

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


TOOLS = [{"name": n} for n in ("get_current_conditions", "get_ais_targets", "get_alerts",
                               "fetch_forecast", "get_tactics", "get_route", "get_fleet",
                               "get_sail_advice", "get_navigator")]


def set_cached(mode):
    RM._cache.clear()
    if mode:
        RM._cache["mode"] = mode


print("fail-closed:")
os.environ.pop("RACE_MODE_DEFAULT", None)
RM = importlib.reload(RM)
check("no RACE_MODE_DEFAULT → default is race", RM.DEFAULT_MODE == "race")
os.environ["RACE_MODE_DEFAULT"] = "Bogus"
RM = importlib.reload(RM)
check("a bogus default → race", RM.DEFAULT_MODE == "race")
os.environ["RACE_MODE_DEFAULT"] = " Practice "
RM = importlib.reload(RM)
check("RACE_MODE_DEFAULT=practice is honoured (dev compose)", RM.DEFAULT_MODE == "practice")
os.environ.pop("RACE_MODE_DEFAULT", None)
RM = importlib.reload(RM)
set_cached(None)
check("DB unreadable → racing (withhold)", RM.current_mode() == "race" and RM.racing())
check("…and the answer is cached", RM._cache.get("mode") == "race")

print("racing:")
set_cached("race")
check("tactics withheld", RM.is_gated("get_tactics"))
check("every gated tool withheld", all(RM.is_gated(t) for t in RM.GATED_TOOLS))
check("own instruments, AIS, alerts and the public forecast still allowed",
      not any(RM.is_gated(t) for t in ("get_current_conditions", "get_ais_targets", "get_alerts",
                                        "fetch_forecast")))
allowed = [t["name"] for t in RM.allowed_tools(TOOLS)]
check(f"tool list filtered to {allowed}", allowed == ["get_current_conditions", "get_ais_targets",
                                                        "get_alerts", "fetch_forecast"])
check("tactical questions are caught by the fallback",
      all(RM.gated_intent(q) for q in ("which side is favoured?", "when do we peel to the a3",
                                       "what's our eta to the finish", "are we lifted")))
check("safety and instrument questions are not",
      not any(RM.gated_intent(q) for q in ("any ships near us?", "what's the depth",
                                           "how windy is it")))
check("the refusal names RRS 41 and Practice", "RRS 41" in RM.REFUSAL and "Practice" in RM.REFUSAL)

print("practice:")
set_cached("practice")
check("nothing gated", not any(RM.is_gated(t) for t in RM.GATED_TOOLS) and not RM.racing())
check("all tools allowed, same list", RM.allowed_tools(TOOLS) is TOOLS)

print("set_mode refuses a bad mode before touching the DB:")
for bad in ("", None, "racing", "off"):
    try:
        RM.set_mode(bad)
        check(f"set_mode({bad!r}) raises", False)
    except ValueError:
        check(f"set_mode({bad!r}) → ValueError", True)
check("…and the mode did not change", RM.current_mode() == "practice")

print("audit_refusal never raises into the caller:")
try:
    RM.audit_refusal("chat", tool="get_tactics")
    check("DB down → swallowed", True)
except Exception as exc:  # noqa: BLE001
    check(f"DB down → raised {type(exc).__name__}", False)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
