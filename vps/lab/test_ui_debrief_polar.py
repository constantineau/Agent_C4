"""Debrief + Polar tab fixes from the UI grill (#81, #97 G61-G66, #103 G39 G40 G88 G90 G91, #240 sails).

The Lab page is plain browser JS with no test runner in the Lab image, so these read app.js and pin
the shape of each fix: each check fails on the code before it.

No network. Run in-container (make ci): python3 /repo/vps/lab/test_ui_debrief_polar.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JS = open(os.path.join(HERE, "web", "app.js")).read()
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def fn(name):
    """The source of a top-level `function name(` / `async function name(` up to the next one."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", JS, re.M)
    if not m:
        return ""
    nxt = re.search(r"^(?:async )?function \w+\(|^const \w+ = |^let \w+ = ", JS[m.end():], re.M)
    return JS[m.start(): m.end() + (nxt.start() if nxt else len(JS))]


print("#81 Re-run asks before adding another run:")
run = fn("debRunRecording")
check("debRunRecording asks with askModal when the recording is already debriefed",
      "x.debrief" in run and "askModal(" in run and run.index("askModal(") < run.index("apiPost("))
check("cancel stops before anything is posted", re.search(r'if \(go !== "yes"\) return;', run))
card = fn("debRecordingsCard") + fn("debRecRows")   # the rows are shared with Practice sails (#322)
check("Open sits at least 8 px from Re-run", "margin-left:8px" in card)

print("#97 Debrief:")
check("G61 the YB race id box is a text input (dark style applies)",
      re.search(r'<input type="text" id="retroYb"', JS))
paint = fn("paintDebrief")
check("G62 playbook headline is no longer cut at 56 characters", ".slice(0, 56)" not in paint)
clip = fn("clipText")
check("G62 a cut headline ends in an ellipsis", clip and "…" in clip and "clipText(b.headline" in paint)
check("G63 the LOG button note is inline text, not a hover title",
      "button said" not in card and "LOG button stopped at" in card)
check("G64 debrief dates are YYYY-MM-DD", "isoDate(d.created_at)" in card
      and "toISOString().slice(0, 10)" in fn("isoDate"))
for name in ("learnTrendCard", "learnArchiveCard"):
    check(f"G64 {name} uses the same date format", "toLocaleDateString" not in fn(name) and "isoDate(" in fn(name))
render = fn("renderDebrief")
check("G65 the tab opens the newest archived debrief on load",
      "debLatestId()" in render and "debOpenDebrief(latest)" in render)
latest = fn("debLatestId")
check("G65 'newest' is by created_at", "created_at" in latest)
check("G65 the row on screen is marked", "onScreen" in card and "showing" in card)
check("G65 a late open does not paint over another tab", 'if (!stale("debrief")) paintDebrief();' in fn("debOpenDebrief"))
opn = fn("debOpenDebrief")
check("G65 an open answer for a race or debrief the user left is dropped",
      "Deb.raceId !== race" in opn and "Deb.viewing !== id" in opn and opn.count("if (moved()) return;") == 2)
no_race = render[:render.index("Deb.playbooks")]
check("G66 no race no longer returns a bare placeholder", "Add one in the Races tab.</div>'; return;" not in no_race)
check("G66 no race still loads and paints Fleet retro", "retroLoad()" in no_race and "paintDebrief()" in no_race)
check("G66 the empty state links to Races and keeps the retro card",
      re.search(r'if \(!Deb\.raceId\) \{[^}]*href="#races".*?retroCard\(\)', paint, re.S))

print("#240 Polar sails use the crew names and the chart's colours:")
check("the display order is by sail family, never a fixed list of one boat's names (#305)",
      "const POL_FAMILY_ORDER" in JS and "polOrder" in JS and "const POL_ORDER" not in JS)
check("logged S2 is pooled as S1 on the server, so the page has no S2 rename of its own",
      "polSailName" not in JS and "(logged " not in JS)
check("sail colours come from SAIL_COLORS (one colour per sail across the Lab)",
      "SAIL_COLORS[t]" in fn("polSailHex") and "POL_HUES" not in JS)
check("SS falls back to the staysail pink until SAIL_COLORS has it", 'POL_SS = "#f28cc4"' in JS)
check("a two-sail set draws its second sail dashed in that sail's colour",
      "sr.color2" in fn("polSvg") and "stroke-dasharray" in fn("polSvg"))
check("one cell per sail and angle, so the line goes through every point (no pick-the-bigger)",
      "polLinePts" not in JS and "sr.pts.map(" in fn("polSvg"))
paintp = fn("paintPolar")
check("legend and filter list the sails in the crossover chart's order", "polConfigNames()" in paintp
      and "d.configs || []" not in paintp)

print("#103 Polar chart:")
svg = fn("polSvg")
check("G39 tips open on tap, not only mouse hover", "onclick=\"polTipTap(" in svg and "onmousemove" not in svg)
check("G39 hit area is about 22 (finger size)", re.search(r'class="pol-hit"[^>]*r="22"', svg))
check("G39 overlapping hit areas pick the nearest point", "polNearest(ev, i)" in fn("polTipTap"))
check("G39 a tap elsewhere closes the tip", re.search(r'addEventListener\("pointerdown"[^\n]*\n[^\n]*pol-hit', JS))
check("G40 no inline curve labels (the legend names every line)", "esc(sr.label)" not in svg)
check("G88 ring speeds are no longer printed on the 90° spoke", "${CY - 4}" not in svg
      and re.search(r'x="\$\{CX - 4\}" y="\$\{CY - rr\(v\) \+ 4\}"', svg))
vb = re.search(r'viewBox="([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)"', svg)
check("G90 the viewBox is cropped to the half circle (narrower than 640)",
      vb and float(vb.group(3)) <= 400 and float(vb.group(1)) > 0)
check("G91 speeds print with 2 decimals", "toFixed(2)" in fn("kn2")
      and "kn2(c.stw)" in paintp and "kn2(c.median_stw)" in paintp)
check("G91 speed headers say kn", "best held (kn)" in paintp and "typical (kn)" in paintp)

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
