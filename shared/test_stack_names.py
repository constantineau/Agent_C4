"""Stack names: neutral defaults for a new install, the live names on a pinned box (#314 PR C).

Every compose project, database volume and container takes its name from the compose files. The
defaults are neutral (c4-dev, c4-prod, c4-boat, c4-bench). The live boxes were set up under
sr33-* names and keep them through pins in their own .env (C4_DEV_PROJECT, C4_DEV_PGDATA, ...),
so a rebuild there must resolve to EXACTLY today's names: same containers, same volumes, same
database. A volume whose resolved name changes is a database or boat archive that silently
starts empty. This test resolves each compose file twice, with the pins and without, and checks
both lists.

It also checks the in-container data paths: every env var that points into a Pi volume
(ARCHIVE_DB, ENGINE_DB, QUEUE_DIR) sits under that volume's mount, and the code's fallback
defaults agree with compose.

The CI image has no docker, so the files are resolved here (YAML + ${VAR:-default}
interpolation). Where `docker compose` is available (on the VPS, run by hand) the same
scenarios also go through `docker compose config` and must agree with this resolver.

Run: python3 shared/test_stack_names.py
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# What each box runs under today (docker ps / docker volume ls on 2026-11-13). The pins below are
# what each box's .env carries; with them, the names must not move.
PINS = {
    "vps": {"C4_DEV_PROJECT": "sr33-dev", "C4_PROD_PROJECT": "sr33-prod",
            "C4_DEV_PGDATA": "sr33-dev_sr33_dev_pgdata",
            "C4_PROD_PGDATA": "sr33-prod_sr33_prod_pgdata",
            "POSTGRES_DB": "sr33_dev", "POSTGRES_USER": "sr33"},
    "pi": {"C4_BOAT_PROJECT": "sr33-pi"},
    "bench": {"C4_BENCH_PROJECT": "sr33-pi"},
}
LAB = ["coastline", "enc", "gribcache", "ingested", "learning", "playbooks", "plume", "retro",
       "shares"]
PI_VOLS = ["engine_state", "sk_archive", "sk_config", "sk_queue"]
DEV, PROD = PINS["vps"]["C4_DEV_PROJECT"], PINS["vps"]["C4_PROD_PROJECT"]
BOAT, BENCH = PINS["pi"]["C4_BOAT_PROJECT"], PINS["bench"]["C4_BENCH_PROJECT"]
LIVE = {
    "compose.dev.yml": (DEV, [PINS["vps"]["C4_DEV_PGDATA"]] + [f"{DEV}_lab_{v}" for v in LAB]),
    "compose.prod.yml": (PROD, [f"{PROD}_certbot_webroot", f"{PROD}_letsencrypt",
                                PINS["vps"]["C4_PROD_PGDATA"]]),
    "compose.pi.yml": (BOAT, [f"{BOAT}_{v}" for v in PI_VOLS]),
    "compose.pi.yml+compose.pi.sample.yml": (BENCH, [f"{BENCH}_{v}" for v in PI_VOLS]),
}
NEUTRAL = {
    "compose.dev.yml": ("c4-dev", ["c4-dev_pgdata"] + [f"c4-dev_lab_{v}" for v in LAB]),
    "compose.prod.yml": ("c4-prod", ["c4-prod_certbot_webroot", "c4-prod_letsencrypt",
                                     "c4-prod_pgdata"]),
    "compose.pi.yml": ("c4-boat", [f"c4-boat_{v}" for v in PI_VOLS]),
    "compose.pi.yml+compose.pi.sample.yml": ("c4-bench", [f"c4-bench_{v}" for v in PI_VOLS]),
}
BOX = {"compose.dev.yml": "vps", "compose.prod.yml": "vps", "compose.pi.yml": "pi",
       "compose.pi.yml+compose.pi.sample.yml": "bench"}

# ${VAR}, ${VAR:-d}, ${VAR-d}, ${VAR:?e}, ${VAR?e}, ${VAR:+a}, ${VAR+a}, $VAR, and $$ (a literal $)
VAR = re.compile(r"\$\$|\$\{(\w+)(?:(:?[-?+])([^}]*))?\}|\$(\w+)")


def interpolate(text, env):
    def one(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(4)
        op, arg = m.group(2), m.group(3) or ""
        val = env.get(name)
        empty = val is None or (op is not None and op.startswith(":") and val == "")
        if op in ("-", ":-"):
            return arg if empty else val
        if op in ("?", ":?"):
            return "<required>" if empty else val
        if op in ("+", ":+"):
            return "" if empty else arg
        return val or ""
    return VAR.sub(one, text)


def resolve(files, env):
    """(project, sorted volume names, {service: container name}, merged services) as compose
    would name them: the last file's `name:` wins; a volume is its `name:` or <project>_<key>."""
    project, volumes, services = None, {}, {}
    for f in files:
        with open(os.path.join(ROOT, f)) as fh:
            doc = yaml.safe_load(fh)
        if doc.get("name"):
            project = interpolate(str(doc["name"]), env)
        for k, v in (doc.get("volumes") or {}).items():
            volumes[k] = {**volumes.get(k, {}), **(v or {})}
        for s, v in (doc.get("services") or {}).items():
            services.setdefault(s, {}).update(v or {})
    names = sorted(interpolate(v["name"], env) if v.get("name") else f"{project}_{k}"
                   for k, v in volumes.items())
    containers = {s: v.get("container_name") or f"{project}-{s}-1" for s, v in services.items()}
    return project, names, containers, services


def compose_config(files, env):
    """The same through `docker compose config` (None when docker compose is not here)."""
    if not shutil.which("docker"):
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as ef:
        for k, v in env.items():
            ef.write(f"{k}={v}\n")
    args = ["docker", "compose", "--project-directory", ROOT, "--env-file", ef.name]
    for f in files:
        args += ["-f", os.path.join(ROOT, f)]
    clean = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
             if k in os.environ}
    try:
        r = subprocess.run(args + ["config", "--format", "json"], capture_output=True, text=True,
                           env=clean, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        os.unlink(ef.name)
    if r.returncode != 0:
        if "is not a docker command" in r.stderr or "unknown command" in r.stderr:
            return None   # docker without the compose plugin
        raise SystemExit(f"docker compose config failed for {files}:\n{r.stderr}")
    cfg = json.loads(r.stdout)
    return cfg["name"], sorted(v["name"] for v in (cfg.get("volumes") or {}).values())


# Values for the settings compose.*.yml require (${X:?...}); only names are checked here.
REQUIRED = {k: "x" for k in ("POSTGRES_PASSWORD", "DATABASE_URL", "BOAT_ID", "BOAT_PASSWORD",
                             "DEPLOY_BOAT_ID", "ANTHROPIC_API_KEY", "INGEST_TOKEN", "AUTH_SECRET",
                             "SERVER_NAME", "BENCH_ORIN_URL")}
REQUIRED["VPS_TAILNET_IP"] = "127.0.0.2"

print("compose names, pinned box vs new install:")
have_compose = None
for key in LIVE:
    files = key.split("+")
    for label, env, want in (("pinned", {**REQUIRED, **PINS[BOX[key]]}, LIVE[key]),
                             ("new install", dict(REQUIRED), NEUTRAL[key])):
        project, vols, containers, _ = resolve(files, env)
        check(f"{key} {label}: project {project}", project == want[0])
        check(f"{key} {label}: volumes {vols}", vols == sorted(want[1]))
        check(f"{key} {label}: containers are <project>-<service>-1",
              all(c == f"{want[0]}-{s}-1" for s, c in containers.items()))
        real = compose_config(files, env)
        if real is not None:
            have_compose = True
            check(f"{key} {label}: docker compose config agrees ({real[0]}, {len(real[1])} volumes)",
                  real == (project, vols))
if not have_compose:
    print("  [SKIP] docker compose is not available here: names checked by this file's resolver only")

print("the database defaults:")
for f, db in (("compose.dev.yml", "c4_dev"), ("compose.prod.yml", "c4_prod")):
    _, _, _, services = resolve([f], dict(REQUIRED))
    env = services["timescaledb"]["environment"]
    check(f"{f}: new install user c4, database {db}",
          interpolate(env["POSTGRES_USER"], {}) == "c4" and interpolate(env["POSTGRES_DB"], {}) == db)
    check(f"{f}: the pinned user and database win",
          interpolate(env["POSTGRES_DB"], PINS["vps"]) == "sr33_dev"
          and interpolate(env["POSTGRES_USER"], PINS["vps"]) == "sr33")
    with open(os.path.join(ROOT, f)) as fh:
        doc = yaml.safe_load(fh)
    check(f"{f}: no project name hard-coded (each file reads its own C4_*_PROJECT, not "
          f"COMPOSE_PROJECT_NAME: dev and prod share one .env)",
          re.fullmatch(r"\$\{C4_(DEV|PROD)_PROJECT:-c4-(dev|prod)\}", doc["name"]))
    pg = [v for v in services["timescaledb"]["volumes"] if v.endswith(":/var/lib/postgresql/data")]
    key = pg[0].split(":")[0] if pg else None
    check(f"{f}: the database volume ({key}) has an explicit, pinnable name",
          key and re.fullmatch(r"\$\{C4_(DEV|PROD)_PGDATA:-c4-(dev|prod)_pgdata\}",
                               (doc["volumes"][key] or {}).get("name", "")))

print("in-container data paths (compose.pi.yml):")
_, _, _, services = resolve(["compose.pi.yml"], {})
POINTS = {"ARCHIVE_DB": "sk_archive", "ENGINE_DB": "engine_state", "QUEUE_DIR": "sk_queue"}
paths = {}
for s, spec in services.items():
    mounts = {}
    for m in spec.get("volumes") or []:
        parts = m.split(":")
        if parts[0] in POINTS.values():
            mounts[parts[0]] = parts[1]
    for var, vol in POINTS.items():
        val = (spec.get("environment") or {}).get(var) if isinstance(spec.get("environment"), dict) else None
        if val is None:
            continue
        paths.setdefault(var, set()).add(val)
        under = mounts.get(vol)
        check(f"{s}: {var}={val} is inside its {vol} mount ({under})",
              under is not None and (val == under or val.startswith(under.rstrip("/") + "/")))
        check(f"{s}: {var} is under /var/lib/c4/", val.startswith("/var/lib/c4/"))
check("each path variable has one value across services",
      paths and all(len(v) == 1 for v in paths.values()))

print("code defaults agree with compose:")
DEFAULT = re.compile(r"""environ(?:\.get\(|\[)?\s*["'](ARCHIVE_DB|ENGINE_DB|QUEUE_DIR)["']\s*,\s*["']([^"']+)["']""")
found = 0
for path in sorted(glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True)):
    rel = os.path.relpath(path, ROOT)
    if os.path.basename(rel).startswith("test_") or "/.venv/" in rel:
        continue
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            for var, val in DEFAULT.findall(line):
                found += 1
                want = next(iter(paths.get(var, {None})))
                if val != want:
                    check(f"{rel}:{i}: {var} default {val} == compose {want}", False)
check(f"{found} code defaults for ARCHIVE_DB / ENGINE_DB / QUEUE_DIR all match compose", found >= 8)

sys.exit(0 if ok else 1)
