"""The seed scripts must not write fake data under a real boat or to a remote stack (#39).

They used to default to `--boat-id sr33`, the live record, so one run of the documented command
put two hours of made-up sailing (or a fake own ship) into the boat's history. Now the default is
`demo-boat` and anything that looks real needs --allow-real. Runs each script's own argument
parsing with the network faked out, so nothing is posted.

Run: python3 vps/db/seed/test_seed_guard.py
"""
import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ais_inject                                                            # noqa: E402
import fake_telemetry                                                        # noqa: E402
import seed_guard                                                            # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


POSTS = []


def fake_post_batch(url, token, boat_id, readings):
    POSTS.append(("raw", url, boat_id))
    return {"accepted": len(readings)}


def fake_post(url, path, token, payload):
    POSTS.append((path, url, payload["boat_id"]))
    return {"accepted": 1}


fake_telemetry.post_batch = fake_post_batch
ais_inject.post = fake_post


def run(mod, *argv):
    """Run a script's main() with argv; returns (exit code, boat ids it posted to)."""
    POSTS.clear()
    old = sys.argv
    sys.argv = [mod.__name__, *argv]
    code = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mod.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.argv = old
    return code, {p[2] for p in POSTS}


for name, mod, extra in (("fake_telemetry", fake_telemetry, ["--minutes", "1"]),
                         ("ais_inject", ais_inject, ["--once"])):
    code, boats = run(mod, *extra)
    check(f"{name}: default writes under demo-boat only", code == 0 and boats == {"demo-boat"})
    code, boats = run(mod, *extra, "--boat-id", "sr33")
    check(f"{name}: refuses the real boat id sr33, posts nothing", code != 0 and not boats)
    code, boats = run(mod, *extra, "--boat-id", "c4")
    check(f"{name}: refuses any non-demo boat id", code != 0 and not boats)
    code, boats = run(mod, *extra, "--url", "https://ingest.example.org")
    check(f"{name}: refuses a non-local URL even for demo-boat", code != 0 and not boats)
    code, boats = run(mod, *extra, "--boat-id", "demo-2", "--url", "http://127.0.0.1:8101")
    check(f"{name}: allows another demo id on a local URL", code == 0 and boats == {"demo-2"})
    code, boats = run(mod, *extra, "--boat-id", "sr33", "--allow-real")
    check(f"{name}: --allow-real is the explicit override", code == 0 and boats == {"sr33"})

check("demo-boat lookalikes are not demo ids",
      not seed_guard.is_demo_boat("demo") and not seed_guard.is_demo_boat("sr33-demo"))
check("localhost.example.org is not local", not seed_guard.is_local_url("http://localhost.example.org"))
check("IPv6 loopback is local", seed_guard.is_local_url("http://[::1]:8101"))

sys.exit(0 if ok else 1)
