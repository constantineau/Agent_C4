"""No new `sr33` in code (#314).

sr33 is one boat's id. The code used to carry it as the project's own name too (browser globals,
events, container names, help text), which a second boat would inherit. #314 PR A renamed what
lives only in code. What is left is listed below, each entry a file (glob) plus the exact pattern
that may appear there, and why:

  * group 2 — names that hold state on live machines (systemd units, browser storage keys). Later
    #314 PRs move them to neutral defaults and SHRINK THIS LIST; delete an entry in the same PR
    that removes its last use. PR C did compose projects, databases, volumes, in-container paths
    and CI image tags: code has only neutral defaults, and the live boxes keep their names through
    pins in their own .env (shared/test_stack_names.py checks both).
  * group 3 — places where sr33 really is that boat: its data files, its boat_id, its history,
    and migrations that stay as written.

Anything else that says sr33 / SR33 fails here. Say "the boat" (or take the boat id from the
profile) instead; if it truly is sr33's own data, add a precise entry with the reason.

Not scanned: Markdown (docs, the progress file) and test files (test_*, *.test.*), which name
sr33 on purpose to prove another boat never gets its data.
"""
import fnmatch
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SR33 = re.compile(r"sr33", re.I)
CODE = (".py", ".sh", ".js", ".mjs", ".json", ".sql", ".yml", ".yaml", ".html", ".css", ".txt",
        ".service", ".timer", ".conf", ".template", ".example", ".toml", ".cfg")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "data",
             ".coastline_cache"}

# Group 2: state on live machines. Later #314 PRs shrink this.
UNIT = r"\bsr33-(?:can0|orin-[\w-]+)|\bsr33-oom\.conf|/etc/sr33/|/run/sr33-shed|=sr33-shed\b"
ORIN_HOME = r"/home/agent-c4/sr33/"                   # the Orin's playbook directory
OLD_STORAGE = r"OLD = \"sr33\.\"|OLD = \"sr33\.dash\.\""  # the old browser-key prefix, read once
# The names a box set up before #314 PR C pins in its .env, shown as examples (commented out).
LIVE_PINS = (r"^#C4_(?:DEV|PROD|BOAT|BENCH)_(?:PROJECT|PGDATA)="
             r"sr33-(?:dev|prod|pi)(?:_sr33_(?:dev|prod)_pgdata)?(?=\s|$)")

ALLOWED = [
    # ---- group 2 ----
    ([".env.example"], LIVE_PINS,
     "the pins an existing install sets to keep its live names (PR C: neutral defaults, live boxes pinned)"),
    (["pi/systemd/*", "pi/orin/*"], UNIT, "systemd units and their config paths (PR D)"),
    (["pi/systemd/sr33-orin-copilot.service"], ORIN_HOME, "Orin playbook directory (PR D)"),
    (["vps/web/public/app.js", "pi/console/dashboard/dashboard.js"], OLD_STORAGE,
     "old browser-key prefix: carried over to c4.* so a deploy keeps the iPad's login (PR B)"),
    (["vps/web/public/app.js", "vps/web/public/sail.js", "vps/web/public/plot.js"],
     r"LEGACY_(?:KEY|EVT) = [\"']sr33[.:][\"']",
     "cached older scripts still work with the new ones; remove after 2027-04 (PR B)"),
    (["vps/web/public/app.js", "pi/console/default.conf.template"], r"window\.SR33_ONBOARD\b",
     "onboard flag under the old name too, for a script pair loaded mid-deploy; remove after 2027-04 (PR B)"),
    (["vps/agent/app/auth.py"], r"\"sr33-auth:\"", "token key salt: changing it logs every device out"),

    # ---- group 3: sr33 is that boat ----
    (["*"], r"\bpolars_sr33\.sql|\bsr33_(?:sail_polars|crossovers|wardrobe|speed_guide)\.\w+"
            r"|\bsr33\.json\b", "sr33's own data files, by name"),
    (["*"], r"another boat must never write as sr33 \(#307\)", "the #307 note: sr33 was the default"),
    (["vps/lab/boats/sr33.json", "vps/db/seed/polars_sr33.sql", "vps/db/seed/sr33_*.json",
      "vps/agent/knowledge/sr33_wardrobe.json"], r"(?i)sr33", "sr33's own data files"),
    (["vps/db/migrations/001_init.sql", "vps/db/migrations/002_telemetry_raw.sql"], r"(?i)sr33",
     "old migrations stay as written"),
    (["vps/db/migrations/00[345]_*.sql"], r"DEFAULT 'sr33'", "old migrations stay as written"),
    (["vps/db/seed/source_priority.sql", "vps/db/seed/source_notes.sql"], r"'sr33'|for the SR33\.",
     "sr33's instruments, seeded under its boat_id"),
    (["vps/db/seed/dev_seed.sql", "vps/db/seed/seed_dev.sh"], r"SR33 (?:\"C4\" )?ORC",
     "sr33's ORC certificate"),
    (["vps/lab/polar_fixture.py", "pi/archiver/corrupt_fixtures.py"], r"(?i)sr33",
     "test support: routes on / records as sr33 on purpose"),
    (["shared/n2k_sources.py"], r"\bSR33_DEVICES\b|\{\"sr33\": ", "sr33's device table"),
    (["tools/analysis/perf_by_config.py"], r"'sr33'", "reads sr33's polar rows"),
    (["pi/systemd/sr33-orin-reroute.service"], r"REROUTE_BOAT=sr33\b", "the boat this Orin re-routes"),
    (["compose.pi.sample.yml"], r"\bsr33-bench\b|boat_id=sr33\b", "the bench boat id and why"),
    (["vps/agent/knowledge/build_speed_guide.py"], r"\bsr33 --wardrobe", "usage example for sr33"),
    (["shared/tool_contracts.py"], r"\"boat_id\": \"sr33\"", "example payload"),
    (["vps/agent/app/boatinfo.py", "vps/agent/app/matcher.py", "pi/engine/engine_app.py",
      "vps/lab/app/boats.py", "vps/lab/app/polars.py", "shared/boat_id.py"], r"\bsr33's",
     "about sr33 (another boat must not get sr33's data)"),
    (["vps/agent/app/main.py"], r"typed \"sr33\" is not the sr33 profile", "about sr33's profile id"),
    (["vps/agent/app/boatlib.py"], r"sr33 \+ its polar", "size of sr33's library entry"),
    (["tools/ci/run.sh"], r"quietly being sr33\b", "the #307 note: sr33 was the default"),
    (["tools/replay/web/overrides.js"], r"the sr33 jib bands", "sr33's jib bands, mirrored"),
    (["shared/boat_words.py", "pi/orin/copilot/boatname.py"], r"\bSR33\b",
     "docstring examples of sr33's wording"),
    (["shared/boat_profile.py"], r"\bSR33\b", "sr33 was profile #1"),
    (["vps/lab/app/geo/enc.py", "vps/lab/app/geo/obstacles.py", "compose.dev.yml"],
     r"SR33 (?:= )?7 ft", "sr33's draft"),
    (["vps/lab/races/*.json"], r"\"cls\": \"[^\"]*SR33\"", "a fleet entry's class"),
    (["vps/lab/web/app.js"], r"SR33 \\\"C4\\\"", "placeholder: the own boat's name"),
]

# File NAMES that may say sr33: group 2 units (PR D) and group 3 data files.
ALLOWED_NAMES = ["pi/systemd/sr33-*", "pi/systemd/ollama.service.d/sr33-oom.conf",
                 "vps/lab/boats/sr33.json", "vps/db/seed/polars_sr33.sql", "vps/db/seed/sr33_*",
                 "vps/agent/knowledge/sr33_*"]


used = set()


def leftover(rel, line):
    """The line with every allowed pattern for this file removed: what is still unexplained."""
    for n, (globs, pat, _why) in enumerate(ALLOWED):
        if any(fnmatch.fnmatch(rel, g) for g in globs):
            line, k = re.subn(pat, "", line)
            if k:
                used.add(n)
    return line


def is_test(name):
    return name.startswith("test_") or ".test." in name


print("the allow-list itself:")
check("a new global is caught", SR33.search(leftover("vps/web/public/app.js", "window.SR33_X = 1")))
check("the old storage prefix is allowed in the carry-over helper",
      not SR33.search(leftover("vps/web/public/app.js", 'const OLD = "sr33.";')))
check("but a storage key under the old name is not",
      SR33.search(leftover("vps/web/public/app.js", "localStorage.getItem('sr33.mode')")))
check("a live name as an ops script's default is not (PR C: it comes from the box's pin)",
      SR33.search(leftover("pi/deploy/deploy-data-path.sh", "P=${COMPOSE_PROJECT:-sr33-pi}")))
check("a live-name pin is allowed as a commented example in .env.example",
      not SR33.search(leftover(".env.example", "#C4_DEV_PGDATA=sr33-dev_sr33_dev_pgdata")))
check("but not as a live setting there",
      SR33.search(leftover(".env.example", "C4_DEV_PROJECT=sr33-dev")))
check("help text naming sr33 is not",
      SR33.search(leftover("pi/deploy/deploy-data-path.sh", "# deploy to the SR33")))
check("a line is cleared only when EVERY sr33 in it is allowed",
      SR33.search(leftover("pi/deploy/deploy-data-path.sh", "sr33-pi and the sr33 boat")))
for globs, pat, why in ALLOWED:
    re.compile(pat)

print("no sr33 in code outside the allow-list:")
used.clear()
hits, names = [], []
for d, dirs, files in os.walk(ROOT):
    dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
    for f in sorted(files):
        rel = os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/")
        if f.endswith(".md") or is_test(f):
            continue
        if not (f.endswith(CODE) or f in ("Dockerfile", "Makefile") or f.startswith("compose.")):
            continue
        if SR33.search(rel) and not any(fnmatch.fnmatch(rel, g) for g in ALLOWED_NAMES):
            names.append(rel)
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if SR33.search(line) and SR33.search(leftover(rel, line)):
                    hits.append(f"{rel}:{i}: {line.strip()[:120]}")

check(f"no sr33 outside the allow-list ({len(hits)} found)", not hits)
for h in hits[:40]:
    print(f"      {h}")
check(f"no new file named sr33 ({len(names)} found)", not names)
for n in names:
    print(f"      {n}")
# An entry nothing needs any more is removed, so the list only ever shrinks to what is left.
stale = [f"{why}: {pat}" for n, (_g, pat, why) in enumerate(ALLOWED) if n not in used]
check(f"every allow-list entry is still needed ({len(stale)} stale)", not stale)
for s in stale:
    print(f"      {s}")

if not ok:
    print("\nSay 'the boat' / take the boat id from the profile; if it truly is sr33's own data or a "
          "live name #314 has not moved yet, add a precise entry to ALLOWED in this file.")
sys.exit(0 if ok else 1)
