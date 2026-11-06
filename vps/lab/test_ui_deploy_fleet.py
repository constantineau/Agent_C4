"""UI grill fixes on the Lab's Deploy, Fleet, Gameplan and Learnings tabs (#98 #99 #100 #101).

Static checks on the page source: the Lab web UI has no browser test rig in CI, so each check pins
the markup a finding was about. Run: python3 vps/lab/test_ui_deploy_fleet.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
JS = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(HERE, "web", "styles.css"), encoding="utf-8").read()
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("[OK ] " if cond else "[FAIL] ") + name)


def fn(name):
    """Source of a top-level function, from its header to the next top-level function."""
    m = re.search(r"^(?:async )?function " + name + r"\(.*?(?=^(?:async )?function |\Z)", JS, re.S | re.M)
    return m.group(0) if m else ""


def css_rule(sel):
    m = re.search(r"(?:^|\})\s*" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


deploy, panel = fn("paintDeploy"), fn("deployPanel")
print("#98 G22 the hand-run commands have a Copy button:")
check("a Copy button next to the commands", 'onclick="depCopyCmds(this)"' in panel and 'id="depCmds"' in panel)
copy = fn("depCopyCmds")
check("copy tries the clipboard, then falls back to selecting the block",
      "navigator.clipboard.writeText" in copy and "selectNodeContents" in copy and 'execCommand("copy")' in copy)

print("#98 G68 only Push to boat is the red button:")
check("both downloads are mini buttons",
      '<button class="mini" onclick="downloadPlaybook(' in panel
      and '<button class="mini" onclick="downloadHomework(' in panel)
check("Re-lock is a mini button", 'id="depLock" class="${lock ? "mini" : ""}"' in deploy)
check("Push to boat stays a bare (red) button", '<button id="depPush" onclick="depPush()"' in panel)

print("#98 G70 Re-lock is off when the selection is already locked:")
check("isLocked compares the selection with the lock", "Dep.sel === lock.playbook_id" in deploy)
check("a lock problem or a newer signature keeps Re-lock on",
      "!r.lock_problem" in deploy and "selPb.signed_at" in deploy)
check("the button is disabled and says so", "Dep.busy || isLocked" in deploy and "✓ Locked in" in deploy)

guns, glance, fleet = fn("fleetGunsCard"), fn("fleetGlance"), fn("paintFleet")
print("#99 G25 the roster isn't pushed below the fold:")
check("Import folds into <details> once a roster exists",
      'roster.length\n      ? `<details class="card"' in fleet and "Lab.fleetImportOpen" in fleet)
check("an empty roster still shows Import open", '<h3>Import the fleet</h3>' in fleet)
check("the import message stays outside the fold",
      -1 < fleet.find("Lab.fleetMsg ?") < fleet.find('<details class="card" ${Lab.fleetImportOpen'))
check("start times fold once all are set", "if (set === divs.length)" in guns and "Lab.fleetGunsOpen" in guns)

print("#99 G72 our division says (us), not ◆:")
check("no ◆", "◆" not in guns)
check("(us)", '" (us)"' in guns)

print("#99 G74 handicap fields name their quantity; scoring shown separately:")
check("our GPH has units", "ORC GPH (s/nm)" in fleet)
check("our rating is the ORC ToT coefficient", "ORC ToT coefficient" in fleet)
check("no bare 'Rating' label or column", ">Rating " not in fleet and "<th>Rating</th>" not in fleet + glance)
check("roster columns read ToT and GPH (s/nm)",
      "<th>ToT</th>" in fleet and "<th>GPH (s/nm)</th>" in fleet and "<th>ToT</th>" in glance)
check("the race's scoring method shows under our boat", "Race scoring: <b>${esc(sc.method" in fleet)

game = fn("renderGameplan")
print("#100 G30 routing controls explain with a tap, not a hover:")
for txt in ("Slows the boat in waves", "Includes water current", "Also routes each weather model",
            "Fine is sharper near shore", "More scenarios cover more weather"):
    check(f"'{txt}…' is a ? toggle, not a title", f'qhelp("{txt}' in game and f'title="{txt}' not in game)

print("#100 G31 Start names the zone and echoes UTC:")
check("label shows the zone's short name", 'Start (<span id="optStartTz">${esc(optStartZone())}</span>)' in game)
check("no 'race local' in the Start label", "race local" not in game)
abbr = fn("tzAbbr")
check("tzAbbr uses the short zone name", 'timeZoneName: "short"' in abbr)
note = fn("optStartNote")
check("a picked time echoes its UTC", "optStartEpoch()" in note and 'UTC. ' in note)
check("the gun-time note names the zone too", "tzAbbr(tz, e)" in note and "race local" not in note)
check("typing updates the zone label", "optStartTz" in fn("optStartInput"))

card = fn("boatModelCard")
print("#101 G32 the toss-up marker leaves the sail labels clear:")
check("no ≈ glyph drawn on the track", '">≈</div>' not in card)
check("toss-up tracks get room for the strip", 'class="xo-track xo-tu"' in card)
ol = css_rule(".xo-ol")
check("the strip sits on the bottom edge, thin", "bottom: 0" in ol and "height: 5px" in ol and "top: 0" not in ol)
check("zones on those tracks stop above the strip", "calc(100% - 5px)" in css_rule(".xo-tu .xo-zone"))
check("the plain jib bar keeps full-height zones", "height: 100%" in css_rule(".xo-zone"))

print("#101 G34 the polar grid folds away:")
grid = fn("renderPolarGrid")
check("grid is inside a closed-by-default <details>",
      '<details class="pg-fold" ${Opt.polarGridOpen ? "open" : ""}' in grid)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
