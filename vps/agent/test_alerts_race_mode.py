"""Cloud alerts respect race mode (RRS 41; vps/agent/app/alerts.py).

Pins:
  * every alert kind the rules can make is classed safety or advice; nothing unclassed;
  * practice: all six rules fire (collision, depth, stale data, slow vs polar, wind shift, fatigue);
  * race mode: only safety fires; "Favour the left side", "Rotate the driver now" and the polar
    deficit are never made;
  * race mode: advice still on record is not served by /alerts, get_alerts or the /ws opening
    push (all read active_alerts), and when it clears the push carries no advice text;
  * an unknown kind counts as advice (fail closed).

No DB: the reads are stubbed. Run in-container (make ci):
  PYTHONPATH=vps/agent:. python3 vps/agent/test_alerts_race_mode.py
"""
import sys

from app import alerts, race_mode, tools

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# every rule firing at once
tools.get_ais_targets = lambda: {"own_fix": True, "targets": [
    {"mmsi": "1", "name": "Tanker", "closing": True, "cpa_nm": 0.2, "tcpa_min": 4, "bearing": 30}]}
tools.get_strip = lambda: {"available": True, "data_age_seconds": 500, "depth": 3.0,
                           "stw": 4.0, "tws": 12.0, "twa": 45.0}
tools.get_polar_target = lambda tws, twa: {"target_stw": 6.5}
tools.get_tactics = lambda route=None: {"available": True, "favored_side": "left",
                                        "wind": {"persistent": True, "shift_deg": 14, "trend": "right"}}
tools.get_fatigue = lambda: {"available": True, "level": "rotate_now", "index": 88}

ALL = {"ais", "depth_shoaling", "stale_telemetry", "polar_deficit", "wind_shift", "fatigue"}

print("classification:")
check("safety and advice don't overlap", not (alerts.SAFETY_KINDS & alerts.ADVICE_KINDS))
check("every kind the rules make is classed", ALL == alerts.SAFETY_KINDS | alerts.ADVICE_KINDS)
check("safety = collision, depth, stale data",
      alerts.SAFETY_KINDS == {"ais", "depth_shoaling", "stale_telemetry"})
check("unknown kind: advice (withheld while racing)", not alerts.allowed("new_kind", racing=True)
      and alerts.allowed("new_kind", racing=False))

print("practice mode:")
race_mode.racing = lambda: False
kinds = {c["kind"] for c in alerts._conditions()}
check("all six rules fire", kinds == ALL)

print("race mode:")
race_mode.racing = lambda: True
conds = alerts._conditions()
kinds = {c["kind"] for c in conds}
check("only safety fires", kinds == {"ais", "depth_shoaling", "stale_telemetry"})
text = " ".join(c["message"] for c in conds)
check("no tactical text", "Favour" not in text and "Rotate" not in text and "target" not in text)

print("race mode, advice still on record:")
ROWS = {
    "wind_shift": {"id": 1, "key": "wind_shift", "kind": "wind_shift", "severity": "warn",
                   "message": "Wind shifted right 14°. Favour the left side.", "raised_epoch": None},
    "ais:1": {"id": 2, "key": "ais:1", "kind": "ais", "severity": "danger",
              "message": "Tanker closing.", "raised_epoch": None},
}
alerts._active_rows = lambda: dict(ROWS)
served = alerts.get_alerts()
check("/alerts + get_alerts serve safety only", [a["kind"] for a in served["alerts"]] == ["ais"]
      and served["count"] == 1)
race_mode.racing = lambda: False
check("practice: both served", {a["kind"] for a in alerts.active_alerts()} == {"ais", "wind_shift"})
race_mode.racing = lambda: True


class FakeCur:
    def execute(self, *a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn(FakeCur):
    def cursor(self):
        return FakeCur()

    def commit(self):
        pass


class FakePool:
    def connection(self):
        return FakeConn()


alerts.pool = FakePool()
changes = alerts.evaluate()
gone = [c for c in changes if c["event"] == "cleared" and c["alert"]["key"] == "wind_shift"]
check("the advice alert is cleared at once", len(gone) == 1)
check("…and the clear carries no advice text", gone and gone[0]["alert"]["message"] == "")
check("nothing new or updated is advice", all(c["alert"]["kind"] in alerts.SAFETY_KINDS
                                              for c in changes if c["event"] != "cleared"))

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
