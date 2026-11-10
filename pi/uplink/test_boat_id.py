"""The uplink won't start without BOAT_ID (#307).

Before: unset, it labelled every batch "sr33", so a second boat's data landed in sr33's history.
Now it stops at start with one plain line naming the setting, and labels batches with the id set.
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


def run(code, **env):
    e = {k: v for k, v in os.environ.items() if k != "BOAT_ID"}
    e.update(env, PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""),
             QUEUE_DIR=tempfile.mkdtemp())
    return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True,
                          timeout=60)


p = run("import uplink")
check("no BOAT_ID → stops, saying so",
      p.returncode != 0 and "BOAT_ID is not set" in (p.stderr.strip().splitlines() or [""])[-1])
p = run("import uplink; print(uplink.BOAT_ID)", BOAT_ID="bravo")
check("BOAT_ID set → batches carry it", p.returncode == 0 and p.stdout.strip() == "bravo")

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
