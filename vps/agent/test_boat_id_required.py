"""The agent (and the Pi engine, which shares its data-source code) won't start without a boat
id or crew password (#307).

Before: `BOAT_ID` defaulted to "sr33" in 13 agent modules and the crew password to a known dev
value, so an install with them left out came up as sr33 with a guessable password. Each check
imports a module in a fresh interpreter with the setting removed and expects a stop with one
plain line naming it.
"""
import os
import subprocess
import sys

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def start(module, drop=(), **extra):
    env = dict(os.environ, BOAT_ID="bravo", BOAT_PASSWORD="test-only", **extra)
    for k in drop:
        env.pop(k, None)
    p = subprocess.run([sys.executable, "-c", f"import {module}"], env=env,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, p.stderr.strip().splitlines()[-1:] or [""]


MODULES = ("app.tools", "app.datasource", "app.datasource_onboard", "app.alerts", "app.summarizer",
           "app.race_mode", "app.agent", "app.polar_tool", "app.sensor_health", "app.ais",
           "app.navigator", "app.onboard_conditions", "app.fatigue")

print("no BOAT_ID → stops, saying so:")
for m in MODULES:
    rc, last = start(m, drop=("BOAT_ID",))
    check(f"{m}", rc != 0 and "BOAT_ID is not set" in last[0])

print("BOAT_ID set → starts, as that boat:")
rc, last = start("app.datasource_onboard")
check("app.datasource_onboard imports", rc == 0)
p = subprocess.run([sys.executable, "-c", "from app import tools; print(tools.BOAT_ID)"],
                   env=dict(os.environ, BOAT_ID="bravo", BOAT_PASSWORD="x"),
                   capture_output=True, text=True, timeout=120)
check("tools.BOAT_ID is the one set (bravo)", p.stdout.strip() == "bravo")

print("crew password:")
rc, last = start("app.auth", drop=("BOAT_PASSWORD",), AUTH_ENABLED="true")
check("auth on, no BOAT_PASSWORD → stops, saying so", rc != 0 and "BOAT_PASSWORD is not set" in last[0])
rc, last = start("app.auth", drop=("BOAT_PASSWORD",), AUTH_ENABLED="false")
check("open bench (AUTH_ENABLED=false) needs none", rc == 0)
p = subprocess.run([sys.executable, "-c",
                    "from app import auth; print(auth.check_password('sr33-dev'))"],
                   env={**{k: v for k, v in os.environ.items() if k != "BOAT_PASSWORD"},
                        "AUTH_ENABLED": "false"}, capture_output=True, text=True, timeout=120)
check("the old dev password is not a default", p.stdout.strip() != "True")
p = subprocess.run([sys.executable, "-c",
                    "from app import auth; print(auth.check_password(' pw '), auth.check_password('pw'))"],
                   env=dict(os.environ, BOAT_PASSWORD=" pw ", AUTH_ENABLED="true"),
                   capture_output=True, text=True, timeout=120)
check("the password is used exactly as set (not stripped)", p.stdout.strip() == "True False")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
