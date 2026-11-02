"""Deterministic archive damage for the archiver tests (test_brownout, test_startup).

Every case writes the SAME bytes on every run: page numbers come from the file's own schema
(`sqlite_master.rootpage`) and `dbstat`, and any "random" bytes come from a seeded
`random.Random`. The old test scribbled `os.urandom` across a byte range, so which page it hit,
and which error SQLite raised, changed from run to run (about 7% of runs failed; pi review
2026-11-02, tests-system A1/A2).

Not a test file itself (no `test_` prefix): CI runs only test_*.py.
"""
import random
import sqlite3
from pathlib import Path

ROW = ("2026-07-18T18:00:00.000Z", "sr33", "n2k-socketcan.15",
       "navigation.speedThroughWater", 3.2, None)


def rows(n, start=0):
    """n distinct rows, one per second from 18:00:00."""
    out = []
    for i in range(start, start + n):
        t = f"2026-07-18T{18 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}.000Z"
        out.append((t,) + ROW[1:])
    return out


def checkpoint(path):
    """Move the WAL into the main file, so damage to the main file is what SQLite reads."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def layout(path):
    """{page_size, page_count, root: {name: page}, leaves: {name: [pages in tree order]}}."""
    conn = sqlite3.connect(path)
    try:
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        pc = conn.execute("PRAGMA page_count").fetchone()[0]
        root = {n: r for n, r in conn.execute("SELECT name, rootpage FROM sqlite_master")
                if r}
        leaves = {}
        for name, page, _path in conn.execute(
                "SELECT name, pageno, path FROM dbstat WHERE pagetype = 'leaf' ORDER BY path"):
            leaves.setdefault(name, []).append(page)
    finally:
        conn.close()
    return {"page_size": ps, "page_count": pc, "root": root, "leaves": leaves}


def _write(path, offset, data):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)
        f.flush()


def scribble_page(path, page, seed, start=0):
    """Overwrite page `page` (1-based) from byte `start` of the page with seeded random bytes."""
    lay = layout(path)
    ps = lay["page_size"]
    _write(path, (page - 1) * ps + start, random.Random(seed).randbytes(ps - start))


def scribble_from(path, offset, seed):
    """Seeded random bytes from `offset` to the end of the file (the old test's shape)."""
    size = Path(path).stat().st_size
    _write(path, offset, random.Random(seed).randbytes(max(0, size - offset)))


def garble_last_text(path, needle=b"navigation.speedThroughWater"):
    """Replace the LAST copy of `needle` in the file with invalid UTF-8 of the same length —
    a text value garbled in place, record structure intact."""
    data = Path(path).read_bytes()
    at = data.rindex(needle)
    _write(path, at, b"\xff" * len(needle))


# name -> (rows in the db, what to do to it, the error class the probes must hit)
# The seeds for the two schema-record shapes were found by searching (SQLite 3.46.1, the CI
# image): they reproduce the two errors the review saw crash the startup, deterministically.
def damage(case, path):
    """Apply case `case` to the checkpointed archive at `path`. Returns a description."""
    checkpoint(path)
    lay = layout(path)
    if case == "header":
        _write(path, 0, b"this is not sqlite" + b"\0" * 82)
        return "the 100-byte file header"
    if case == "schema_page":
        scribble_page(path, 1, seed=0, start=100)
        return "page 1 (the schema b-tree), seed 0"
    if case == "schema_record_toobig":
        scribble_from(path, 1024, seed=384)
        return "page 1's records onward, seed 384 (string or blob too big)"
    if case == "schema_record_utf8":
        scribble_from(path, 1024, seed=226)
        return "page 1's records onward, seed 226 (UnicodeDecodeError)"
    if case == "table_root":
        scribble_page(path, lay["root"]["readings"], seed=1)
        return f"readings root page {lay['root']['readings']}"
    if case == "time_index_root":
        scribble_page(path, lay["root"]["readings_time_idx"], seed=2)
        return f"readings_time_idx root page {lay['root']['readings_time_idx']}"
    if case == "path_index_root":
        scribble_page(path, lay["root"]["readings_path_time_idx"], seed=3)
        return f"readings_path_time_idx root page {lay['root']['readings_path_time_idx']}"
    if case == "table_tail_leaf":
        page = lay["leaves"]["readings"][-1]
        scribble_page(path, page, seed=4)
        return f"the last readings leaf, page {page} (where the last writes went)"
    if case == "garbled_text":
        garble_last_text(path)
        return "the newest row's path text, invalid UTF-8"
    if case == "table_middle_leaf":
        lv = lay["leaves"]["readings"]
        page = lv[len(lv) // 2]
        scribble_page(path, page, seed=5)
        return f"a readings leaf in the MIDDLE, page {page} (no bounded probe reaches it)"
    raise ValueError(case)


# What each case is, and what the archiver must do about it.
# case -> (rows, the error the open/probes raise on it). The bounded startup probes must catch
# all of these. The error is checked as a PRECONDITION: if a new SQLite turns a case into a
# different error, the test says so instead of silently testing something else.
STARTUP_CASES = {
    "header": (2000, "DatabaseError: file is not a database"),
    "schema_page": (2000, "DatabaseError: database disk image is malformed"),
    "schema_record_toobig": (1, "DataError: string or blob too big"),
    "schema_record_utf8": (1, "UnicodeDecodeError"),
    "table_root": (2000, "DatabaseError: database disk image is malformed"),
    "time_index_root": (2000, "DatabaseError: database disk image is malformed"),
    "path_index_root": (2000, "DatabaseError: database disk image is malformed"),
    "table_tail_leaf": (2000, "DatabaseError: database disk image is malformed"),
    "garbled_text": (2000, "OperationalError: Could not decode to UTF-8"),
}
