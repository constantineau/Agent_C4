"""The archiver (and its backfill drain) won't start without BOAT_ID (#307).

Before: unset, it filed every reading under "sr33", so a second boat's data landed in sr33's history.
Now it stops at start with one plain line naming the setting, and files readings under the id set.
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
             ARCHIVE_DB=os.path.join(tempfile.mkdtemp(), "a.db"))
    return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True,
                          timeout=60)


p = run("import archiver")
check("no BOAT_ID → stops, saying so",
      p.returncode != 0 and "BOAT_ID is not set" in (p.stderr.strip().splitlines() or [""])[-1])
p = run("import archiver; print(archiver.BOAT_ID)", BOAT_ID="bravo")
check("BOAT_ID set → readings carry it", p.returncode == 0 and p.stdout.strip() == "bravo")

p = run("import backfill")
check("backfill: no BOAT_ID → stops, saying so",
      p.returncode != 0 and "BOAT_ID is not set" in (p.stderr.strip().splitlines() or [""])[-1])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
