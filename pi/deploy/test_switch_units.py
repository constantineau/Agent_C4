"""Switching a box's systemd units from sr33-* to c4-* (#314 PR D).

Runs pi/deploy/switch-units-box.sh against a fake box: ROOT is a temp directory holding etc/ and
run/, and systemctl is faked (it keeps each unit's enabled/active state in files and logs every
call). Checks the dry run changes nothing, a switch carries each unit's state over in the right
order, the Pi's can0 is never stopped or started, config is copied and never overwritten, and a
rollback puts the old units back.

Run: python3 pi/deploy/test_switch_units.py
"""
import glob
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, "switch-units-box.sh")
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def read(path):
    try:
        return open(path).read()
    except FileNotFoundError:
        return None


tmp = tempfile.mkdtemp()
BIN = os.path.join(tmp, "bin")
# systemctl: state in $ROOT/state/<unit>.{en,act}; every call logged to $ROOT/calls.log. Starting
# the shed guard writes its state file, as the real one does. FAIL_START names a unit that won't run.
write(f"{BIN}/systemctl", r'''#!/bin/bash
S=$ROOT/state; mkdir -p "$S"; echo "$*" >> "$ROOT/calls.log"
cmd=$1; shift; now=0; [[ $1 == --now ]] && { now=1; shift; }; u=$1
case $cmd in
  is-enabled) cat "$S/$u.en" 2>/dev/null || { echo not-found; exit 1; } ;;
  is-active) a=$(cat "$S/$u.act" 2>/dev/null || echo inactive); echo "$a"; [[ $a == active ]] ;;
  enable) echo enabled > "$S/$u.en" ;;
  disable) echo disabled > "$S/$u.en"; ((now)) && echo inactive > "$S/$u.act" ;;
  start) [[ $u == "$FAIL_START" ]] && { echo failed > "$S/$u.act"; exit 1; }
         echo active > "$S/$u.act"
         [[ $u == c4-orin-shed.service ]] && { mkdir -p "$ROOT/run/c4-shed"; echo '{}' > "$ROOT/run/c4-shed/state.json"; }
         [[ $u == sr33-orin-shed.service ]] && { mkdir -p "$ROOT/run/sr33-shed"; echo '{}' > "$ROOT/run/sr33-shed/state.json"; }
         true ;;
  stop) echo inactive > "$S/$u.act" ;;
esac
exit 0
''', 0o755)
write(f"{BIN}/sleep", "#!/bin/bash\nexit 0\n", 0o755)

ORIN_UNITS = {   # old unit -> (enabled, active), as the live Orin had them
    "sr33-orin-shed.service": ("enabled", "active"),
    "sr33-orin-copilot.service": ("enabled", "active"),
    "sr33-orin-copilot-dashboard.service": ("disabled", "inactive"),
    "sr33-orin-reroute.service": ("enabled", "active"),
    "sr33-orin-plume.timer": ("enabled", "active"),
    "sr33-orin-plume.service": ("static", "inactive"),
}


def make_box(units, reroute_env=None, c4_env=None):
    root = tempfile.mkdtemp(dir=tmp)
    sysd = f"{root}/etc/systemd/system"
    for u, (en, act) in units.items():
        body = "[Service]\nExecStart=/bin/true\n"
        if u == "sr33-orin-reroute.service":
            body += "Environment=REROUTE_BOAT=bravo\n"
        write(f"{sysd}/{u}", f"# old {u}\n{body}")
        write(f"{root}/state/{u}.en", en + "\n")
        write(f"{root}/state/{u}.act", act + "\n")
    if any(u.startswith("sr33-orin") for u in units):
        write(f"{sysd}/ollama.service.d/sr33-oom.conf", "[Service]\nOOMScoreAdjust=500\n")
        write(f"{root}/etc/sr33/copilot.env", "ENGINE_URL=http://engine\n", 0o600)
        if reroute_env is not None:
            write(f"{root}/etc/sr33/reroute.env", reroute_env)
    for name, text in (c4_env or {}).items():
        write(f"{root}/etc/c4/{name}", text)
    return root


def run(root, *args, **env):
    e = {**os.environ, "PATH": f"{BIN}:{os.environ['PATH']}", "ROOT": root, "WAIT_S": "2", **env}
    return subprocess.run(["bash", SCRIPT, *args], env=e, capture_output=True, text=True)


def calls(root):
    return (read(f"{root}/calls.log") or "").splitlines()


def st(root, unit, kind):
    return (read(f"{root}/state/{unit}.{kind}") or "").strip()


def snapshot(root):
    out = {}
    for p in glob.glob(f"{root}/etc/**", recursive=True):
        if os.path.isfile(p):
            out[p] = read(p)
    return out


print("orin, dry run")
box = make_box(ORIN_UNITS)
before = snapshot(box)
bk = f"{box}/backup"
r = run(box, "orin", REPO, bk)
check(f"dry run exits cleanly (rc {r.returncode}) {r.stderr.strip()}", r.returncode == 0)
check("says it's a dry run", "Dry run" in r.stdout)
check("lists each unit and its new name",
      all(f"{u} " in r.stdout and u.replace("sr33-", "c4-") in r.stdout for u in ORIN_UNITS))
check("says REROUTE_BOAT moves into /etc/c4/reroute.env", "REROUTE_BOAT=bravo goes into" in r.stdout)
check("changes no file", snapshot(box) == before and not os.path.exists(bk))
check("only asks systemctl questions", all(c.split()[0].startswith("is-") for c in calls(box)))

print("orin, apply")
r = run(box, "orin", REPO, bk, "--apply")
check(f"switch exits cleanly (rc {r.returncode}) {r.stderr.strip()}", r.returncode == 0)
sysd = f"{box}/etc/systemd/system"
for old, (en, act) in ORIN_UNITS.items():
    new = old.replace("sr33-", "c4-")
    check(f"{new} installed from the checkout",
          read(f"{sysd}/{new}") == read(f"{REPO}/pi/systemd/{new}"))
    check(f"{old} removed, backed up", not os.path.exists(f"{sysd}/{old}")
          and read(f"{bk}/{old}").startswith(f"# old {old}"))
    if en == "enabled":
        check(f"{new} enabled, {old} disabled", st(box, new, "en") == "enabled"
              and st(box, old, "en") == "disabled")
check("running services run under the new name",
      all(st(box, u, "act") == "active" for u in
          ("c4-orin-shed.service", "c4-orin-copilot.service", "c4-orin-reroute.service",
           "c4-orin-plume.timer")))
check("the old ones are stopped",
      all(st(box, u, "act") == "inactive" for u in ORIN_UNITS))
check("the dashboard unit stays off, as it was",
      st(box, "c4-orin-copilot-dashboard.service", "act") in ("", "inactive")
      and st(box, "c4-orin-copilot-dashboard.service", "en") in ("", "disabled"))
check("the plume pass is left to its timer (never started by hand)",
      "start c4-orin-plume.service" not in calls(box))
log = calls(box)
check("the old plume pass is stopped before the new timer starts (it may fire at once)",
      log.index("stop sr33-orin-plume.service") < log.index("start c4-orin-plume.timer"))
starts = [c.split()[1] for c in log if c.startswith("start ")]
check(f"the shed guard starts first ({starts})", starts and starts[0] == "c4-orin-shed.service")
check("the shed writes its new state file", os.path.exists(f"{box}/run/c4-shed/state.json"))
check("config copied to /etc/c4 as is, mode kept",
      read(f"{box}/etc/c4/copilot.env") == "ENGINE_URL=http://engine\n"
      and stat.S_IMODE(os.stat(f"{box}/etc/c4/copilot.env").st_mode) == 0o600)
check("/etc/sr33 is left in place", read(f"{box}/etc/sr33/copilot.env") == "ENGINE_URL=http://engine\n")
check("REROUTE_BOAT moved into /etc/c4/reroute.env",
      "\nREROUTE_BOAT=bravo\n" in "\n" + (read(f"{box}/etc/c4/reroute.env") or ""))
check("ollama drop-in renamed, same setting",
      not os.path.exists(f"{sysd}/ollama.service.d/sr33-oom.conf")
      and "OOMScoreAdjust=500" in read(f"{sysd}/ollama.service.d/c4-oom.conf"))
check("says where the backup is", "Backup:" in r.stdout and os.path.exists(f"{bk}/plan"))

print("orin, run again")
r = run(box, "orin", REPO, f"{box}/backup2", "--apply")
check(f"nothing left to switch (rc {r.returncode})", r.returncode == 0 and "Nothing to switch" in r.stdout)

print("orin, rollback")
r = run(box, "--rollback", bk)
check("rollback dry run changes nothing", r.returncode == 0 and "Dry run" in r.stdout
      and os.path.exists(f"{sysd}/c4-orin-copilot.service"))
r = run(box, "--rollback", bk, "--apply")
check(f"rollback exits cleanly (rc {r.returncode}) {r.stderr.strip()}", r.returncode == 0)
for old, (en, act) in ORIN_UNITS.items():
    new = old.replace("sr33-", "c4-")
    check(f"{old} back, {new} gone", os.path.exists(f"{sysd}/{old}")
          and not os.path.exists(f"{sysd}/{new}"))
check("the old units run again",
      all(st(box, u, "act") == "active" for u, (_, a) in ORIN_UNITS.items() if a == "active"))
check("and are enabled again",
      all(st(box, u, "en") == "enabled" for u, (e, _) in ORIN_UNITS.items() if e == "enabled"))
log = calls(box)
check("rollback stops the new plume pass before the old timer starts",
      log.index("stop c4-orin-plume.service") < log.index("start sr33-orin-plume.timer"))
check("the old drop-in is back", os.path.exists(f"{sysd}/ollama.service.d/sr33-oom.conf")
      and not os.path.exists(f"{sysd}/ollama.service.d/c4-oom.conf"))
check("rollback says to put the Orin checkout back too", "checkout back" in r.stdout)

print("orin, config already in /etc/c4")
box = make_box(ORIN_UNITS, reroute_env="REROUTE_BOAT=charlie\n",
               c4_env={"copilot.env": "ENGINE_URL=http://newer\n"})
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
check(f"switch exits cleanly (rc {r.returncode})", r.returncode == 0)
check("an existing /etc/c4 file is never overwritten",
      read(f"{box}/etc/c4/copilot.env") == "ENGINE_URL=http://newer\n")
check("REROUTE_BOAT already in reroute.env: not added again",
      read(f"{box}/etc/c4/reroute.env") == "REROUTE_BOAT=charlie\n")

print("orin, /etc/c4/reroute.env has settings but no boat; /etc/sr33/reroute.env has the boat")
box = make_box(ORIN_UNITS, reroute_env="REROUTE_BOAT=delta\n",
               c4_env={"reroute.env": "REROUTE_MODELS=gfs"})   # no final newline
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
env = read(f"{box}/etc/c4/reroute.env")
check(f"the old env file's boat is added, settings kept (rc {r.returncode})",
      r.returncode == 0 and env.startswith("REROUTE_MODELS=gfs\n") and "\nREROUTE_BOAT=delta\n" in env
      and "bravo" not in env)

print("orin, /etc/sr33/reroute.env without a boat: the unit's inline one is added to the copy")
box = make_box(ORIN_UNITS, reroute_env="REROUTE_MODELS=hrrr\n")
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
env = read(f"{box}/etc/c4/reroute.env")
check(f"copy plus the unit's boat (rc {r.returncode})",
      r.returncode == 0 and env.startswith("REROUTE_MODELS=hrrr\n") and "\nREROUTE_BOAT=bravo\n" in env)

print("orin, a unit with overrides: refused before any change")
for folder in ("sr33-orin-copilot.service.d", "c4-orin-shed.service.d"):
    box = make_box(ORIN_UNITS)
    write(f"{box}/etc/systemd/system/{folder}/x.conf", "[Service]\nNice=1\n")
    before = snapshot(box)
    r = run(box, "orin", REPO, f"{box}/backup", "--apply")
    check(f"{folder}: stops, says move them by hand, nothing changed (rc {r.returncode})",
          r.returncode != 0 and "by hand" in r.stderr and snapshot(box) == before)

print("orin, the boat set quoted beside another variable")
box = make_box(ORIN_UNITS)
u = f"{box}/etc/systemd/system/sr33-orin-reroute.service"
write(u, read(u).replace("Environment=REROUTE_BOAT=bravo", 'Environment=X=1 "REROUTE_BOAT=echo"'))
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
check(f"the quoted boat is moved (rc {r.returncode})",
      r.returncode == 0 and "\nREROUTE_BOAT=echo\n" in "\n" + read(f"{box}/etc/c4/reroute.env"))

print("orin, an indented boat in the old env file; both names installed")
box = make_box(ORIN_UNITS, reroute_env="  REROUTE_BOAT=foxtrot\n")
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
check(f"the indented boat is kept, the unit's not added (rc {r.returncode})",
      r.returncode == 0 and "bravo" not in read(f"{box}/etc/c4/reroute.env"))
box = make_box(ORIN_UNITS)
write(f"{box}/etc/systemd/system/c4-orin-copilot.service", "# mine\n")
before = snapshot(box)
r = run(box, "orin", REPO, f"{box}/backup", "--apply")
check(f"both names installed: stops before changing anything (rc {r.returncode})",
      r.returncode != 0 and "both" in r.stderr and snapshot(box) == before)

print("orin, a new unit won't start")
box = make_box(ORIN_UNITS)
r = run(box, "orin", REPO, f"{box}/backup", "--apply", FAIL_START="c4-orin-copilot.service")
check(f"stops with an error (rc {r.returncode})", r.returncode != 0)
check("names the unit and how to roll back",
      "c4-orin-copilot.service" in r.stderr and "--rollback" in r.stderr)

print("pi")
box = make_box({"sr33-can0.service": ("enabled", "active")})
r = run(box, "pi", REPO, f"{box}/backup", "--apply")
check(f"switch exits cleanly (rc {r.returncode}) {r.stderr.strip()}", r.returncode == 0)
log = calls(box)
check("can0 is never stopped or started",
      not any(c.split()[0] in ("stop", "start") or "--now" in c for c in log))
check("c4-can0 enabled for next boot, sr33-can0 disabled",
      st(box, "c4-can0.service", "en") == "enabled" and st(box, "sr33-can0.service", "en") == "disabled")
check("the old unit file is gone, the new one installed",
      not os.path.exists(f"{box}/etc/systemd/system/sr33-can0.service")
      and os.path.exists(f"{box}/etc/systemd/system/c4-can0.service"))
r = run(box, "--rollback", f"{box}/backup", "--apply")
log = calls(box)
check(f"pi rollback never stops or starts can0 either (rc {r.returncode})",
      r.returncode == 0 and not any(c.split()[0] in ("stop", "start") or "--now" in c for c in log))
check("sr33-can0 enabled again", st(box, "sr33-can0.service", "en") == "enabled")

print("the repo's units")
names = sorted(os.path.relpath(p, f"{REPO}/pi/systemd")
               for p in glob.glob(f"{REPO}/pi/systemd/**/*", recursive=True) if os.path.isfile(p))
check(f"every unit file is named c4-* ({names})",
      all(os.path.basename(n).startswith("c4-") for n in names))
check("no unit sets a boat id (REROUTE_BOAT lives in /etc/c4/reroute.env)",
      not any("REROUTE_BOAT=" in l and not l.lstrip().startswith("#")
              for n in names for l in open(f"{REPO}/pi/systemd/{n}")))

shutil.rmtree(tmp, ignore_errors=True)
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
