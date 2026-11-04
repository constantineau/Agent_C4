"""Lab files are replaced whole, never truncated and rewritten in place (#152, #180).

`open(p, "w")` + `json.dump` empties the file first: a crash or full disk mid-dump loses it
(issue #152: a re-freeze that dies leaves the playbook corrupt), and a reader in between — the
nightly backup — sees an empty or half file. Every Lab writer now goes through app/atomicio.

  - a dump that fails half way leaves the previous playbook intact (no temp left behind);
  - a reader polling while the playbook is rewritten never sees empty or partial JSON;
  - no `open(..., "w"/"wb")` writer is left in vps/lab/app outside atomicio (a new one must use
    the helper too).

Run (lab image): docker run --rm -v $PWD:/repo:ro -w /srv sr33-ci-lab python3 /repo/vps/lab/test_atomic_writes.py
"""
import json
import os
import re
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["PLAYBOOK_DIR"] = _tmp
from app import atomicio, pbstore  # noqa: E402

pbstore.PLAYBOOK_DIR = _tmp
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class Unserializable:
    pass


print("a failed save keeps the previous playbook (#152)")
good = {"race_id": "mackinac", "start_epoch": 1784394000, "steps": ["a"] * 5000}
pid = pbstore.save(good)
bad = dict(good, steps=["b"] * 5000, zzz_last=Unserializable())   # dies late in the dump
try:
    pbstore.save(bad)
    raised = False
except TypeError:
    raised = True
check("the broken save raises", raised)
check("the previous playbook is still there, whole", pbstore.get(pid) == good)
check("no temp file left behind", sorted(os.listdir(_tmp)) == [f"{pid}.json"])

print("a reader never sees a half-written playbook")
bundles = [dict(good, steps=[c * 50] * 4000) for c in "xyz"]
stop, seen, bad_reads = threading.Event(), [0], []


def reader():
    p = pbstore._path(pid)
    while not stop.is_set():
        try:
            with open(p) as f:
                txt = f.read()
        except FileNotFoundError:
            bad_reads.append("missing")
            continue
        try:
            json.loads(txt)
            seen[0] += 1
        except ValueError:
            bad_reads.append(len(txt))


t = threading.Thread(target=reader)
t.start()
for i in range(150):
    pbstore.save(bundles[i % 3])
stop.set()
t.join()
check(f"{seen[0]} reads during 150 rewrites, none empty, partial or missing "
      f"({len(bad_reads)} bad)", seen[0] > 0 and not bad_reads)

print("atomicio itself")
p = os.path.join(_tmp, "sub", "x.json")
atomicio.write_json(p, {"a": 1})
check("write_json creates the directory and the file", json.load(open(p)) == {"a": 1})
try:
    with atomicio.atomic_open(p, "w") as f:
        f.write('{"a": 2')
        raise RuntimeError("crash mid-write")
except RuntimeError:
    pass
check("a block that raises leaves the old file", json.load(open(p)) == {"a": 1}
      and os.listdir(os.path.dirname(p)) == ["x.json"])

print("every Lab writer uses the helper")
app_dir = os.path.dirname(atomicio.__file__)
raw = []
for d, _, fs in os.walk(app_dir):
    for n in fs:
        if not n.endswith(".py") or n == "atomicio.py":
            continue
        for i, line in enumerate(open(os.path.join(d, n)), 1):
            # open(..., "w"/"wb"/"a") by name; os.fdopen on a mkstemp temp is fine
            if "atomicio." in line:
                continue
            if re.search(r"(?<![.\w])open\([^)]*['\"](w|wb|a|ab|w\+)['\"]", line) \
                    or re.search(r"\.write_text\(|\.write_bytes\(|shutil\.copy2?\(", line):
                raw.append(f"{os.path.relpath(os.path.join(d, n), app_dir)}:{i}: {line.strip()}")
check("no in-place writer left in vps/lab/app" + ("" if not raw else ": " + "; ".join(raw[:5])),
      not raw)

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
