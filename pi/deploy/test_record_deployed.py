"""Each deploy leaves a record on the box of what it put there (#187).

record-deployed.sh writes the hash of every copied file as it sits on the box, plus each rebuilt
service's built and running image, to ~/DEPLOYED (and a copy in the deploy's backup folder), and
fails when the box doesn't match the checkout. ssh, git and docker are faked here; the box is a
temp home directory. Also checks the runbooks no longer tell anyone to pull or reset on a box.

Run: python3 pi/deploy/test_record_deployed.py
"""
import hashlib
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, "record-deployed.sh")
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode | stat.S_IRUSR)


tmp = tempfile.mkdtemp()
here, box, bin_ = (os.path.join(tmp, d) for d in ("checkout", "box", "bin"))
X = 0o755
# ssh: drop the options and the host, run the rest locally with the box as $HOME.
write(f"{bin_}/ssh", '#!/bin/bash\nwhile [[ $1 == -o ]]; do shift 2; done\nshift\n'
      f'HOME={box} exec "$@"\n', X)
# git: only what the script asks. The checkout is clean; the box has no git repo.
write(f"{bin_}/git", '#!/bin/bash\ncase "$*" in\n'
      f'  "rev-parse --show-toplevel") echo {here} ;;\n'
      '  "rev-parse --short HEAD") echo abc1234 ;;\n'
      '  status*) ;;\n  *) exit 128 ;;\nesac\n', X)
# docker: the built image is sha256:new; the container runs $RUNNING (default the same).
write(f"{bin_}/docker", '#!/bin/bash\ncase "$1" in\n'
      '  image) echo sha256:new ;;\n  inspect) echo "${RUNNING:-sha256:new}" ;;\nesac\n', X)

FILE = "pi/engine/engine_app.py"
write(f"{here}/{FILE}", "print('v2')\n")
write(f"{box}/Agent_C4/{FILE}", "print('v2')\n")
want = hashlib.sha256(b"print('v2')\n").hexdigest()


def run(*args, **env):
    e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", **env}
    return subprocess.run(["bash", SCRIPT, *args], cwd=here, env=e, capture_output=True, text=True)


r = run("pi", "20261106T000000Z", "w1", FILE, "--", "engine")
check(f"matching deploy records and passes (rc {r.returncode})", r.returncode == 0)
log = open(f"{box}/DEPLOYED").read() if os.path.exists(f"{box}/DEPLOYED") else ""
check("~/DEPLOYED names the deploy and the checkout commit",
      "deploy 20261106T000000Z w1 from abc1234" in log)
check("~/DEPLOYED has the copied file's hash on the box", f"file {want} {FILE}" in log)
check("~/DEPLOYED has the service's built and running image",
      "image engine built sha256:new running sha256:new" in log)
check("the backup folder keeps its own copy",
      os.path.exists(f"{box}/deploy-backups/w1-20261106T000000Z/DEPLOYED"))

r = run("pi", "20261106T010000Z", "w2", FILE, "--", "engine")
log = open(f"{box}/DEPLOYED").read()
check("a second deploy appends (both blocks kept, newest last)",
      log.index("w1 from") < log.index("w2 from"))

write(f"{box}/Agent_C4/{FILE}", "print('v1')\n")
r = run("pi", "20261106T020000Z", "w3", FILE)
check("a file that differs on the box fails", r.returncode == 1 and "differs" in r.stdout)

os.remove(f"{box}/Agent_C4/{FILE}")
r = run("pi", "20261106T030000Z", "w4", FILE)
check("a file missing on the box fails", r.returncode == 1 and "not on the box" in r.stdout)

write(f"{box}/Agent_C4/{FILE}", "print('v2')\n")
r = run("pi", "20261106T040000Z", "w5", FILE, "--", "engine", RUNNING="sha256:old")
check("a container still on the old image fails",
      r.returncode == 1 and "does not run the image just built" in r.stdout)

r = run("pi", "20261106T050000Z", "w6", "a file; rm -rf ~")
check("names with shell characters are refused", r.returncode == 2)

# The runbooks: one deploy method (pi/deploy/README.md), no pull or reset on a box.
for doc in ("docs/REMOTE_OPS.md", "CLAUDE.md"):
    text = open(os.path.join(REPO, doc)).read()
    check(f"{doc} has no `git reset --hard origin/main` deploy", "reset --hard origin/main" not in text)
    check(f"{doc} has no `git pull` deploy", "git pull &&" not in text and "git pull +" not in text)
    check(f"{doc} points at pi/deploy/README.md and the DEPLOYED record",
          "pi/deploy/README.md" in text and "record-deployed.sh" in text)

sys.exit(0 if ok else 1)
