"""Orin load shedding (#248): under memory, heat or power pressure, shed narration first, then the
plume, then re-route. Never the engine, recording or the iPad display (those live on the Pi).

Levels (each includes the ones below it):
  0  normal
  1  narration paused: the copilot makes no LLM calls (deterministic text only)
  2  also no plume passes (the timer still fires; each pass exits at once)
  3  also no new re-route runs ("Re-route paused: Orin low on memory."); a run already going finishes

How it works: this file runs as a small daemon (sr33-orin-shed.service). Every SHED_INTERVAL_S it
reads MemAvailable (/proc/meminfo), the junction temperature (thermal zone "tj-thermal") and the
over-current throttle counters (hwmon "soctherm_oc"), works out the level, and writes it to
SHED_STATE (a JSON file under /run). The copilot, the plume and the re-route service only read that
file (`paused(...)`). If the file is missing or older than SHED_STALE_S (the guard is stopped or
dead), they run as normal: the guard can only ever take load away, never block the boat.

Hysteresis, two kinds:
  * value: each signal enters a level at one threshold and leaves it only past a looser one;
  * time: the overall level drops only after SHED_HOLD_S with no signal asking for it. That also
    covers the model unload below, which frees ~5 GB at once and would otherwise flap.

On entering a memory level the guard asks Ollama to unload its model (SHED_UNLOAD_MODEL=1), after
the new level is written, and again every 30 s while memory stays short if a model is loaded. With
narration paused nothing would use it, and Ollama would otherwise hold the ~5 GB for its 5 min
keep-alive. Heat and power don't unload: an idle model draws no GPU power.

Stdlib only; runs on the Orin's system python3. Config: /etc/sr33/shed.env (see DEPLOYMENT.md §10).
"""
from __future__ import annotations

import glob
import json
import os
import sys
import threading
import time
import urllib.request

LEVEL_OF = {"narration": 1, "plume": 2, "reroute": 3}
NAMES = {"narration": "Narration", "plume": "Plume", "reroute": "Re-route"}
CAUSE_WORDS = {"memory": "Orin low on memory", "heat": "Orin running hot",
               "power": "Orin short of power"}
PAUSED_AT = {1: ["narration"], 2: ["narration", "plume"], 3: ["narration", "plume", "reroute"]}

DEFAULT_STATE = "/run/sr33-shed/state.json"
DEFAULTS = {
    # MemAvailable, MB: enter level 1/2/3 below these ...
    "SHED_MEM_MB": "750,600,500",
    # ... and leave each level only above these
    "SHED_MEM_CLEAR_MB": "950,850,750",
    # junction temperature, deg C: enter level 1/2/3 at or above these, leave below the clear ones
    "SHED_TJ_C": "85,90,95",
    "SHED_TJ_CLEAR_C": "80,85,90",
    # level asked for while the over-current throttle counters are rising (0 = ignore power)
    "SHED_POWER_LEVEL": "1",
    "SHED_HOLD_S": "300",
    "SHED_INTERVAL_S": "5",
    "SHED_STALE_S": "30",
    "SHED_UNLOAD_MODEL": "1",
    "OLLAMA_URL": "http://127.0.0.1:11434",
}


def _log(msg):
    print(f"[shed] {msg}", flush=True)


# ---- reading the state (the copilot, plume and re-route call these) ---------------------------
def state_path():
    return os.environ.get("SHED_STATE", DEFAULT_STATE)


def read_state(path=None, now=None, stale_s=None):
    """The guard's current state → dict with at least `level`. Missing, unreadable or stale (the
    guard isn't running) → level 0 with `guard` saying why: fail open, never block on a dead guard."""
    path = path or state_path()
    now = time.time() if now is None else now
    stale_s = float(os.environ.get("SHED_STALE_S", DEFAULTS["SHED_STALE_S"])) \
        if stale_s is None else stale_s
    try:
        with open(path) as f:
            st = json.load(f)
        level = int(st.get("level") or 0)
        updated = float(st.get("updated") or 0)
    except FileNotFoundError:
        return {"level": 0, "guard": "off"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"level": 0, "guard": "unreadable"}
    if now - updated > stale_s:
        return {"level": 0, "guard": "stale", "updated": updated}
    st["level"] = max(0, min(3, level))
    st["guard"] = "on"
    return st


def message(what, state):
    """'Re-route paused: Orin low on memory.' for the state's main cause."""
    words = CAUSE_WORDS.get(state.get("cause"), "Orin overloaded")
    return f"{NAMES[what]} paused: {words}."


def paused(what, path=None, now=None):
    """None when `what` (narration | plume | reroute) may run; else the plain sentence why not."""
    st = read_state(path, now)
    if st["level"] >= LEVEL_OF[what]:
        return message(what, st)
    return None


# ---- readings ---------------------------------------------------------------------------------
def read_mem_available_mb(meminfo="/proc/meminfo"):
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_tj_c(thermal_root="/sys/class/thermal"):
    """The junction temperature (zone type tj-thermal); without one, the hottest readable zone."""
    temps, tj = [], None
    for z in sorted(glob.glob(os.path.join(thermal_root, "thermal_zone*"))):
        try:
            with open(os.path.join(z, "type")) as f:
                kind = f.read().strip()
            with open(os.path.join(z, "temp")) as f:
                t = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue            # some zones (cv*) answer "No data available"
        temps.append(t)
        if kind == "tj-thermal":
            tj = t
    if tj is not None:
        return tj
    return max(temps) if temps else None


def read_oc_events(hwmon_root="/sys/class/hwmon"):
    """Sum of the over-current throttle event counters (hwmon soctherm_oc), None if absent."""
    total, seen = 0, False
    for h in glob.glob(os.path.join(hwmon_root, "hwmon*")):
        try:
            with open(os.path.join(h, "name")) as f:
                if f.read().strip() != "soctherm_oc":
                    continue
        except OSError:
            continue
        for c in glob.glob(os.path.join(h, "oc*_event_cnt")):
            try:
                with open(c) as f:
                    total += int(f.read().strip())
                seen = True
            except (OSError, ValueError):
                continue
    return total if seen else None


# ---- the decision -----------------------------------------------------------------------------
def signal_level(cur, value, enter, clear, low_is_bad):
    """One signal's level with value hysteresis: enter level k past enter[k-1]; once at k or above,
    stay at k until past clear[k-1]. No reading → 0."""
    if value is None:
        return 0
    for k in (3, 2, 1):
        e, c = enter[k - 1], clear[k - 1]
        if low_is_bad:
            hit, holding = value < e, value < c
        else:
            hit, holding = value >= e, value > c
        if hit or (cur >= k and holding):
            return k
    return 0


def _triple(s, name):
    v = [float(x) for x in str(s).split(",")]
    if len(v) != 3:
        raise ValueError(f"{name} needs three numbers")
    return v


def load_config(env=None):
    """Thresholds from the environment, checked. A bad value logs and falls back to the default:
    a typo in shed.env must not stop the guard."""
    env = os.environ if env is None else env
    cfg = {}
    for key in DEFAULTS:
        cfg[key] = env.get(key, DEFAULTS[key])

    def build(c):
        mem, memc = _triple(c["SHED_MEM_MB"], "SHED_MEM_MB"), \
            _triple(c["SHED_MEM_CLEAR_MB"], "SHED_MEM_CLEAR_MB")
        tj, tjc = _triple(c["SHED_TJ_C"], "SHED_TJ_C"), _triple(c["SHED_TJ_CLEAR_C"], "SHED_TJ_CLEAR_C")
        if not (mem[0] > mem[1] > mem[2] > 0) or any(memc[i] <= mem[i] for i in range(3)):
            raise ValueError("memory thresholds must fall level by level, each clear above its enter")
        if not (tj[0] < tj[1] < tj[2]) or any(tjc[i] >= tj[i] for i in range(3)):
            raise ValueError("heat thresholds must rise level by level, each clear below its enter")
        power = int(c["SHED_POWER_LEVEL"])
        if not 0 <= power <= 3:
            raise ValueError("SHED_POWER_LEVEL must be 0-3")
        return {"mem": mem, "mem_clear": memc, "tj": tj, "tj_clear": tjc, "power": power,
                "hold_s": float(c["SHED_HOLD_S"]), "interval_s": float(c["SHED_INTERVAL_S"]),
                "unload": c["SHED_UNLOAD_MODEL"].strip() not in ("0", "false", "no", ""),
                "ollama": c["OLLAMA_URL"].rstrip("/")}

    try:
        return build(cfg)
    except (ValueError, TypeError) as exc:
        _log(f"bad config ({exc}); using the defaults")
        return build(dict(DEFAULTS))


class Guard:
    """The level from readings, with both hysteresis kinds. `step(readings, now)` → state dict.
    Readings: mem_mb, tj_c, oc_events (any may be None)."""

    UNLOAD_EVERY_S = 30.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.sig = {"memory": 0, "heat": 0, "power": 0}
        self.last_ask = {1: None, 2: None, 3: None}   # last time any signal asked for level k
        self.level = 0
        self.since = None
        self.cause = None
        self.last_oc = None
        self.last_unload = None
        self.now = None

    def unload_due(self):
        """True when the caller should (re)unload Ollama's model: on entering a memory level, then
        every UNLOAD_EVERY_S while memory stays short (a request already queued in Ollama when we
        unloaded would load it again). Called AFTER the new level is written, so nothing new starts
        while the unload runs. Heat and power never unload: an idle model draws no GPU power."""
        if not (self.cfg["unload"] and self.sig["memory"] >= 1):
            self.last_unload = None
            return False
        if self.last_unload is not None and self.now - self.last_unload < self.UNLOAD_EVERY_S:
            return False
        self.last_unload = self.now
        return True

    def step(self, r, now):
        c = self.cfg
        self.now = now
        rr = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()}
        self.sig["memory"] = signal_level(self.sig["memory"], r.get("mem_mb"), c["mem"],
                                          c["mem_clear"], True)
        self.sig["heat"] = signal_level(self.sig["heat"], r.get("tj_c"), c["tj"], c["tj_clear"],
                                        False)
        oc = r.get("oc_events")
        rising = oc is not None and self.last_oc is not None and oc > self.last_oc
        if oc is not None:
            self.last_oc = oc
        self.sig["power"] = c["power"] if rising else 0

        asked = max(self.sig.values())
        for k in range(1, asked + 1):
            self.last_ask[k] = now
        level = asked        # never below what is asked now; the hold only keeps a higher one
        for k in (3, 2, 1):
            t = self.last_ask[k]
            if k > level and t is not None and now - t < c["hold_s"]:
                level = k
                break
        if asked:
            # the main cause: the signal asking for the most (memory, then heat, then power on a tie)
            self.cause = max(("memory", "heat", "power"), key=lambda s: (self.sig[s],
                             {"memory": 2, "heat": 1, "power": 0}[s]))
        if level != self.level:
            self.since = now
            what = ", ".join(PAUSED_AT.get(level, [])) or "nothing"
            _log(f"level {self.level} -> {level} ({self.cause if level else 'recovered'}); "
                 f"paused: {what}; readings {json.dumps(rr)}")
            self.level = level
        if level == 0:
            self.cause = None
        return {"level": level, "cause": self.cause, "since": self.since, "updated": now,
                "paused": PAUSED_AT.get(level, []), "signals": dict(self.sig),
                "readings": rr}


def unload_models(base):
    """Ask Ollama to drop every loaded model now (keep_alive 0). Errors only log."""
    try:
        with urllib.request.urlopen(base + "/api/ps", timeout=5) as resp:
            names = [m.get("name") or m.get("model") for m in json.loads(resp.read()).get("models", [])]
    except (OSError, ValueError) as exc:
        _log(f"unload: could not list Ollama models: {exc}")
        return
    # nothing loaded (the usual case on a repeat check): no log, no request
    for name in filter(None, names):
        try:
            req = urllib.request.Request(base + "/api/generate", data=json.dumps(
                {"model": name, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            _log(f"unloaded {name} to free memory")
        except OSError as exc:
            _log(f"unload {name} failed: {exc}")


def write_state(st, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, path)


def readings(meminfo="/proc/meminfo", thermal_root="/sys/class/thermal",
             hwmon_root="/sys/class/hwmon"):
    return {"mem_mb": read_mem_available_mb(meminfo), "tj_c": read_tj_c(thermal_root),
            "oc_events": read_oc_events(hwmon_root)}


def main():
    cfg = load_config()
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    g = Guard(cfg)
    unloader = None
    _log(f"watching: memory {cfg['mem']} MB (clear {cfg['mem_clear']}), heat {cfg['tj']} C "
         f"(clear {cfg['tj_clear']}), power level {cfg['power']}, hold {cfg['hold_s']:.0f} s; "
         f"state {path}")
    while True:
        try:
            write_state(g.step(readings(), time.time()), path)
            # only after the pause is published, and off the loop so the state keeps refreshing
            if g.unload_due() and not (unloader and unloader.is_alive()):
                unloader = threading.Thread(target=unload_models, args=(cfg["ollama"],), daemon=True)
                unloader.start()
        except Exception as exc:  # noqa: BLE001 — keep watching; a stale file fails open anyway
            _log(f"step failed: {type(exc).__name__}: {exc}")
        time.sleep(cfg["interval_s"])


if __name__ == "__main__":
    sys.exit(main())
