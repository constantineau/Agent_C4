"""Race ingest: a model reply with a JSON slip is retried once, then fails plainly.
No network or API key. Run (make ci): PYTHONPATH=vps/lab python3 vps/lab/test_extract_retry.py"""
import sys

from app import extract, llm

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


GOOD = '{"race_id": "sorc-2027", "name": "Islands in the Stream"}'
BAD = '{"race_id": "sorc-2027", "name": }'      # the kind of slip seen 2026-11-02


def fake(replies):
    calls = []

    def complete(system, user, max_tokens=4000):
        calls.append(user)
        return replies[len(calls) - 1], "fake-model"
    llm.complete = complete
    return calls


extract.API_KEY = "test"
DOCS = [("nor.pdf", "NOTICE OF RACE ...")]

print("one bad reply, then a good one: retried and parsed")
calls = fake([BAD, GOOD])
d = extract.extract_race_definition(DOCS, "2026-11-02")
check("the definition comes back", d.get("race_id") == "sorc-2027")
check("exactly two calls", len(calls) == 2)
check("the retry says what was wrong", "not valid JSON" in calls[1] and "NOTICE OF RACE" in calls[1])

print("a good reply first: one call only")
calls = fake([GOOD])
check("parsed", extract.extract_race_definition(DOCS)["name"] == "Islands in the Stream")
check("one call", len(calls) == 1)

print("two bad replies: a plain error, no third call")
calls = fake([BAD, BAD, GOOD])
try:
    extract.extract_race_definition(DOCS)
    raised = ""
except RuntimeError as e:
    raised = str(e)
check("fails with a plain message", "wasn't valid JSON twice" in raised and "Try again" in raised)
check("stopped at two calls", len(calls) == 2)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
