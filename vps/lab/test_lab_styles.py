"""Lab styles.css checks for the UI grill Batch 0 fixes (#81 #84 #91 #97-#105).

Plain script: prints [OK ]/[FAIL] lines, exits 1 on any failure. Parses styles.css into
(media, selectors, declarations) rules with a tiny CSS reader (no nesting beyond one @media level).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "web", "styles.css")).read()
JS = open(os.path.join(HERE, "web", "app.js")).read()

fails = 0


def check(cond, msg):
    global fails
    print(("[OK ] " if cond else "[FAIL] ") + msg)
    if not cond:
        fails += 1


def split_selectors(sel):
    """Split a selector list on top-level commas (not inside parentheses)."""
    out, depth, cur = [], 0, ""
    for ch in sel:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [re.sub(r"\s+", " ", s) for s in out]


def parse(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, i, media = [], 0, None
    depth_media = 0
    buf = ""
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = buf.strip()
            buf = ""
            if head.startswith("@media"):
                media = head
                depth_media = 1
                i += 1
                continue
            j = css.index("}", i)
            body = css[i + 1:j]
            decls = {}
            for d in body.split(";"):
                if ":" in d:
                    k, v = d.split(":", 1)
                    decls[k.strip()] = re.sub(r"\s+", " ", v.strip())
            rules.append((media, split_selectors(head), decls))
            i = j + 1
            continue
        if ch == "}" and depth_media:
            media, depth_media, buf = None, 0, ""
            i += 1
            continue
        buf += ch
        i += 1
    return rules


RULES = parse(CSS)


def decl(selector, prop, media=None):
    """Last value of prop across rules whose selector list contains `selector` exactly."""
    val = None
    for m, sels, d in RULES:
        if m == media and selector in sels and prop in d:
            val = d[prop]
    return val


# --- touch targets: 44 px under (any-pointer: coarse) (#81, #84, #104 G45) ---
coarse = [r for r in RULES if r[0] and "any-pointer: coarse" in r[0]]
check(bool(coarse), "an @media (any-pointer: coarse) block exists")
coarse_44 = set()
for _, sels, d in coarse:
    if d.get("min-height") == "44px":
        coarse_44.update(sels)
for s in (".tabs a", ".ghost", "button.mini", ".cin", ".ein", "td select", ".jib-edit input",
          ".optchk", ".req-flag", ".req-chk", ".echk"):
    check(s in coarse_44, f"coarse pointer: {s} gets min-height 44px")
check(any("label:has(> input:is([type=checkbox], [type=radio]))" in s for s in coarse_44),
      "coarse pointer: a label wrapping a checkbox/radio is a 44 px target (whole label clickable)")
check(not any("(pointer: coarse)" in (m or "") for m, _, _ in RULES),
      "uses any-pointer, not pointer (an iPad with a trackpad still gets 44 px)")

# --- checkbox/radio beside its words, not above (#100 G28, #81 G38, #103 G89) ---
for s in (".opt-controls label:has(> input:is([type=checkbox], [type=radio]))",
          ".opt-group label.optchk", ".opt-controls label.req-flag"):
    check(decl(s, "flex-direction") == "row", f"{s} lays out as a row")

# --- white text on the red buttons (#100 G79, #104 G95) ---
for s in (".gatebox button", "#ingBtn", "#saveCourseBtn", "#detailSaveBtn", "#optRun"):
    check(decl(s, "color") == "var(--ink-on-accent)", f"{s} text is --ink-on-accent, not near-black")

# --- a button.pill is not the red CTA (#97 G21) ---
cta = [sels for _, sels, d in RULES if any(x.startswith("main button:not(.mini)") for x in sels)]
check(len(cta) >= 3 and all(":not(.pill)" in x for sels in cta for x in sels),
      "every main-button CTA rule excludes .pill")

# --- dark inputs everywhere (#97 G61, #99 G71, #104 G93) ---
check(decl(":root", "color-scheme") == "dark", ":root sets color-scheme: dark")
glob = [d for m, sels, d in RULES if m is None and any(x.startswith(":where(input") and "textarea" in x for x in sels)]
check(bool(glob) and glob[-1].get("background") == "var(--panel2)",
      "a zero-specificity input/textarea/select rule gives bare fields the dark panel background")

# --- S1 / SS colours: same sail, same colour in chart (JS) and badges/legend (CSS) (#91) ---
m = re.search(r"const SAIL_COLORS = \{([^}]*)\}", JS)
js_cols = dict(re.findall(r"(\w+):\s*\"(#[0-9a-fA-F]{6})\"", m.group(1))) if m else {}
for sail in ("S1", "SS"):
    css_bg = decl(f".sail-{sail}", "background")
    check(css_bg is not None, f".sail-{sail} has a colour (was black/grey)")
    check(decl(f".sail-bg-{sail}", "background") == css_bg, f".sail-bg-{sail} matches .sail-{sail}")
    check(js_cols.get(sail, "").lower() == (css_bg or "").lower(),
          f"SAIL_COLORS.{sail} in app.js matches the CSS ({js_cols.get(sail)} vs {css_bg})")
others = {k: v.lower() for k, v in js_cols.items() if k != "SS"}
check("SS" in js_cols and js_cols["SS"].lower() not in others.values(), "SS colour is distinct from every other sail")

# --- course map uses the card width (#96 G59) ---
check(decl(".coursemap", "max-width") is None, ".coursemap has no 760 px max-width")

# --- room for the Feedback pill (#101 G82, #103 G92) ---
pad = decl("main", "padding") or ""
parts = [int(p[:-2]) for p in pad.split() if p.endswith("px")]
bottom = parts[2] if len(parts) >= 3 else (parts[0] if parts else 0)
check(bottom >= 72, f"main bottom padding clears the Feedback pill ({bottom}px)")

# --- deploy fallback commands wrap instead of clipping (#98 G22) ---
check(decl(".dep-cmds", "white-space") == "pre-wrap", ".dep-cmds wraps long commands")

# --- nav: one row, sideways scroll, selected tab kept visible (#97 G67, #99 G75, #105 G97) ---
check(decl(".tabs", "flex-wrap") == "nowrap", ".tabs never wraps to a second line")
check(decl(".tabs", "overflow-x") == "auto", ".tabs scrolls sideways")
check("function keepTabVisible(" in JS and re.search(
    r'classList\.toggle\("active"[^;]*;\s*keepTabVisible\(', JS) is not None,
    "route() scrolls the selected tab into view")

print(f"\n{fails} failed")
sys.exit(1 if fails else 0)
