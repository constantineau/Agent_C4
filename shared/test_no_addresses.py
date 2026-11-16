"""No boat's network address or host name is a default in code (#314).

Before: the Lab's Deploy push, its live boat read and the plume push defaulted to one boat's Pi
on its Tailscale address, and the drain and data-path deploy scripts carried that address and the
Pi's ssh host name. A second boat needed overrides everywhere, and the repo is public. Now they
come from env (DEPLOY_PI_ENGINE_URL, PI_HOST, DRAIN_VPS_URL, VPS_URL, VPS_TAILNET_IP,
BENCH_ORIN_URL); unset, the tool says which setting is missing and does nothing.

This file checks that statically: no Tailscale (100.64.0.0/10) address in code, compose files,
units or the example .env, except made-up ones in 100.64.0.x that tests use; and no ssh target or
URL naming a boat's host. Markdown (docs, notes) is not scanned here.

The boat's internal cable addresses (10.10.10.x) stay: they are the onboard wiring plan every boat
is set up with (pi/SETUP.md), and the Orin's allowlists fail closed on them.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


TAILNET = re.compile(r"(?<![\d.])100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.(\d{1,3})\.(\d{1,3})(?![\d])")
# an ssh target or URL host, not a compose project / image / container name (sr33-pi-engine-1)
HOST = re.compile(r"(sr33-pi|agent-c4)@|//sr33-pi(?![-\w])|\bssh\s+sr33-pi(?![-\w])"
                  r"|[\"'=]sr33-pi[\"']")
# a compose-project pin names the project, not a host (C4_BOAT_PROJECT=sr33-pi, #314 PR C)
PROJECT_PIN = re.compile(r"(?:C4_(?:DEV|PROD|BOAT|BENCH)_PROJECT|BOX_PROJECT)\W{1,4}sr33-pi[\"']")
CODE = (".py", ".sh", ".yml", ".yaml", ".js", ".mjs", ".json", ".example", ".service",
        ".template", ".conf", ".env", ".toml", ".cfg", ".html")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "vendor"}
SELF = os.path.join("shared", "test_no_addresses.py")

print("the patterns:")
check("a real-looking Tailscale address is caught", TAILNET.search("http://100.101.2.3:8200"))
check("an ordinary address is not", not TAILNET.search("http://100.1.2.3/ 10.10.10.1 1100.70.1.2"))
check("an ssh target is caught", HOST.search("PI=sr33-pi@host") and HOST.search("ssh sr33-pi 'x'"))
check("a container name is not", not HOST.search("docker exec sr33-pi-engine-1 ls"))
check("the compose project name is not", not HOST.search("name: sr33-pi"))
check("nor a project pin", not HOST.search(PROJECT_PIN.sub("", '"C4_BOAT_PROJECT": "sr33-pi",')))
check("but a pin does not hide an ssh target on the same line",
      HOST.search(PROJECT_PIN.sub("", 'C4_BOAT_PROJECT="sr33-pi" PI=sr33-pi@host')))

print("no address or host defaults in code:")
addr_hits, host_hits = [], []
for d, dirs, files in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
    for f in files:
        rel = os.path.relpath(os.path.join(d, f), ROOT)
        if rel == SELF or not (f.endswith(CODE) or f == "Dockerfile" or f.startswith("compose.")):
            continue
        is_test = f.startswith("test_") or ".test." in f
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                for m in TAILNET.finditer(line):
                    if not (is_test and m.group(1) == "64" and m.group(2) == "0"):
                        addr_hits.append(f"{rel}:{i}")
                if HOST.search(PROJECT_PIN.sub("", line)):
                    host_hits.append(f"{rel}:{i}")
check("no Tailscale address" + (f" ({addr_hits})" if addr_hits else ""), not addr_hits)
check("no boat host as an ssh target or URL" + (f" ({host_hits})" if host_hits else ""),
      not host_hits)

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
