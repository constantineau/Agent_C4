"""Race checklist — unit test. Stubs the datasource + the navigator reads so it runs standalone;
locks the trigger taxonomy (sunset window + re-arm, location latch, finishing), per-window acks,
manual items and the payload shape.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_checklist.py
"""
import calendar

from app import checklist, navigator

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


ITEMS = [
    {"id": "nav-lights", "category": "safety", "phase": "in_race", "deliver_to_ipad": True,
     "text": "Display navigation lights between sunset and sunrise.",
     "trigger_type": "time", "trigger_detail": "sunset->sunrise", "critical": True},
    {"id": "gate-photo", "category": "compliance", "phase": "in_race", "deliver_to_ipad": True,
     "text": "Photograph the GPS at the Cove Island Gate.",
     "trigger_type": "location", "trigger_detail": "Cove Island gate"},
    {"id": "finish-procedure", "category": "compliance", "phase": "in_race",
     "deliver_to_ipad": True, "text": "Cross the finish line East to West; display numbers.",
     "trigger_type": "event", "trigger_detail": "finishing", "critical": True},
    {"id": "sponsor-flag", "category": "admin", "phase": "in_race", "deliver_to_ipad": True,
     "text": "Display the supplied sponsor backstay flag if required.",
     "trigger_type": "event", "trigger_detail": "if supplied / per SI"},
    {"id": "not-for-ipad", "text": "Lab-only prep item", "deliver_to_ipad": False},
]

MARKS = [{"seq": 1, "name": "Start", "lat": 43.0, "lon": -82.42},
         {"seq": 2, "name": "Cove Island Virtual Gate", "lat": 45.3333, "lon": -81.85},
         {"seq": 3, "name": "Finish", "lat": 45.85, "lon": -84.62}]


class StubDS:
    blob = {}

    def save_checklist(self, blob):
        StubDS.blob = dict(blob)

    def get_checklist(self):
        return dict(StubDS.blob)


POS = {"lat": 44.0, "lon": -82.3}
NAV = {"available": True, "marks_total": 3,
       "next_mark": {"name": "Cove Island Virtual Gate", "seq": 2, "index": 1,
                     "distance_nm": 84.0}}

checklist.datasource.active = lambda: StubDS()
navigator._latest = lambda: {"lat": POS["lat"], "lon": POS["lon"], "twd": None, "tws": None,
                             "sog": None, "cog": None, "heading": None}
navigator._marks = lambda route: list(MARKS)
navigator.get_navigator = lambda route=None: dict(NAV)

# 2026-07-18 18:00 UTC = race-day afternoon on Lake Huron (sunset ~01:10 UTC on the 19th)
T_AFTERNOON = calendar.timegm((2026, 7, 18, 18, 0, 0))

# --- solar sanity --------------------------------------------------------------------------------
print("solar:")
ss, sr = checklist._night_window(44.0, -82.3, T_AFTERNOON)
check("July Lake Huron sunset lands 00:00–02:30 UTC (evening local)",
      ss is not None and 6 * 3600 <= ss - T_AFTERNOON <= 8.5 * 3600)
check("night is 7–11 h long", 7 * 3600 <= sr - ss <= 11 * 3600)
mid_ss, mid_sr = checklist._night_window(44.0, -82.3, ss + 3 * 3600)
check("mid-night resolves the SAME window", abs(mid_ss - ss) < 60 and abs(mid_sr - sr) < 60)

# --- load ----------------------------------------------------------------------------------------
print("load:")
r = checklist.load({"definition": {"race_id": "bvm26", "requirements": ITEMS}})
check("definition load filters deliver_to_ipad", r["loaded"] and r["items"] == 4)
check("empty load refused", not checklist.load({"items": []})["loaded"])
st = checklist.get_checklist(now=T_AFTERNOON)
check("plan_set + 4 items", st["plan_set"] and len(st["items"]) == 4)
by = {i["id"]: i for i in st["items"]}

# --- afternoon: nothing due ----------------------------------------------------------------------
print("afternoon (84 nm from the gate, pre-sunset):")
check("nav-lights pending w/ countdown", by["nav-lights"]["status"] == "pending"
      and "sunset in" in (by["nav-lights"]["measure"] or ""))
check("gate-photo pending w/ distance", by["gate-photo"]["status"] == "pending"
      and "Cove Island Virtual Gate" in by["gate-photo"]["measure"])
check("finish pending (not final leg)", by["finish-procedure"]["status"] == "pending")
check("sponsor flag is manual", by["sponsor-flag"]["status"] == "manual")
check("nothing active", st["counts"].get("active") is None)

# --- sunset window -------------------------------------------------------------------------------
print("sunset:")
t_lead = ss - 10 * 60                       # 10 min before sunset — inside the 30-min lead
by = {i["id"]: i for i in checklist.get_checklist(now=t_lead)["items"]}
check("nav-lights active inside the lead", by["nav-lights"]["status"] == "active")
t_night = ss + 4 * 3600
by = {i["id"]: i for i in checklist.get_checklist(now=t_night)["items"]}
check("still active mid-night", by["nav-lights"]["status"] == "active"
      and "sunrise" in by["nav-lights"]["measure"])
checklist.ack({"id": "nav-lights"}, now=t_night)
by = {i["id"]: i for i in checklist.get_checklist(now=t_night)["items"]}
check("acked → done for this night", by["nav-lights"]["status"] == "done")
# #43: the boat MOVES all night, so each poll computes a slightly different sunset. The ack must
# hold (it was keyed on int(sunset) and re-armed after a few nm of westing)
POS.update(lon=-82.6)                       # ~13 nm west: sunset ~70 s later
ss2, _ = checklist._night_window(POS["lat"], POS["lon"], t_night)
check("moving west shifts the computed sunset", int(ss2) != int(ss))
saves = []
_orig_save = StubDS.save_checklist
StubDS.save_checklist = lambda self, blob: (saves.append(1), _orig_save(self, blob))
by = {i["id"]: i for i in checklist.get_checklist(now=t_night + 600)["items"]}
check("ack holds after the boat moves (same night)", by["nav-lights"]["status"] == "done")
check("no blob rewrite on a plain poll", not saves)
StubDS.save_checklist = _orig_save
POS.update(lon=-82.3)
t_tomorrow = sr + 5 * 3600                  # next morning, well past sunrise
by = {i["id"]: i for i in checklist.get_checklist(now=t_tomorrow)["items"]}
check("next day: ack expired, pending again", by["nav-lights"]["status"] == "pending")

# --- location latch ------------------------------------------------------------------------------
print("location (Cove Island gate):")
POS.update(lat=45.25, lon=-81.90)           # ~5.5 nm from the gate
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("inside 8 nm → active", by["gate-photo"]["status"] == "active")
POS.update(lat=45.55, lon=-82.4)            # past the gate, ~25 nm away
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("past the gate unacked → STILL active (latched)", by["gate-photo"]["status"] == "active")
checklist.ack({"id": "gate-photo"}, now=T_AFTERNOON)
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("acked → done, stays done", by["gate-photo"]["status"] == "done")

# --- finishing -----------------------------------------------------------------------------------
print("finishing:")
NAV["next_mark"] = {"name": "Finish", "seq": 3, "index": 2, "distance_nm": 42.0}
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("final leg but 42 nm out → pending", by["finish-procedure"]["status"] == "pending"
      and "finish in 42" in by["finish-procedure"]["measure"])
NAV["next_mark"]["distance_nm"] = 7.2
st = checklist.get_checklist(now=T_AFTERNOON)
by = {i["id"]: i for i in st["items"]}
check("inside 10 nm of the finish → active", by["finish-procedure"]["status"] == "active"
      and "finish in 7.2 nm" in by["finish-procedure"]["measure"])
check("active list carries it", any(i["id"] == "finish-procedure" for i in st["active"]))

# --- manual + shape ------------------------------------------------------------------------------
print("manual + shape:")
checklist.ack({"id": "sponsor-flag"}, now=T_AFTERNOON)
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("manual item ackable → done", by["sponsor-flag"]["status"] == "done")
check("undo restores", checklist.ack({"id": "sponsor-flag", "undo": True}) and
      {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
      ["sponsor-flag"]["status"] == "manual")
check("unknown ack refused", checklist.ack({"id": "nope"}).get("available") is False)
st = checklist.get_checklist(now=T_AFTERNOON)
check("counts add up", sum(st["counts"].values()) == 4)
StubDS.blob = {}
st = checklist.get_checklist(now=T_AFTERNOON)
check("nothing loaded → empty-but-valid", st["available"] and not st["plan_set"])

# --- #183-6: a poll in flight must not write away a crew ack -----------------------------------
print("\nack vs poll race:")
checklist.load({"items": ITEMS})
POS.update({"lat": 45.30, "lon": -81.85})             # a couple of nm from the gate: it arms now
_real_nav = navigator.get_navigator
_fired = {"n": 0}


def _nav_with_ack(route=None):
    # the crew taps an ack while this poll is between its blob read and its latch save
    if _fired["n"] == 0:
        _fired["n"] += 1
        checklist.ack({"id": "sponsor-flag"}, now=T_AFTERNOON)
    return _real_nav(route)


navigator.get_navigator = _nav_with_ack
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
navigator.get_navigator = _real_nav
check("the poll armed the gate item", by["gate-photo"]["status"] == "active")
by = {i["id"]: i for i in checklist.get_checklist(now=T_AFTERNOON)["items"]}
check("the ack made mid-poll survives the poll's save", by["sponsor-flag"]["status"] == "done")
check("and the poll's new latch is kept", "gate-photo" in (StubDS.blob.get("armed") or {}))
POS.update({"lat": 44.0, "lon": -82.3})
# a poll that started before a reload must not save its latches into the new list
checklist.load({"items": ITEMS})
_old_loaded = StubDS.blob["loaded_at"]
POS.update({"lat": 45.30, "lon": -81.85})


def _nav_with_reload(route=None):
    if _fired["n"] == 1:
        _fired["n"] += 1
        StubDS.blob = {**StubDS.blob, "loaded_at": _old_loaded + 1, "armed": {}, "acks": {}}
    return _real_nav(route)


navigator.get_navigator = _nav_with_reload
checklist.get_checklist(now=T_AFTERNOON)
navigator.get_navigator = _real_nav
check("a reload mid-poll keeps the new list clean", StubDS.blob.get("armed") == {})
POS.update({"lat": 44.0, "lon": -82.3})

# an ack that started before a reload must not land on the new list
checklist.load({"items": ITEMS})


def _nav_reload_during_ack(route=None):
    if _fired["n"] == 2:
        _fired["n"] += 1
        StubDS.blob = {**StubDS.blob, "loaded_at": StubDS.blob["loaded_at"] + 1}
    return _real_nav(route)


navigator.get_navigator = _nav_reload_during_ack
res = checklist.ack({"id": "sponsor-flag"}, now=T_AFTERNOON)
navigator.get_navigator = _real_nav
check("an ack across a reload is refused", res.get("available") is False
      and "sponsor-flag" not in (StubDS.blob.get("acks") or {}))

print("\nPASS" if ok else "\nFAIL")
raise SystemExit(0 if ok else 1)
