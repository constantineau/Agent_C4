"""salvage.py --resume must never trust a torn destination (#184-11).

The destination used to be written journal OFF / synchronous OFF, where a kill or power cut
can tear the file, and --resume then picked up at max(id) as if every row below it were there.
Now the destination is WAL with real commits (always a clean prefix) and --resume refuses one
that fails quick_check.
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # pi/archiver, for corrupt_fixtures

import corrupt_fixtures                             # noqa: E402
import salvage                                      # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def make_src(path, n):
    c = sqlite3.connect(path)
    c.execute(salvage.CREATE)
    c.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?)",
                  [(i, f"2026-07-18T18:00:{i % 60:02d}Z", "sr33", "s", "p" * 40, float(i), None)
                   for i in range(1, n + 1)])
    c.commit()
    c.close()


with tempfile.TemporaryDirectory() as d:
    src, dst = os.path.join(d, "src.db"), os.path.join(d, "dst.db")
    make_src(src, 3000)

    print("the destination is written with a journal (commits are atomic):")
    salvage.main(["--src", src, "--dst", dst, "--chunk", "500", "--min-free-g", "0"])
    c = sqlite3.connect(dst)
    mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    n = c.execute("SELECT count(*) FROM readings").fetchone()[0]
    c.close()
    check(f"journal_mode is wal (got {mode})", mode == "wal")
    check("all 3000 rows salvaged", n == 3000)

    print("--resume refuses a destination that fails quick_check:")
    c = sqlite3.connect(dst)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    leaves = corrupt_fixtures.layout(dst)["leaves"]["readings"]
    check("fixture has several table leaves", len(leaves) >= 3)
    corrupt_fixtures.scribble_page(dst, leaves[1], seed=7, start=8)   # a middle leaf torn
    refused = None
    try:
        salvage.main(["--src", src, "--dst", dst, "--resume", "--min-free-g", "0"])
    except SystemExit as exc:
        refused = str(exc)
    except sqlite3.DatabaseError as exc:
        refused = None
        print(f"    (raised {exc!r} instead of refusing)")
    check("refused with a message naming quick_check", refused and "quick_check" in refused)

    print("--resume on a clean destination continues where it stopped:")
    dst2 = os.path.join(d, "dst2.db")
    salvage.main(["--src", src, "--dst", dst2, "--hi", "1200", "--chunk", "500",
                  "--min-free-g", "0"])
    salvage.main(["--src", src, "--dst", dst2, "--resume", "--chunk", "500",
                  "--min-free-g", "0"])
    c = sqlite3.connect(dst2)
    n, lo, hi = c.execute("SELECT count(*), min(id), max(id) FROM readings").fetchone()
    c.close()
    check(f"3000 rows, ids 1..3000 (got {n}, {lo}..{hi})", (n, lo, hi) == (3000, 1, 3000))

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
