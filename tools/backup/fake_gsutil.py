#!/usr/bin/env python3
"""A stand-in for gsutil over a local directory, for tests only (no network, no bucket).

gs://<bucket>/<path> maps to $FAKEGCS_ROOT/<bucket>/<path>. Supports what lab_backup uses:
ls, cat, cp (both ways), stat (md5), rm. Not-found answers the way gsutil does ("matched no
objects" / "No URLs matched", exit 1). $FAKEGCS_FAIL names a JSON file
{"<cmd> <url>": [call numbers to fail, or "always"], ...}; a failing call prints a 503 (or the
given message) and exits 1, the way a transient service error does.
"""
import base64
import hashlib
import json
import os
import shutil
import sys

ROOT = os.environ["FAKEGCS_ROOT"]


def local(url):
    return os.path.join(ROOT, url[len("gs://"):])


def maybe_fail(cmd, url):
    spec_path = os.environ.get("FAKEGCS_FAIL")
    if not spec_path or not os.path.exists(spec_path):
        return
    spec = json.load(open(spec_path))
    key = f"{cmd} {url}"
    if key not in spec:
        return
    count_path = spec_path + ".count"
    counts = json.load(open(count_path)) if os.path.exists(count_path) else {}
    counts[key] = counts.get(key, 0) + 1
    json.dump(counts, open(count_path, "w"))
    rule = spec[key]
    if rule == "always" or counts[key] in rule.get("calls", []):
        sys.stderr.write(rule.get("msg", "ServiceException: 503 Service Unavailable.")
                         if isinstance(rule, dict) else "AccessDeniedException: 403 retention\n")
        sys.exit(1)


def main(argv):
    args = [a for a in argv if a != "-q"]
    cmd = args[0]
    url = args[1] if args[1].startswith("gs://") else args[2]
    maybe_fail(cmd, url)
    if cmd == "ls":
        d = local(url)
        if not os.path.isdir(d) or not os.listdir(d):
            sys.stderr.write("CommandException: One or more URLs matched no objects.\n")
            return 1
        base = url.rstrip("/") + "/"
        for n in sorted(os.listdir(d)):
            if n.startswith("."):
                continue
            print(base + n + ("/" if os.path.isdir(os.path.join(d, n)) else ""))
        return 0
    if cmd in ("cat", "stat", "rm") and not os.path.isfile(local(url)):
        sys.stderr.write(f"CommandException: No URLs matched: {url}\n")
        return 1
    if cmd == "cat":
        sys.stdout.write(open(local(url)).read())
    elif cmd == "stat":
        if "-q" in argv:                    # like the real gsutil: -q stat prints nothing
            return 0
        h = hashlib.md5(open(local(url), "rb").read()).digest()
        print(f"{url}:\n    Content-Length:    {os.path.getsize(local(url))}\n"
              f"    Hash (md5):    {base64.b64encode(h).decode()}")
    elif cmd == "rm":
        os.remove(local(url))
    elif cmd == "cp":
        src, dst = args[1], args[2]
        if dst.startswith("gs://"):
            os.makedirs(os.path.dirname(local(dst)), exist_ok=True)
            shutil.copyfile(src, local(dst))
        else:
            shutil.copyfile(local(src), dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
