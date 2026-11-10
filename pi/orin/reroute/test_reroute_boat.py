"""The Orin re-route won't run as a boat nobody named (#307).

Before: `REROUTE_BOAT` defaulted to "sr33", so an Orin installed for another boat re-routed with
sr33's profile and polar. Now the service stops at start, and a run (job.py) stops, with one plain
line naming the setting.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def run(args, **env):
    e = {k: v for k, v in os.environ.items() if k != "REROUTE_BOAT"}
    tmp = tempfile.mkdtemp()
    e.update(env, REROUTE_DIR=os.path.join(tmp, "reroute"), GRIB_CACHE=os.path.join(tmp, "grib"),
             PLUME_DIR=os.path.join(tmp, "plume"), INGESTED_DIR=os.path.join(tmp, "ingested"))
    return subprocess.run([sys.executable, *args], env=e, capture_output=True, text=True,
                          timeout=120, cwd=HERE)


def said(p):
    return p.returncode != 0 and "REROUTE_BOAT is not set" in (p.stderr.strip().splitlines() or [""])[-1]


check("service: no REROUTE_BOAT → stops at start, saying so",
      said(run(["-c", "import service; service.main()"])))
check("a run (job.py): no REROUTE_BOAT → stops, saying so",
      said(run([os.path.join(HERE, "job.py"), tempfile.mkdtemp()])))
p = run(["-c", "import job; print(job.BOAT_ID)"], REROUTE_BOAT="bravo")
check("REROUTE_BOAT set → that boat", p.returncode == 0 and p.stdout.strip() == "bravo")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
