"""delete_archived.sh deletes a local file only after a checksum AND a restore prove the GCS copy.

Cole on #243: nothing is deleted locally until a checksum match and a restore check prove the
off-boat copy. The script used to delete once `gsutil stat` showed the object existed with a
non-zero size, so a truncated or corrupt object (or another file under that name) was enough to
delete the only good copy. Now it reads the whole object back, decompresses it, and requires
that stream to hash to the local file's sha256 (which must still match the manifest).

Runs the real script against fake_gsutil.py (a local directory as the bucket) and a stand-in
`zstd` (a 4-byte header instead of real compression; anything else fails to decompress).

Run: python3 tools/backup/test_delete_archived.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "delete_archived.sh")
BUCKET = "gs://bucket/pull"
MAGIC = b"ZST1"
ok = True

FAKE_ZSTD = f"""#!/usr/bin/env python3
import sys
data = sys.stdin.buffer.read()
if "-dc" not in sys.argv[1:] or not data.startswith({MAGIC!r}):
    sys.stderr.write("zstd: unknown header\\n")
    sys.exit(1)
sys.stdout.buffer.write(data[{len(MAGIC)}:])
"""


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


with tempfile.TemporaryDirectory() as tmp:
    src, gcs, bin_ = (os.path.join(tmp, d) for d in ("src", "gcs", "bin"))
    obj_dir = os.path.join(gcs, "bucket", "pull")
    for d in (src, obj_dir, bin_):
        os.makedirs(d)
    with open(os.path.join(bin_, "gsutil"), "w") as f:
        f.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, 'fake_gsutil.py')} \"$@\"\n")
    with open(os.path.join(bin_, "zstd"), "w") as f:
        f.write(FAKE_ZSTD)
    for n in ("gsutil", "zstd"):
        os.chmod(os.path.join(bin_, n), 0o755)

    files = {                      # name -> (local bytes, what the bucket holds)
        "good.db": (b"good archive " * 100, "same"),
        "corrupt.db": (b"real rows " * 100, MAGIC + b"real rows " * 99 + b"real rowz "),
        "truncated.db": (b"long file " * 100, MAGIC + b"long file " * 10),
        "garbage.db": (b"some file " * 100, b"\x00" * 1000),          # will not decompress
        "missing.db": (b"never uploaded " * 10, None),
        "changed.db": (b"after the manifest " * 10, "same"),
        "unlisted.db": (b"no manifest entry " * 10, "same"),
        "flaky.db": (b"read fails " * 100, "same"),
    }
    manifest = []
    for name, (data, remote) in files.items():
        with open(os.path.join(src, name), "wb") as f:
            f.write(data)
        h = hashlib.sha256(data).hexdigest()
        if name == "changed.db":
            h = hashlib.sha256(b"what was archived").hexdigest()
        if name != "unlisted.db":
            manifest.append(f"{h}  {name}\n")
        if remote is not None:
            with open(os.path.join(obj_dir, name + ".zst"), "wb") as f:
                f.write(MAGIC + data if remote == "same" else remote)
    man = os.path.join(tmp, "MANIFEST-sha256.txt")
    with open(man, "w") as f:
        f.writelines(manifest)
    fail_spec = os.path.join(tmp, "fail.json")
    with open(fail_spec, "w") as f:
        json.dump({f"cat {BUCKET}/flaky.db.zst": "always"}, f)

    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "SRC": src, "MAN": man,
           "PULL": BUCKET, "FAKEGCS_ROOT": gcs, "FAKEGCS_FAIL": fail_spec}
    r = subprocess.run(["bash", SCRIPT, *files], env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr

    def there(n):
        return os.path.exists(os.path.join(src, n))

    check("a copy that restores to the same sha256 is deleted", not there("good.db"))
    check("a corrupt object (exists, right size) does not license a delete", there("corrupt.db"))
    check("a truncated object does not license a delete", there("truncated.db"))
    check("an object that will not decompress does not license a delete", there("garbage.db"))
    check("no object, no delete", there("missing.db"))
    check("a local file changed since the manifest is kept", there("changed.db"))
    check("a file with no manifest entry is kept", there("unlisted.db"))
    check("a failed read from GCS keeps the file", there("flaky.db"))
    check("every kept file is said, and the run exits non-zero",
          out.count("[SKIP]") == 7 and r.returncode == 1)
    if not ok:
        print(out[-2000:])

    r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)
    check(f"nothing asked, nothing skipped: exits 0 (rc {r.returncode})", r.returncode == 0)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
