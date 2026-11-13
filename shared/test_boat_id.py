"""No install is assumed to be sr33 (#307).

Before: every service read `BOAT_ID` (Orin: `REROUTE_BOAT`, Lab Deploy: `DEPLOY_BOAT_ID`) with a
default of "sr33", and the crew password with a default of a known dev value. A second boat's Pi
with the setting left out wrote its data as sr33's. Now the setting is required: missing → the
service stops at start with one plain line naming it.

This file checks the shared helper, that sr33's N2K device map is never handed to another boat,
and (statically) that no service code, compose file or the example .env carries those defaults.
"""
import os
import re
import sys

from shared import boat_id, n2k_sources

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def exits(fn):
    """(exited, message) for fn()."""
    try:
        fn()
    except SystemExit as e:
        return True, str(e.code)
    return False, ""


print("the helper:")
check("set → its value, stripped", boat_id.require("BOAT_ID", {"BOAT_ID": " bravo "}) == "bravo")
gone, msg = exits(lambda: boat_id.require("BOAT_ID", {}))
check("missing → stops, and says which setting", gone and "BOAT_ID is not set" in msg)
gone, msg = exits(lambda: boat_id.require("REROUTE_BOAT", {"REROUTE_BOAT": "  "}))
check("blank counts as missing", gone and "REROUTE_BOAT is not set" in msg)

print("sr33's device map stays sr33's:")
check("sr33 still has its map", n2k_sources.devices_for("sr33"))
check("another boat gets none (label-only), never sr33's", n2k_sources.devices_for("bravo") == {})

print("no default boat or password anywhere:")
SETTINGS = "BOAT_ID|REROUTE_BOAT|DEPLOY_BOAT_ID|BOAT_PASSWORD|DATABASE_URL|POSTGRES_PASSWORD"
# the old dev password as a value; "sr33-dev-lab" / "sr33-dev_lab_..." / "name: sr33-dev" are names,
# and so is a compose-project pin (C4_DEV_PROJECT=sr33-dev, #314 PR C)
OLD_PW = re.compile(r"sr33-dev(?![-_\w])")
PROJECT_PIN = re.compile(r"C4_(?:DEV|PROD|BOAT|BENCH)_PROJECT\W{1,4}sr33-dev\b")
PY_DEFAULT = re.compile(r"""environ\.get\(\s*["'](%s)["']\s*,\s*["'][^"']+["']""" % SETTINGS)
COMPOSE_DEFAULT = re.compile(r"\$\{(%s):?-[^}]" % SETTINGS)   # an empty default is fine
SKIP_DIRS = {".git", "node_modules", "__pycache__", "docs"}
py_hits, compose_hits, pw_hits = [], [], []
for d, dirs, files in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
    for f in files:
        p = os.path.join(d, f)
        rel = os.path.relpath(p, ROOT)
        if f.endswith((".py", ".yml", ".yaml", ".md", ".sh", ".example", ".js", ".mjs", ".json",
                       ".env", ".service")) and rel != os.path.join("shared", "test_boat_id.py"):
            with open(p, encoding="utf-8", errors="replace") as fh:
                pw_hits += [f"{rel}:{i}" for i, line in enumerate(fh, 1)
                            if OLD_PW.search(PROJECT_PIN.sub("", line))
                            and not re.match(r"\s*name:\s*sr33-dev\s*$", line)]
        if f.endswith(".py") and not f.startswith("test_"):
            with open(p, encoding="utf-8", errors="replace") as fh:
                py_hits += [f"{rel}: {m.group(0)}" for m in PY_DEFAULT.finditer(fh.read())]
        elif re.fullmatch(r"compose\..*\.ya?ml", f) and "sample" not in f:
            with open(p, encoding="utf-8") as fh:
                compose_hits += [f"{rel}: {m.group(0)}" for m in COMPOSE_DEFAULT.finditer(fh.read())]
check("no service code defaults a boat or the password" + (f" ({py_hits})" if py_hits else ""),
      not py_hits)
check("no compose file defaults them" + (f" ({compose_hits})" if compose_hits else ""),
      not compose_hits)
check("the old dev password appears nowhere as a value" + (f" ({pw_hits})" if pw_hits else ""),
      not pw_hits)
with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as fh:
    example = dict(line.strip().split("=", 1) for line in fh
                   if "=" in line and not line.lstrip().startswith("#"))
check(".env.example leaves BOAT_ID and BOAT_PASSWORD for the owner to fill",
      example.get("BOAT_ID") == "" and example.get("BOAT_PASSWORD") == "")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
