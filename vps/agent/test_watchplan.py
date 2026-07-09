"""Watch plan — unit test for the generator, the status resolver and the live quick edits.
Pure stdlib, no engine needed.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_watchplan.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared import watchplan as wp

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

T0 = 1_800_000_000.0          # arbitrary anchor epoch
H = 3600.0

# --- generator ----------------------------------------------------------------------------------
print("generator:")
blocks = wp.generate(T0, 24, "4on4off")
check("4on4off covers 24h in 6 blocks", len(blocks) == 6 and blocks[-1]["end"] == T0 + 24 * H)
check("teams alternate", [b["on"] for b in blocks] == ["A", "B", "A", "B", "A", "B"])
sw = wp.generate(T0, 48, "swedish", first_on="B")
check("swedish cycle: 5 blocks / 24h, first team B", sw[0]["on"] == "B" and sw[4]["end"] == T0 + 24 * H)
check("swedish alternation flips night slots day 2", sw[0]["on"] != sw[5]["on"])
check("custom hours list works", len(wp.generate(T0, 12, [6, 6])) == 2)
check("bad inputs -> []", wp.generate(None, 24, "4on4off") == [] and wp.generate(T0, 0, [4]) == [])

# --- normalize ----------------------------------------------------------------------------------
print("normalize:")
raw = {"teams": {"A": {"name": " Port ", "members": ["Al", " "]}, "B": {}},
       "blocks": [{"start": T0 + 4 * H, "end": T0 + 8 * H, "on": "b"},
                  {"start": T0, "end": T0 + 5 * H, "on": "A"},        # overlaps the one above
                  {"start": "junk", "end": T0, "on": "A"},             # dropped
                  {"start": T0 + 8 * H, "end": T0 + 8 * H, "on": "A"}]}  # zero-length, dropped
p = wp.normalize(raw)
check("teams coerced", p["teams"]["A"]["name"] == "Port" and p["teams"]["A"]["members"] == ["Al"]
      and p["teams"]["B"]["name"] == "B")
check("bad blocks dropped, sorted", len(p["blocks"]) == 2 and p["blocks"][0]["start"] == T0)
check("overlap clipped (later start wins)", p["blocks"][0]["end"] == T0 + 4 * H)
check("on upper-cased", p["blocks"][1]["on"] == "B")
check("garbage -> empty plan", wp.normalize("nope")["blocks"] == [])

# --- status_at ----------------------------------------------------------------------------------
print("status_at:")
plan = wp.normalize({"teams": {"A": {"name": "Port"}, "B": {"name": "Stbd"}},
                     "blocks": wp.generate(T0, 24, "4on4off")})
st = wp.status_at(plan, T0 + 1 * H)
check("mid-block: active, A on", st["active"] and st["on"] == "A" and st["on_label"] == "Port")
check("countdown to the block end", st["mins_to_change"] == 180.0 and st["next_change"] == T0 + 4 * H)
check("next team resolved through contiguous blocks", st["next_on"] == "B" and st["next_on_label"] == "Stbd")
check("upcoming lists later blocks", len(st["upcoming"]) == 3 and st["upcoming"][0]["on"] == "B")
before = wp.status_at(plan, T0 - H)
check("before the plan: inactive, next = first block", not before["active"]
      and before["next_change"] == T0 and before["next_on"] == "A")
after = wp.status_at(plan, T0 + 30 * H)
check("after the plan: inactive, no change", not after["active"] and after["next_change"] is None)
gap = wp.normalize({"blocks": [{"start": T0, "end": T0 + H, "on": "A"},
                               {"start": T0 + 2 * H, "end": T0 + 3 * H, "on": "B"}]})
gst = wp.status_at(gap, T0 + 0.5 * H)
check("gap after current block: change = block end, next team unknown",
      gst["next_change"] == T0 + H and gst["next_on"] is None)

# --- quick edits --------------------------------------------------------------------------------
print("edits:")
held = wp.hold(plan, T0 + 1 * H, 60)
hst = wp.status_at(held, T0 + 1 * H)
check("hold extends the current block", hst["next_change"] == T0 + 5 * H)
check("hold shifts later blocks intact", held["blocks"][1]["start"] == T0 + 5 * H
      and held["blocks"][-1]["end"] == T0 + 25 * H)
check("hold logged", held["log"][-1]["action"] == "hold")
check("hold outside any block is a no-op", wp.hold(plan, T0 - H, 60)["blocks"] == plan["blocks"])

sw2 = wp.swap(plan, T0 + 1 * H)
check("swap flips current + later teams", [b["on"] for b in sw2["blocks"][:3]] == ["B", "A", "B"])
check("swap leaves past blocks alone", wp.swap(plan, T0 + 5 * H)["blocks"][0]["on"] == "A")

ah = wp.all_hands(plan, T0 + 3.5 * H, 60)
ast = wp.status_at(ah, T0 + 3.75 * H)
check("all-hands active", ast["all_hands"] and ast["on"] == "ALL")
check("all-hands splits the blocks it overlaps",
      any(b["on"] == "A" and abs(b["end"] - (T0 + 3.5 * H)) < 1 for b in ah["blocks"])
      and any(b["on"] == "B" and abs(b["start"] - (T0 + 4.5 * H)) < 1 for b in ah["blocks"]))
check("apply_edit dispatch", wp.apply_edit(plan, "swap", T0 + H)["blocks"][0]["on"] == "B"
      and wp.apply_edit(plan, "bogus", T0)["blocks"] == plan["blocks"])

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
