"""#306: the coach's prompt names the boat it runs for and carries ITS speed guide, or none.

sr33 (the shore agent's BOAT_ID) keeps its wording: "SR33 AI Navigator", its ORC speed guide,
"J1 jib, A3 asymmetric, S1 symmetric kite". A boat with no profile or guide gets neutral words and
no guide, never sr33's. Exits non-zero on any failure.
"""
import os

os.environ["BOAT_ID"] = "sr33"
os.environ.pop("DATA_SOURCE", None)        # the shore agent

from app import agent  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


print("sr33 on the shore: its own words and guide")
blocks = agent.system_blocks()
text = blocks[0]["text"]
check("SR33 AI Navigator", text.startswith("You are the SR33 AI Navigator"))
check("its sails in words", "(J1 jib, A3 asymmetric, S1 symmetric kite)" in text)
check("its asym in the peel example", "the A3 is faster than the jib" in text)
check("judged against its speed guide", "the SR33 ORC Speed Guide provided below" in text)
check("the guide rides as a second, cached block", len(blocks) == 2
      and blocks[1]["text"].startswith("## SR33 ORC SPEED GUIDE (reference)\n\n# SR33")
      and blocks[1].get("cache_control"))

print("a boat with no profile and no guide: neutral words, no guide")
os.environ["BOAT_ID"] = "ci-boat"
try:
    blocks = agent.system_blocks()
finally:
    os.environ["BOAT_ID"] = "sr33"
text = blocks[0]["text"]
check("one block, no guide", len(blocks) == 1)
check("no SR33 anywhere", "sr33" not in text.lower())
check("says there is no speed guide", "no speed guide is loaded for this boat" in text)
check("plain 'AI Navigator'", text.startswith("You are the AI Navigator"))

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
