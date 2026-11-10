"""Orin load shed (#248): the guard's readings, levels, hysteresis and the readers' fail-open rule.

Pins:
  * readings come from /proc/meminfo, the tj-thermal zone (zones that can't be read are skipped)
    and the soctherm_oc counters — all simulated here in a temp dir;
  * order: low memory sheds narration first (1), then the plume (2), then re-route (3);
  * value hysteresis: a level is left only past its clear threshold, not its enter one;
  * time hysteresis: the overall level drops only after the hold with nobody asking for it;
  * heat sheds the same way; rising over-current counters ask for level 1 (a steady count doesn't);
  * Ollama is unloaded once on entering a memory level, never for heat or power;
  * readers: missing, stale or broken state = level 0 (a dead guard never blocks the boat); the
    sentence is plain ("Re-route paused: Orin low on memory.");
  * a bad shed.env falls back to the defaults instead of stopping the guard.

    cd pi/orin && python3 -m shed.test_guard
"""
import json
import os
import sys
import tempfile

from shed import guard

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def fake_sys(root, mem_kb=6_800_000, tj_milli=45093, oc=(0, 0, 14368)):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "meminfo"), "w") as f:
        f.write(f"MemTotal:        7665156 kB\nMemFree:          100000 kB\n"
                f"MemAvailable:    {mem_kb} kB\nBuffers: 0 kB\n")
    th = os.path.join(root, "thermal")
    for i, (kind, temp) in enumerate([("cpu-thermal", "44875"), ("cv0-thermal", None),
                                      ("tj-thermal", str(tj_milli))]):
        z = os.path.join(th, f"thermal_zone{i}")
        os.makedirs(z, exist_ok=True)
        with open(os.path.join(z, "type"), "w") as f:
            f.write(kind + "\n")
        tp = os.path.join(z, "temp")
        if temp is None:
            os.makedirs(tp, exist_ok=True)      # unreadable as a file, like "No data available"
        else:
            with open(tp, "w") as f:
                f.write(temp + "\n")
    hw = os.path.join(root, "hwmon")
    for name, files in (("hwmon2", {"name": "ina3221", "curr1_input": "952"}),
                        ("hwmon3", {"name": "soctherm_oc", "oc1_event_cnt": str(oc[0]),
                                    "oc2_event_cnt": str(oc[1]), "oc3_event_cnt": str(oc[2])})):
        d = os.path.join(hw, name)
        os.makedirs(d, exist_ok=True)
        for k, v in files.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")
    return lambda: guard.readings(os.path.join(root, "meminfo"), th, hw)


def mb(x):
    return {"mem_mb": float(x), "tj_c": 45.0, "oc_events": 14368}


def main():
    tmp = tempfile.mkdtemp()
    print("readings (simulated /proc and /sys)")
    read = fake_sys(os.path.join(tmp, "sys"))
    r = read()
    check("MemAvailable read in MB", abs(r["mem_mb"] - 6_800_000 / 1024) < 0.01)
    check("tj from the tj-thermal zone; an unreadable zone is skipped", r["tj_c"] == 45.093)
    check("over-current counters summed from soctherm_oc only", r["oc_events"] == 14368)
    check("no /proc file → None, not a crash",
          guard.read_mem_available_mb(os.path.join(tmp, "nope")) is None)

    cfg = guard.load_config({})
    g = guard.Guard(cfg)
    t = 1000.0

    print("memory: narration, then plume, then re-route")
    st = g.step(mb(6000), t)
    check("plenty free → level 0, nothing paused", st["level"] == 0 and st["paused"] == [])
    st = g.step(mb(1000), t + 5)
    check("1000 MB (worst seen with only the copilot) → still 0", st["level"] == 0)
    st = g.step(mb(700), t + 10)
    check("700 MB → level 1: narration paused", st["level"] == 1 and st["paused"] == ["narration"])
    check("entering a memory level asks for an unload", g.unload_due())
    check("…once: not again on the same step", not g.unload_due())
    st = g.step(mb(580), t + 15)
    check("580 MB → level 2: narration + plume", st["level"] == 2
          and st["paused"] == ["narration", "plume"])
    st = g.step(mb(340), t + 20)
    check("340 MB (worst seen, #253) → level 3: re-route too",
          st["level"] == 3 and st["paused"] == ["narration", "plume", "reroute"])
    check("cause is memory", st["cause"] == "memory")
    check("no repeat unload within 30 s", not g.unload_due())
    st = g.step(mb(340), t + 45)
    check("memory still short 35 s later → unload again (a queued request may have reloaded it)",
          g.unload_due())

    print("value hysteresis")
    st = g.step(mb(700), t + 55)
    check("back above the level-3 enter (500) but under its clear (750) → signal stays 3",
          g.sig["memory"] == 3)
    st = g.step(mb(800), t + 60)
    check("above 750 → memory signal drops to 2 (under its 850 clear)", g.sig["memory"] == 2)

    print("time hysteresis (hold 300 s)")
    st = g.step(mb(6000), t + 65)
    check("memory back, signal 0, but the level holds at 3", g.sig["memory"] == 0 and st["level"] == 3)
    st = g.step(mb(6000), t + 55 + 299)
    check("299 s after level 3 was last asked for → still 3", st["level"] == 3)
    st = g.step(mb(6000), t + 55 + 301)
    check("301 s → level 3 lifted; level 2 was asked for until t+60 → 2", st["level"] == 2)
    st = g.step(mb(6000), t + 60 + 301)
    check("then back to 0", st["level"] == 0 and st["cause"] is None)

    print("heat")
    g2 = guard.Guard(cfg)
    h = lambda c: {"mem_mb": 6000.0, "tj_c": float(c), "oc_events": 0}  # noqa: E731
    check("70 C (measured peak under full load) → 0", g2.step(h(70), t)["level"] == 0)
    check("85 C → level 1", g2.step(h(85), t + 5)["level"] == 1)
    check("95 C → level 3, cause heat", g2.step(h(95), t + 10)["cause"] == "heat"
          and g2.level == 3)
    g2.step(h(91), t + 15)
    check("91 C: under the 95 enter, over the 90 clear → heat signal stays 3", g2.sig["heat"] == 3)
    check("heat never unloads the model", not g2.unload_due())

    print("power")
    g3 = guard.Guard(cfg)
    p = lambda n: {"mem_mb": 6000.0, "tj_c": 45.0, "oc_events": n}  # noqa: E731
    check("first reading of an old count (14368) → 0", g3.step(p(14368), t)["level"] == 0)
    check("count steady → 0", g3.step(p(14368), t + 5)["level"] == 0)
    st = g3.step(p(14370), t + 10)
    check("count rising → level 1, cause power", st["level"] == 1 and st["cause"] == "power")
    check("held after the count stops rising", g3.step(p(14370), t + 15)["level"] == 1)
    check("power never unloads the model", not g3.unload_due())
    check("SHED_UNLOAD_MODEL=0 never unloads",
          not (lambda gg: (gg.step(mb(300), t), gg.unload_due())[1])(
              guard.Guard(guard.load_config({"SHED_UNLOAD_MODEL": "0"}))))
    g0 = guard.Guard(guard.load_config({"SHED_HOLD_S": "0"}))
    check("SHED_HOLD_S=0 still sheds what is asked now (no hold)",
          g0.step(mb(340), t)["level"] == 3 and g0.step(mb(6000), t + 5)["level"] == 0)
    check("SHED_POWER_LEVEL=0 ignores power",
          guard.Guard(guard.load_config({"SHED_POWER_LEVEL": "0"})).step(p(5), t)["level"] == 0)

    print("readers (copilot, plume, re-route)")
    path = os.path.join(tmp, "state.json")
    check("no state file → level 0, guard off",
          guard.read_state(path) == {"level": 0, "guard": "off"})
    check("no state file → nothing paused", guard.paused("reroute", path) is None)
    guard.write_state({"level": 3, "cause": "memory", "updated": 5000.0}, path)
    check("level 3 → re-route paused, plain words",
          guard.paused("reroute", path, now=5001) == "Re-route paused: Orin low on memory.")
    check("level 3 → plume and narration paused too",
          guard.paused("plume", path, now=5001) and guard.paused("narration", path, now=5001))
    check("stale (guard dead) → runs as normal", guard.paused("reroute", path, now=5100) is None
          and guard.read_state(path, now=5100)["guard"] == "stale")
    guard.write_state({"level": 1, "cause": "heat", "updated": 5000.0}, path)
    check("level 1 → only narration paused",
          guard.paused("narration", path, now=5001) == "Narration paused: Orin running hot."
          and guard.paused("plume", path, now=5001) is None
          and guard.paused("reroute", path, now=5001) is None)
    with open(path, "w") as f:
        f.write("{not json")
    check("broken file → level 0", guard.read_state(path)["level"] == 0)
    os.environ["SHED_STALE_S"] = "oops"
    check("a junk SHED_STALE_S in a reader → default 30 s, no crash",
          guard.read_state(path, now=5001)["level"] == 0)
    os.environ.pop("SHED_STALE_S")
    os.environ["SHED_STATE"] = path
    guard.write_state({"level": 2, "cause": "memory", "updated": 5000.0}, path)
    check("SHED_STATE env picks the file", guard.paused("plume", now=5001) is not None)

    print("config")
    bad = guard.load_config({"SHED_MEM_MB": "500,600,750"})
    check("thresholds in the wrong order → defaults", bad["mem"] == [750.0, 600.0, 500.0])
    check("junk → defaults", guard.load_config({"SHED_TJ_C": "hot"})["tj"] == [85.0, 90.0, 95.0])
    check("a bad interval (negative, nan, inf, 0, over 10 s) → defaults",
          all(guard.load_config({"SHED_INTERVAL_S": v})["interval_s"] == 5.0
              for v in ("-1", "nan", "inf", "0", "60")))
    check("nan thresholds → defaults",
          guard.load_config({"SHED_MEM_CLEAR_MB": "nan,nan,nan"})["mem_clear"] == [950.0, 850.0, 750.0])
    check("a good override is used",
          guard.load_config({"SHED_MEM_MB": "900,700,550",
                             "SHED_MEM_CLEAR_MB": "1100,950,800"})["mem"] == [900.0, 700.0, 550.0])

    print("state written atomically, as JSON")
    p2 = os.path.join(tmp, "s2.json")
    guard.write_state(guard.Guard(cfg).step(read(), 42.0), p2)
    with open(p2) as f:
        s = json.load(f)
    check("level, paused, readings and updated are in the file",
          s["level"] == 0 and s["updated"] == 42.0 and "mem_mb" in s["readings"])

    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
