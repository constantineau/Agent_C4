"""truth.py keeps the LAST sample in each second, as it claims (#50).

The per-second query grouped by (path, source, second) with a bare `value`, so SQLite returned
whichever row it scanned last, not the latest one. `MAX(time)` makes SQLite take `value` from
the row holding the max.

Run: docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e PYTHONPATH=/repo \
       sr33-ci-agent python3 /repo/tools/replay/test_truth.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "archive.db")
c = sqlite3.connect(db)
c.execute("CREATE TABLE readings (time TEXT, boat_id TEXT, path TEXT, source TEXT, value REAL)")
path = next(iter(truth.PATHS))
# Three samples in one second, written in time order. Without an aggregate SQLite returned the
# first row of the group (the .100 sample), which is the opposite of what the docstring claims.
for t, v in (("2026-07-18T17:00:00.100Z", 1.0), ("2026-07-18T17:00:00.500Z", 2.0),
             ("2026-07-18T17:00:00.900Z", 3.0)):
    c.execute("INSERT INTO readings VALUES (?,?,?,?,?)", (t, "bravo", path, "src1", v))
c.commit()
c.close()

start = datetime(2026, 7, 18, 16, 59, 59, tzinfo=timezone.utc)
end = datetime(2026, 7, 18, 17, 0, 2, tzinfo=timezone.utc)
doc = truth.build(db, start, end, 1.0, os.path.join(tmp, "out"), "bravo")
conv = truth.PATHS[path][2]
series = doc["series"][path]["src1"]
got = next(x for x in series if x is not None)
print(f"     first value after the second: {got} (want {round(conv(3.0), 4)})")
check("the LAST sample in the second is kept, not the first one written",
      got == round(conv(3.0), 4))
with open(os.path.join(tmp, "out", "truth.json")) as fh:
    check("truth.json is written", json.load(fh)["step_s"] == 1.0)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
