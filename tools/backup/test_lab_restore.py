"""lab_restore.py's Postgres drill must FAIL when the restore commands fail, even if every row
count still matches (#180, Codex on #198): a failed index, constraint or function leaves the
counts intact. Docker is faked here; the real drill is in tools/backup/README.md.

Run: python3 tools/backup/test_lab_restore.py
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab_restore as lr                                                     # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


lr.time.sleep = lambda s: None
lr.print = lambda *a, **k: None
RUNS = []
ART = {"name": "vps-sr33_dev.pgdump", "image": "sha256:localid", "user": "sr33", "db": "sr33_dev",
       "image_ref": "timescale/timescaledb@sha256:d833",
       "timescaledb": "2.27.2", "tables": {"public.alerts": 3, "public.telemetry_raw": 10}}


def fake_docker(restore_rc=0, restore_err="", fail_sql=()):
    def docker(*args, **kw):
        r = SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "run":
            RUNS.append(args)
        if args[0] == "logs":
            r.stdout = "PostgreSQL init process complete; ready for start up."
        elif args[0] == "exec" and args[2] == "pg_restore":
            r.returncode, r.stderr = restore_rc, restore_err
        elif args[0] == "exec" and args[2] == "psql":
            sql = args[-1]
            if any(f in sql for f in fail_sql):
                r.returncode, r.stderr = 1, f"ERROR: {sql} failed"
            elif sql == "SELECT 1":
                r.stdout = "1\n"
            elif "extversion" in sql:
                r.stdout = "2.27.2\n"
            elif sql.startswith("SELECT count(*) FROM ONLY "):
                r.stdout = f"{ART['tables'][sql.rsplit(' ', 1)[1]]}\n"
        return r
    return docker


def drill(**kw):
    lr.results.clear()
    lr.docker = fake_docker(**kw)
    with tempfile.TemporaryDirectory() as w:
        lr.verify_pg(ART, os.path.join(w, "x.pgdump"), w, tmpfs="1g")
    return all(lr.results) and bool(lr.results)


check("a clean restore passes", drill())
check("the throwaway postgres runs the image pulled by digest, not the local image id",
      RUNS and RUNS[-1][-1] == ART["image_ref"] and "sha256:localid" not in RUNS[-1])
ref = ART.pop("image_ref")
check("no digest recorded and no image in the set -> FAILS (a new machine could not restore)",
      not drill())
ART["image_ref"] = ref
check("pg_restore exit 1 with an error, counts all matching -> FAILS",
      not drill(restore_rc=1, restore_err="pg_restore: error: could not execute query: "
                "ERROR: could not create unique index \"x\"\n"))
check("exit 0 but an error line -> FAILS",
      not drill(restore_err="pg_restore: error: COPY failed for table \"alerts\"\n"))
check("timescaledb_post_restore() failing -> FAILS",
      not drill(fail_sql=("timescaledb_post_restore",)))
check("timescaledb_pre_restore() failing -> FAILS",
      not drill(fail_sql=("timescaledb_pre_restore",)))
check("CREATE EXTENSION failing -> FAILS", not drill(fail_sql=("CREATE EXTENSION",)))
lr.KNOWN_BENIGN_RESTORE_ERRORS = ("some listed benign thing",)
check("an error that IS on the benign list is accepted",
      drill(restore_rc=1, restore_err="pg_restore: error: some listed benign thing\n"))
check("...but an unlisted one beside it still fails",
      not drill(restore_rc=1, restore_err="pg_restore: error: some listed benign thing\n"
                "pg_restore: error: something else\n"))

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
