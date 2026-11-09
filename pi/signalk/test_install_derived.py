"""Signal K plugin step (#171) and Pi access logs (#184-1).

#171: the signalk-derived-data install must be pinned (1.45.0), must never block Signal K, and a
failed derivation must turn off only its own channels. patch-windground-path.sh must fail loudly
when its target file is missing (it used to say "nothing to do" and exit 0).
#184-1: the engine's uvicorn access log and the console's nginx /api/ access log are off.

Plain script: runs the real shell scripts with sh against a temp config dir and a fake npm.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PATCH = os.path.join(HERE, "patch-windground-path.sh")
INSTALL = os.path.join(HERE, "install-derived.sh")
PIN = "1.45.0"

WINDGROUND_JS = """module.exports = function (app) {
  return { optionKey: 'groundWind',
    calculator: function () { return [{ path: 'environment.wind.angleTrueGround', value: 1 },
                                     { path: 'environment.wind.directionTrue', value: 2 }] } }
}
"""

FAILS = []


def check(ok, msg):
    print(("[OK ] " if ok else "[FAIL] ") + msg)
    if not ok:
        FAILS.append(msg)


def fake_plugin(sk, version, with_windground=True):
    d = os.path.join(sk, "node_modules", "signalk-derived-data")
    os.makedirs(os.path.join(d, "dist", "calcs"), exist_ok=True)
    with open(os.path.join(d, "package.json"), "w") as f:
        f.write('{\n  "name": "signalk-derived-data",\n  "version": "%s",\n  "main": "dist/index.js"\n}\n'
                % version)
    wg = os.path.join(d, "dist", "calcs", "windGround.js")
    if with_windground:
        with open(wg, "w") as f:
            f.write(WINDGROUND_JS)
    elif os.path.exists(wg):
        os.remove(wg)
    return d


def make_env(tmp, npm_mode):
    """A config dir, and a fake npm on PATH. npm_mode: 'fail' | 'install' (writes PIN)."""
    sk = os.path.join(tmp, "sk")
    os.makedirs(sk)
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    log = os.path.join(tmp, "npm.log")
    if npm_mode == "fail":
        body = 'echo "$@" >> "%s"; echo "npm ERR! network" >&2; exit 1\n' % log
    else:
        body = (
            'echo "$@" >> "%s"\n'
            'd="%s/node_modules/signalk-derived-data"; mkdir -p "$d/dist/calcs"\n'
            'printf \'{\\n  "name": "signalk-derived-data",\\n  "version": "%s"\\n}\\n\' > "$d/package.json"\n'
            "cat > \"$d/dist/calcs/windGround.js\" <<'JS'\n%sJS\n"
        ) % (log, sk, PIN, WINDGROUND_JS)
    with open(os.path.join(bindir, "npm"), "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(os.path.join(bindir, "npm"), 0o755)
    env = dict(os.environ, SK_CONFIG=sk, SRC_DIR=HERE, NPM_TIMEOUT_S="20",
               PATH=bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"))
    return sk, log, env


def run_install(env):
    return subprocess.run(["sh", INSTALL], env=env, capture_output=True, text=True, timeout=60)


def npm_calls(log):
    return open(log).read().splitlines() if os.path.exists(log) else []


def conf(sk):
    with open(os.path.join(sk, "plugin-config-data", "derived-data.json")) as f:
        return json.load(f)


def windground(sk):
    with open(os.path.join(sk, "node_modules", "signalk-derived-data", "dist", "calcs",
                           "windGround.js")) as f:
        return f.read()


def test_patch_missing_target_fails_loudly():
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["sh", PATCH, os.path.join(tmp, "nope")], capture_output=True, text=True)
        check(r.returncode != 0, "patch: missing windGround.js exits non-zero (was exit 0)")
        check("FAILED" in r.stderr, "patch: missing target says FAILED on stderr")


def test_patch_applies_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        d = fake_plugin(tmp, PIN)
        r1 = subprocess.run(["sh", PATCH, d], capture_output=True, text=True)
        r2 = subprocess.run(["sh", PATCH, d], capture_output=True, text=True)
        src = windground(tmp)
        check(r1.returncode == 0 and r2.returncode == 0, "patch: applies, and re-runs cleanly")
        check("'environment.wind.directionTrueGround'" in src
              and "'environment.wind.directionTrue'" not in src,
              "patch: ground wind moved to directionTrueGround")


def test_offline_fresh_volume_still_exits_0():
    with tempfile.TemporaryDirectory() as tmp:
        sk, log, env = make_env(tmp, "fail")
        r = run_install(env)
        check(r.returncode == 0, "install: failed npm on a fresh volume still exits 0 (Signal K starts)")
        check("FAILED" in r.stderr, "install: failed npm is logged as FAILED")
        check(npm_calls(log) and ("signalk-derived-data@" + PIN) in npm_calls(log)[0]
              and "--save-exact" in npm_calls(log)[0],
              "install: npm asked for exactly signalk-derived-data@%s" % PIN)


def test_pinned_version_present_needs_no_network():
    with tempfile.TemporaryDirectory() as tmp:
        sk, log, env = make_env(tmp, "fail")
        fake_plugin(sk, PIN)
        r = run_install(env)
        check(r.returncode == 0 and not npm_calls(log), "install: pinned version present, npm not run")
        check(conf(sk)["configuration"]["wind"]["groundWind"] is True, "install: config seeded, ground wind on")
        check("directionTrueGround" in windground(sk), "install: plugin patched")
        check("FAILED" not in r.stderr, "install: clean run logs no FAILED")


def test_other_version_is_replaced_with_pin():
    with tempfile.TemporaryDirectory() as tmp:
        sk, log, env = make_env(tmp, "install")
        fake_plugin(sk, "1.46.1")
        r = run_install(env)
        check(r.returncode == 0 and len(npm_calls(log)) == 1, "install: 1.46.1 present triggers one install")
        with open(os.path.join(sk, "node_modules", "signalk-derived-data", "package.json")) as f:
            check('"version": "%s"' % PIN in f.read(), "install: plugin now at %s" % PIN)
        check("directionTrueGround" in windground(sk), "install: reinstalled plugin patched")


def test_failed_patch_turns_off_only_ground_wind_then_recovers():
    with tempfile.TemporaryDirectory() as tmp:
        sk, log, env = make_env(tmp, "fail")
        fake_plugin(sk, PIN, with_windground=False)
        r = run_install(env)
        c = conf(sk)
        check(r.returncode == 0, "install: failed patch still exits 0")
        check("FAILED" in r.stderr, "install: failed patch logged as FAILED")
        check(c["enabled"] is True and c["configuration"]["wind"]["groundWind"] is False
              and c["configuration"]["wind"]["directionTrue"] is True
              and c["configuration"]["course data"]["vmg_Wind"] is True,
              "install: failed patch turns off groundWind only")
        # the file comes back (e.g. fixed plugin): ground wind is turned back on
        fake_plugin(sk, PIN, with_windground=True)
        r = run_install(env)
        check(r.returncode == 0 and conf(sk)["configuration"]["wind"]["groundWind"] is True,
              "install: patch works again, ground wind turned back on")


def test_crew_turned_ground_wind_off_stays_off():
    with tempfile.TemporaryDirectory() as tmp:
        sk, log, env = make_env(tmp, "fail")
        fake_plugin(sk, PIN)
        os.makedirs(os.path.join(sk, "plugin-config-data"))
        c = json.load(open(os.path.join(HERE, "derived-data.json")))
        c["configuration"]["wind"]["groundWind"] = False
        with open(os.path.join(sk, "plugin-config-data", "derived-data.json"), "w") as f:
            json.dump(c, f, indent=2)
        run_install(env)
        check(conf(sk)["configuration"]["wind"]["groundWind"] is False,
              "install: a groundWind the crew turned off is left off")


def test_compose_never_blocks_signalk():
    src = open(os.path.join(REPO, "compose.pi.yml")).read()
    sk = src[src.index("\n  signalk:\n"):src.index("\n  uplink:\n")]
    dep = sk[sk.index("signalk-derived:"):]
    check(re.search(r"required:\s*false", dep) is not None,
          "compose: signalk starts even if signalk-derived fails (required: false)")
    check("install-derived.sh" in src, "compose: signalk-derived runs install-derived.sh")
    check(not re.search(r"npm install[^\n]*signalk-derived-data(\s|$)", src),
          "compose: no unpinned npm install left")


def test_access_logs_off():
    df = open(os.path.join(REPO, "pi", "engine", "Dockerfile")).read()
    check("--no-access-log" in df, "engine: uvicorn access log off")
    nc = open(os.path.join(REPO, "pi", "console", "default.conf.template")).read()
    api = nc[nc.index("location /api/"):]
    api = api[:api.index("}")]
    check("access_log off;" in api, "console: nginx /api/ access log off")


if __name__ == "__main__":
    if not shutil.which("sh") or not shutil.which("timeout"):
        print("[FAIL] needs sh and timeout on PATH")
        sys.exit(1)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n%d failed" % len(FAILS))
    sys.exit(1 if FAILS else 0)
