"""The proactive AUTO-COACH timer — the copilot volunteers coaching on a cadence.

`make_narration` is PULL: the iPad asks and the narration engine answers. This is the TIMER that
DRIVES it. A background loop on the Orin ticks every `COACH_INTERVAL_S`, runs the narration engine,
and holds the latest result — so the crew gets proactive coaching whether or not anything is polling,
and (the real point) so the TIME-DRIVEN callouts — the staged 15/10/5-min rounding prep, a playbook
branch trigger firing, a sail change-down coming up — fire ON THE CLOCK, not only when a client
happens to hit the endpoint.

It mirrors the cloud alerting loop (`vps/agent/app/alerts.py`): the raise-slow / clear-fast
speak-once dedup already lives in `narrate.step` — this loop just calls it on a schedule and
remembers what was said. The loop OWNS narration for its route (it is the single stepper, so the
speak-once dedup is not raced by client polls); `GET /coach` reads the held state cheaply with NO
recompute. A short rolling history of spoken lines lets the crew see "what did the coach just say".

Best-effort like everything onboard: an engine-unreachable tick is recorded in `last_error` and the
loop keeps ticking. Nothing here takes an action — the coach speaks, it never steers.
"""
import asyncio
import time
from collections import deque

from . import config, copilot

_HISTORY_MAX = 12


class _Coach:
    def __init__(self):
        self.enabled = config.COACH_ENABLED
        self.interval = config.COACH_INTERVAL_S
        self.route = config.DEFAULT_ROUTE
        self.last = None                              # latest make_narration result (+ _coach_at)
        self.history = deque(maxlen=_HISTORY_MAX)     # spoken lines, newest first
        self.ticks = 0
        self.last_tick_at = None
        self.last_error = None
        self._task = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict:
        """The held coach state — what the iPad polls. No recompute; just the last tick's result."""
        last = self.last or {}
        return {
            "enabled": self.enabled,
            "running": self.running(),
            "interval_s": self.interval,
            "route": self.route,
            "ticks": self.ticks,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "active": last.get("active", []),         # full confirmed set (the banner)
            "new": last.get("new", []),               # what was newly voiced on the last tick
            "spoken": last.get("spoken", ""),         # the phrased line for the new callouts
            "narration_mode": last.get("narration_mode", "none"),
            "updated_at": last.get("_coach_at"),
            "history": list(self.history),
            "playbook_loaded": last.get("_meta", {}).get("playbook_loaded"),
        }

    async def tick(self):
        """One coaching cycle: run the narration engine (off the event loop — it's blocking urllib),
        store the result, and log any newly-voiced line. Never raises — records the error and moves on."""
        try:
            res = await asyncio.to_thread(copilot.make_narration, self.route, None, None)
            res["_coach_at"] = time.time()
            self.last = res
            self.last_error = None
            if res.get("new"):                        # something newly worth saying → log it
                self.history.appendleft({
                    "at": res["_coach_at"],
                    "spoken": res.get("spoken", ""),
                    "narration_mode": res.get("narration_mode"),
                    "callouts": [c.get("headline") for c in res["new"]],
                })
        except Exception as e:                        # best-effort — a flaky link must not kill the loop
            self.last_error = f"{type(e).__name__}: {e}"
        self.ticks += 1
        self.last_tick_at = time.time()

    async def run(self):
        # Fresh start → clear the speak-once dedup so the current situation is voiced once.
        copilot.reset_narration(self.route)
        while True:
            await self.tick()
            await asyncio.sleep(self.interval)


COACH = _Coach()


async def start():
    """Launch the auto-coach loop (idempotent). No-op when disabled (COPILOT_COACH=false)."""
    if COACH.enabled and not COACH.running():
        COACH._task = asyncio.create_task(COACH.run())


async def stop():
    """Cancel the loop on shutdown."""
    if COACH._task is not None:
        COACH._task.cancel()
        try:
            await COACH._task
        except asyncio.CancelledError:
            pass
        COACH._task = None
