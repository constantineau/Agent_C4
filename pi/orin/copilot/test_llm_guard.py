"""The onboard LLM never overrides deterministic text or status unchecked (#53, #176).

The engine computes; the LLM may only rephrase. These pins hold that line on every LLM path:

  * coach line (narrate): a safety or "now" callout is the engine's own words; any other LLM line
    is used only if every number in it came from the callouts AND it keeps the top callout's numbers;
  * variant lookup: a right shift never points at the "left" variant just because its
    what_flips_it text mentions "right" (exact id match, same as the adherence tile);
  * /strategy: the engine's assessment and rationale stand; the LLM adds play ids + a grade only;
  * decision brief: a bare "playbook" citation grounds nothing; an item or situation with a number
    the facts don't have is dropped; the whale aside carries no numbers;
  * crew briefs: an LLM rewrite with a new number is thrown away, the deterministic body stays;
  * dashboard brief: the LLM can only RAISE a tile's status (ok → watch → act), never lower it or
    touch an n/a tile, and its focus/notes may not carry numbers the tiles don't have.

Pure: stub LLM + stub engine, no network.
    cd pi/orin && python3 -m copilot.test_llm_guard
"""
import json
import sys

from . import brief as brief_mod
from . import briefs as briefs_mod
from . import config
from . import copilot
from . import dashboard_brief
from . import ground
from . import narrate
from . import playbook as playbook_mod
from .llm import LLMUnavailable

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class FakeLLM:
    """Returns a canned reply; counts calls."""
    def __init__(self, content=""):
        self.content = content
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        c = self.content
        return {"content": c if isinstance(c, str) else json.dumps(c)}


def co(cid, cat, urg, head, detail):
    return narrate._callout(cid, cat, urg, head, detail, ["get_navigator"])


def test_ground():
    print("\n== ground: number check ==")
    check("12 == 12.0, sign-blind, names ignored",
          ground.grounded("Ease 12.0 kts at −60 min, J1 up", "12 kts; -60 min; sail J1"))
    check("a new number is caught", ground.ungrounded_numbers("CPA 0.3 nm", "CPA 0.5 nm") == {0.3})
    check("keeps_numbers needs every required number",
          ground.keeps_numbers("0.6 nm off", "0.6 nm right") and not ground.keeps_numbers("off line", "0.6 nm"))
    check("numbers keep their units: 0.6 nm / 9 min can't become 9 nm / 0.6 min",
          not ground.grounded("9 nm right; mark in 0.6 minutes", "0.6 nm right; mark in 9 minutes"))
    check("-60 min doesn't ground 60 kts", not ground.grounded("Wind now 60 kts", "Now 18 kts; -60 min 12 kts"))
    check("...nor 60-knot gusts, nor 60 mph",
          not ground.grounded("Prepare for 60-knot gusts", "Now 18 kts; -60 min 12 kts")
          and not ground.grounded("Gusts 60 mph", "Now 18 kts; -60 min 12 kts"))
    check("a hyphenated unit matching the source passes", ground.grounded("an 18-knot breeze", "Now 18 kts"))
    check("a plain word after a number isn't a unit", ground.grounded("Wind built 12 to 18 kts", "Now 18 kts; -60 min 12 kts"))
    check("a unit-less engine value grounds the same value with a unit",
          ground.grounded("TWS 12.4 kts from 180°", {"tws": 12.4, "twd": 180}))
    check("an invented number glued to a label (TWS30) is caught", not ground.grounded("Wind TWS30 kts", "Wind 12 kts"))
    check("a name the source has (J1) still passes", ground.grounded("J1 up", "sail J1"))
    check("a leading-dot decimal is a number (.9 nm is not 0.5 nm)",
          not ground.grounded("CPA .9 nm", "CPA 0.5 nm") and ground.grounded("CPA .9 nm", "CPA 0.9 nm"))


def test_coach_line():
    print("\n== coach line ==")
    dev = co("deviation:left:act", "deviation", "soon", "Off the playbook line",
             "0.6 nm right of variant left's optimal track")
    rnd = co("rounding:M2:10", "rounding", "soon", "M2 in ~9 min", "on a beat")

    llm = FakeLLM("Soon: Off the playbook line.")
    txt, mode = narrate.narrate([dev, rnd], llm)
    check("LLM line that drops the top callout's number → deterministic",
          mode == "deterministic" and "0.6 nm" in txt)

    llm = FakeLLM("Off the line by 0.9 nm. Mark in 9 minutes.")
    txt, mode = narrate.narrate([dev, rnd], llm)
    check("LLM line with a number not in the callouts → deterministic",
          mode == "deterministic" and "0.9" not in txt)

    llm = FakeLLM("You're 0.6 nm right of the plan line. M2 in about 9 minutes.")
    txt, mode = narrate.narrate([dev, rnd], llm)
    check("faithful rephrase is used", mode == "llm" and txt.startswith("You're 0.6 nm"))

    now = co("fatigue:rotate_now", "fatigue", "now", "Rotate the helm now",
             "fatigue index 0.82 (rotate_now) — the driver is degrading vs their own baseline")
    llm = FakeLLM("Rotate the helm now.")
    txt, mode = narrate.narrate([now, rnd], llm)
    check("a 'now' callout goes out word for word, LLM not asked",
          mode == "deterministic" and txt.startswith("Rotate the helm now — fatigue index 0.82")
          and llm.calls == 0)


def test_variant_lookup():
    print("\n== variant lookup ==")
    pb = playbook_mod.Playbook({"race_id": "r", "variants": [
        {"id": "left", "summary": "work the left", "what_flips_it": "A persistent right shift of 10°+"},
        {"id": "middle", "summary": "stay central", "what_flips_it": "Abandon middle if the wind shifts RIGHT"}]})
    tac = {"favored_side": "right", "wind": {"persistent": True, "now": 25, "mean_12min": 10}}
    c = narrate._tactics_callouts(tac, pb)
    check("right shift with no 'right' variant → plain shift callout, not 'variant left'",
          len(c) == 1 and "variant" not in c[0]["headline"] and c[0]["category"] == "shift"
          and "right side favored" in c[0]["detail"])
    pb_r = playbook_mod.Playbook({"race_id": "r", "variants": [
        {"id": "left", "what_flips_it": "a right shift"}, {"id": "right", "summary": "go right"}]})
    c = narrate._tactics_callouts(tac, pb_r)
    check("right shift with a 'right' variant → that variant",
          len(c) == 1 and c[0]["headline"] == "Consider playbook variant right")
    check("no playbook → no variant", narrate._tactics_callouts(tac, playbook_mod.Playbook(None))[0]["category"] == "shift")


def test_strategy():
    print("\n== /strategy ==")
    DIGEST = {"available": True, "mode": "deterministic", "assessment": "Hold: Middle start",
              "picture": [{"signal": "shift", "read": "osc ±4°", "grounded_in": ["get_tactics"]}],
              "concordance": {"strength": "none"}, "caveats": [],
              "recommendation": {"action": "Hold: Middle start", "vs_playbook": "on-plan",
                                 "target_variant": "middle", "rationale": "no decisive signal",
                                 "grounded_in": ["get_selector"], "urgency": "monitor"},
              "plays": {"detail": [{"id": "P1", "status": "armed"}]}}


    class StubEngine:
        base_url = "stub"

        def __init__(self, *a, **k):
            pass

        def strategy(self, route):
            return json.loads(json.dumps(DIGEST))


    strat_llm = FakeLLM({"assessment": "Tack now, 18 kts building left.",
                         "recommendation": {"action": "Tack now", "rationale": "tack now, it pays 40 m",
                                            "grounded_in": ["get_tactics"], "urgency": "now",
                                            "confidence": "high"},
                         "play_matches": [{"play_id": "P1", "match": "strong", "why": "tack now, 22 kts"},
                                          {"play_id": "ghost", "match": "strong", "why": "x"}]})
    orig = (copilot.EngineClient, copilot.LLMClient, playbook_mod.load)
    copilot.EngineClient = StubEngine
    copilot.LLMClient = lambda *a, **k: strat_llm
    copilot.playbook_mod.load = lambda: playbook_mod.Playbook(
        {"race_id": "r", "variants": [{"id": "middle"}], "plays": [{"id": "P1", "summary": "breeze left"}]})
    try:
        r = copilot.strategy_brief(route="t", use_llm=True)
    finally:
        copilot.EngineClient, copilot.LLMClient, playbook_mod.load = orig
    check("engine assessment kept", r["assessment"] == "Hold: Middle start")
    check("engine recommendation kept whole (rationale too)", r["recommendation"] == DIGEST["recommendation"])
    check("play matches: real ids, grade only, no free text",
          r.get("play_matches") == [{"play_id": "P1", "match": "strong", "status": "armed",
                                     "grounded_in": ["play:P1"]}])
    check("still marked as an LLM pass", r["mode"] == "llm")


def test_decision_brief():
    print("\n== decision brief ==")
    v = brief_mod.validate({"recommendations": [{"action": "Go left now, 18 kts building",
                                                 "grounded_in": ["playbook"], "urgency": "now",
                                                 "confidence": "high"}]},
                           {"get_conditions"}, [], [])
    check("a bare 'playbook' citation grounds nothing", v["recommendations"] == [])

    facts = {"get_conditions": {"tws": 12.4, "twd": 250}}
    v = brief_mod.validate({"situation": "Wind 30 kts from 250°.",
                            "factors": [{"factor": "TWS 12.4 kts", "grounded_in": ["get_conditions"]}],
                            "recommendations": [{"action": "Reef at 18 kts", "grounded_in": ["get_conditions"],
                                                 "urgency": "soon", "confidence": "high"}],
                            "confidence": "high"},
                           {"get_conditions"}, [], [], facts=facts)
    check("item with a number the facts don't have is dropped",
          v["recommendations"] == [] and len(v["factors"]) == 1)
    check("situation with an invented number is blanked", "30" not in v["situation"])
    check("confidence drops with a dropped item", v["confidence"] == "low")

    # The full loop grounds against the engine's facts only: the system prompt's worked example
    # ("from 250° to 265°") and the crew's typed question are not measurements.
    snap = {"get_conditions": {"tws": 12.4, "twd": 180}}
    reply = {"situation": "Wind shifted right to 265°.",
             "factors": [{"factor": "Wind 265°", "detail": "TWD 265", "grounded_in": ["get_conditions"],
                          "confidence": "high"}],
             "recommendations": [{"action": "Tack at 33 kts", "rationale": "TWS 33",
                                  "grounded_in": ["get_conditions"], "urgency": "soon", "confidence": "high"}]}
    try:
        copilot.llm_brief(None, FakeLLM(reply), snap, "Is 33 kts coming?", playbook_mod.Playbook(None))
        check("numbers from the prompt example / question don't ground a brief", False)
    except LLMUnavailable:
        check("numbers from the prompt example / question don't ground a brief", True)
    good = {"situation": "Wind 12.4 kts from 180°.",
            "factors": [{"factor": "TWS 12.4 kts", "detail": "from 180°", "grounded_in": ["get_conditions"],
                         "confidence": "high"}], "recommendations": []}
    out = copilot.llm_brief(None, FakeLLM(good), snap, "", playbook_mod.Playbook(None))
    check("a brief built from the engine's numbers passes", out["factors"] and "180" in out["situation"])

    w = copilot._whale_touch(FakeLLM("The breeze builds like a 40-ton humpback."), {"situation": "calm"})
    check("whale aside with a number is skipped", w is None)
    w = copilot._whale_touch(FakeLLM("The plush whale approves of this breeze."), {"situation": "calm"})
    check("whale aside without numbers still shows", w and "plush whale" in w)


def test_crew_brief():
    print("\n== crew brief rewrite ==")
    body = "Wind: 12 kts from 250°. Next: M2 in 9 min."
    try:
        briefs_mod._grounded_phrase("mark", "M2 in 9 min", body,
                                    FakeLLM({"headline": "M2 in 9 min", "body": "Wind 14 kts, M2 in 9 min."}))
        check("rewrite with a new number raises (deterministic kept)", False)
    except LLMUnavailable:
        check("rewrite with a new number raises (deterministic kept)", True)
    h, b = briefs_mod._grounded_phrase("mark", "M2 in 9 min", body,
                                       FakeLLM({"headline": "M2 in 9 min", "body": "12 kts from 250°; M2 in 9 min."}))
    check("faithful rewrite passes", b.startswith("12 kts"))


def test_dashboard_status():
    print("\n== dashboard brief (status may only go up) ==")
    TILES = [{"key": "bank", "name": "House Bank", "value": "11.9 V", "sub": "", "status": "act"},
             {"key": "wind", "name": "TWS Trend", "value": "Now 18 kts; -60 min 12 kts", "sub": "", "status": "ok"},
             {"key": "data", "name": "Data", "value": "5", "sub": "sources live", "status": "watch"},
             {"key": "fleet", "name": "Fleet", "value": "—", "sub": "", "status": "na"}]
    reply = {"focus": "Bank at 11.9 V, shed load.",
             "notes": [{"tile": "bank", "text": "Bank 11.9 V, charging — fine.", "confidence": "high"},
                       {"tile": "wind", "text": "Wind built 12 to 25 kts.", "confidence": "high"},
                       {"tile": "wind", "text": "Wind now 11.9 kts.", "confidence": "high"}],
             "adjust": [{"tile": "bank", "status": "ok", "reason": "charging"},
                        {"tile": "data", "status": "watch", "reason": "same"},
                        {"tile": "wind", "status": "watch", "reason": "building fast"},
                        {"tile": "data", "status": "act", "reason": "down to 11.9 sources"},
                        {"tile": "fleet", "status": "act", "reason": "?"}]}
    config_use = config.USE_LLM
    config.USE_LLM = True
    orig_llm = dashboard_brief.LLMClient
    dashboard_brief.LLMClient = lambda *a, **k: FakeLLM(reply)
    try:
        out = dashboard_brief.make(TILES)
    finally:
        dashboard_brief.LLMClient, config.USE_LLM = orig_llm, config_use
    adj = {a["tile"]: a["status"] for a in out.get("adjust", [])}
    check("act → ok on the bank is refused", "bank" not in adj)
    check("a no-op adjust is dropped", "data" not in adj)
    check("ok → watch on the wind is kept", adj.get("wind") == "watch")
    check("an n/a tile is never adjusted", "fleet" not in adj)
    check("note with an invented number dropped (also one borrowing the bank's 11.9), grounded note kept",
          [n["tile"] for n in out["notes"]] == ["bank"])
    check("a raise whose reason borrows another tile's number is dropped", "data" not in adj)
    check("focus with only tile numbers kept", out["focus"] == "Bank at 11.9 V, shed load.")


def main():
    for fn in (test_ground, test_coach_line, test_variant_lookup, test_strategy, test_decision_brief,
               test_crew_brief, test_dashboard_status):
        try:
            fn()
        except Exception as e:          # an old build without the guard crashes here: that is a FAIL
            check(f"{fn.__name__} ran ({type(e).__name__}: {e})", False)
    print("\n" + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
