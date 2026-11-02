"""The proactive AUTO-COACH timer — the copilot volunteers coaching on a cadence.

`make_narration` is PULL: the iPad asks and the narration engine answers. This is the TIMER that
DRIVES it. A background loop on the Orin ticks every `COACH_INTERVAL_S`, runs the narration engine,
and holds the latest result — so the crew gets proactive coaching whether or not anything is polling,
and (the real point) so the TIME-DRIVEN callouts — the staged 15/10/5-min rounding prep, a playbook
branch trigger firing, a sail change-down coming up — fire ON THE CLOCK, not only when a client
happens to hit the endpoint.

It mirrors the cloud alerting loop (`vps/agent/app/alerts.py`): the raise-slow / clear-fast
show-once dedup already lives in `narrate.step` — this loop just calls it on a schedule and
remembers what was shown. The loop OWNS narration for its route (it is the single stepper, so the
show-once dedup is not raced by client polls); `GET /coach` reads the held state cheaply with NO
recompute. A short rolling history of coach lines lets the crew see "what did the coach just show".

The deterministic result is published the moment it's computed; LLM phrasing swaps a nicer line in
later, from its own task. Best-effort like everything onboard: an engine-unreachable tick is
recorded in `last_error` (and raises the "Collision watch OFF" callout) and the loop keeps ticking;
a tick that throws publishes a "Collision watch OFF: copilot error" result rather than keeping the
last one. Nothing here takes an action — the coach line shows, it never steers.
"""
import asyncio
import time
import uuid
from collections import deque

from . import briefs as briefs_mod
from . import config, copilot
from . import narrate as narrate_mod
from .engine_client import EngineClient

_HISTORY_MAX = 12
_BRIEF_HISTORY_MAX = 10


def _busy(task) -> bool:
    return task is not None and not task.done()


class _Coach:
    def __init__(self):
        self.enabled = config.COACH_ENABLED
        self.interval = config.COACH_INTERVAL_S
        self.route = config.DEFAULT_ROUTE
        self.last = None                              # latest step_narration result (+ _coach_at)
        # progress for the iPad, free of wall clock (an NTP jump back at boot must not read as a
        # stall): `seq` goes up by one per published result; `instance` is new per process, so a
        # restart (seq back to 0) still reads as progress
        self.seq = 0
        self.instance = uuid.uuid4().hex
        self.history = deque(maxlen=_HISTORY_MAX)     # coach lines, newest first
        self.ticks = 0
        self.last_tick_at = None
        self.last_error = None
        self._task = None
        self._phrase_task = None                      # the in-flight LLM phrasing (one at a time)
        self._brief_task = None                       # the in-flight brief scheduler pass (one at a time)
        # scheduled CREW BRIEFS state (briefs.py): the held latest per kind + a short history,
        # and the show-once keys so each rotation/hour/mark gets its brief exactly once
        self.briefs = {}                              # kind -> latest full payload
        self.brief_history = deque(maxlen=_BRIEF_HISTORY_MAX)   # {kind, at, headline}, newest first
        self._handover_key = None                     # the rotation (next_change epoch) already briefed
        self._recap_at = 0.0                          # last recap wall-clock
        self._mark_key = None                         # the mark (name:seq) already briefed

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
            "new": last.get("new", []),               # what was newly shown on the last tick
            "spoken": last.get("spoken", ""),         # the phrased line for the new callouts
            "narration_mode": last.get("narration_mode", "none"),
            "updated_at": last.get("_coach_at"),     # display only (the Orin's wall clock)
            "seq": self.seq,                          # progress: +1 per published result
            "instance": self.instance,                # …per process (a restart is progress too)
            "history": list(self.history),
            "playbook_loaded": last.get("_meta", {}).get("playbook_loaded"),
            # the coach-window surface: latest full brief per kind + what fired recently
            "briefs": self.briefs,
            "brief_history": list(self.brief_history),
        }

    def _hold_brief(self, kind, payload):
        self.briefs[kind] = payload
        self.brief_history.appendleft({"kind": kind, "at": payload.get("generated_at"),
                                       "headline": payload.get("headline"),
                                       "mode": payload.get("mode")})

    async def _make_brief(self, kind):
        payload = await asyncio.to_thread(briefs_mod.make, kind, self.route)
        if payload.get("available"):
            self._hold_brief(kind, payload)

    async def check_briefs(self):
        """The BRIEF SCHEDULER — one pass of the show-once rules. Two cheap engine reads decide
        whether anything is due; the (LLM-phrased) brief itself is generated at most once per
        rotation / hour / mark. Never raises."""
        eng = await asyncio.to_thread(EngineClient)
        w = await asyncio.to_thread(eng.watch)
        nav = await asyncio.to_thread(eng.navigator, self.route)
        now = time.time()
        racing = bool(w.get("plan_set") and w.get("active"))

        # handover at T-15 to the next rotation, once per rotation
        mins = w.get("mins_to_change")
        if (racing and w.get("next_on") and isinstance(mins, (int, float))
                and 0 < mins <= config.BRIEF_HANDOVER_T_MIN):
            key = w.get("next_change")
            if key and key != self._handover_key:
                self._handover_key = key
                await self._make_brief("handover")

        # hourly recap — only while racing (an active watch plan is the signal); dock = silence
        if racing and now - self._recap_at >= config.BRIEF_RECAP_S:
            self._recap_at = now
            await self._make_brief("recap")

        # mark pre-brief inside the window, once per mark; the rounding endgame is narrate's
        nm = (nav.get("next_mark") or {}) if nav.get("available") else {}
        eta = nm.get("eta_min")
        if (nm.get("name") and isinstance(eta, (int, float))
                and config.BRIEF_MARK_MIN_T_MIN < eta <= config.BRIEF_MARK_T_MIN):
            key = f"{nm.get('name')}:{nm.get('seq')}"
            if key != self._mark_key:
                self._mark_key = key
                await self._make_brief("mark")

    async def tick(self):
        """One coaching cycle: run the deterministic callout engine (off the event loop — it's
        blocking urllib) and PUBLISH the result straight away, with the callouts' own text as the
        line. LLM phrasing and the scheduled briefs run as separate background tasks, so a cold or
        busy model can never hold a collision warning back, or delay the next tick's check. Never
        raises — records the error and moves on."""
        try:
            res = await asyncio.to_thread(copilot.step_narration, self.route, None)
            res["_coach_at"] = time.time()
            self.last = res
            self.seq += 1
            # an engine that can't be reached is not a quiet tick: the blind callout is up, say so here too
            self.last_error = ("engine unreachable — collision watch OFF"
                               if res.get("_meta", {}).get("engine_lost") else None)
            if res.get("new"):                        # something newly worth showing → log it
                entry = {
                    "at": res["_coach_at"],
                    "spoken": res.get("spoken", ""),
                    "narration_mode": res.get("narration_mode"),
                    "callouts": [c.get("headline") for c in res["new"]],
                    # so the iPad can tell a voiced safety line that has since cleared (#191)
                    "ids": [c.get("id") for c in res["new"]],
                    "categories": [c.get("category") for c in res["new"]],
                }
                self.history.appendleft(entry)
                if config.USE_LLM and not _busy(self._phrase_task):
                    self._phrase_task = asyncio.create_task(self._phrase(res, entry))
        except Exception as e:                        # best-effort — a flaky link must not kill the loop
            self.last_error = f"{type(e).__name__}: {e}"
            self._publish_blind()
        if config.BRIEFS_ENABLED and not _busy(self._brief_task):
            self._brief_task = asyncio.create_task(self._briefs())
        self.ticks += 1
        self.last_tick_at = time.time()

    def _publish_blind(self):
        """A tick that threw computed nothing, so the held result must not stand in for it (old
        callouts, or none, would read as a working watch). Publish a blind result instead: the
        "Collision watch OFF" callout, new on the first failing tick, and a fresh `updated_at` so
        the iPad sees the copilot is alive but the watch is not."""
        c = narrate_mod.blind_now("copilot error")
        was = any(a.get("id") == c["id"] for a in (self.last or {}).get("active", []))
        line = narrate_mod._deterministic_spoken([c])
        self.seq += 1
        self.last = {"active": [c], "new": [] if was else [c], "spoken": line,
                     "narration_mode": "deterministic", "_coach_at": time.time(),
                     "_meta": {"tick_failed": True}}
        if not was:
            self.history.appendleft({"at": self.last["_coach_at"], "spoken": line,
                                     "narration_mode": "deterministic", "callouts": [c["headline"]],
                                     "ids": [c["id"]], "categories": [c["category"]]})
        # the dedup state no longer matches what is shown: start clean when ticks recover
        copilot.reset_narration(self.route)

    async def _phrase(self, res, entry):
        """Swap the published deterministic line for the LLM's phrasing once it lands — if it does.
        Safety callouts never reach the model (`narrate.narrate` keeps their own text)."""
        try:
            out = await asyncio.to_thread(copilot.phrase_narration,
                                          {**res, "_meta": dict(res.get("_meta") or {})}, None)
        except Exception:                             # phrasing is a nicety; the line is already up
            return
        if out.get("narration_mode") != "llm":
            return
        entry["spoken"], entry["narration_mode"] = out["spoken"], "llm"
        if self.last is res:                          # still the latest tick → update the held line too
            res["spoken"], res["narration_mode"] = out["spoken"], "llm"

    async def _briefs(self):
        try:
            await self.check_briefs()
        except Exception as e:                        # a failed brief never kills the coach
            self.last_error = f"briefs: {type(e).__name__}: {e}"

    async def run(self):
        # Fresh start → clear the show-once dedup so the current situation is shown once.
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
    for name in ("_task", "_phrase_task", "_brief_task"):
        task = getattr(COACH, name)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(COACH, name, None)
