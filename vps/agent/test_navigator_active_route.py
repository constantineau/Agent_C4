"""GET /course is a read: it must not re-persist the active route (#82 review).

`get_course()` used to call `set_active(route)` even on a plain read, writing back the route it had
just read. The Lab's boat check and the console's own poll read /course; if a course load switched
the boat from route A to B between that read and its write, the read put A back. An explicit
`route=` (the route picker, the copilot's engine client) still selects + persists it.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_navigator_active_route.py
"""
from app import navigator as NAV

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class FakeDS:
    def __init__(self):
        self.saved = []
        self.stored = "A"

    def marks(self, route):
        return [{"seq": 1, "name": route + "1", "lat": 44.0, "lon": -82.0},
                {"seq": 2, "name": route + "2", "lat": 44.1, "lon": -82.0}]

    def save_active_route(self, r):
        self.saved.append(r)
        self.stored = r

    def get_active_route(self):
        return self.stored


ds = FakeDS()
NAV.datasource.active = lambda: ds
NAV._active, NAV._restored = "default", False

c = NAV.get_course()
check("plain read returns the persisted active route", c["route"] == "A")
check("plain read writes nothing", ds.saved == [])

# a course load switches the boat to B while a read is in flight — the read must not undo it
NAV.set_active("B")
NAV.get_course()
check("after A->B, a plain read leaves B active", NAV.active_route() == "B" and ds.stored == "B"
      and ds.saved == ["B"])

c = NAV.get_course("C")
check("explicit route still selects + persists it", c["route"] == "C" and NAV.active_route() == "C"
      and ds.saved[-1] == "C")

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
