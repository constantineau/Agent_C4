"""Uplink store-and-forward queue — draining, poison files, and atomic enqueue.

Regression cover for the 2026-07-19 stall: a zero-byte spool file sat at the head of the
queue and `_flush_queue` returned on *any* exception, so every newer batch behind it went
unsent for six weeks (Jul 19 -> Aug 30 telemetry never reached the VPS). The distinction
the queue has to make is transient vs permanent:
  - transient (link down, server 5xx) -> stop, keep everything for the next pass;
  - permanent (file corrupt on disk, server rejects payload 4xx) -> set aside and carry on,
    because it will fail identically forever and blocks the whole queue behind it.
Also locked: `_enqueue` writes via rename, so a power cut cannot create the zero-byte file
that started this in the first place.
"""
import importlib.util
import json
import os
import tempfile
import urllib.error
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_uplink(queue_dir):
    """Import uplink.py fresh with QUEUE_DIR pointed at a temp dir."""
    os.environ["QUEUE_DIR"] = str(queue_dir)
    spec = importlib.util.spec_from_file_location(
        "uplink_under_test", os.path.join(HERE, "uplink.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def q(tmp_path):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    return qdir


def _batch(ts, val):
    return {"boat_id": "sr33",
            "readings": [{"time": ts, "source": "s", "path": "p", "value": val}]}


def _write(qdir, name, obj):
    (qdir / name).write_text("" if obj is None else json.dumps(obj))


def _names(d):
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def test_drains_past_zero_byte_file_at_queue_head(q):
    """The actual boat failure: poison at the head must not strand the good batches."""
    up = _load_uplink(q)
    _write(q, "20260719T072000Z.json", _batch("2026-07-19T07:20:00Z", 1))
    _write(q, "20260719T072833Z.json", None)          # the real poison file
    _write(q, "20260719T073000Z.json", _batch("2026-07-19T07:30:00Z", 2))
    _write(q, "20260720T030010Z.json", None)          # a second one, later
    _write(q, "20260720T031000Z.json", _batch("2026-07-20T03:10:00Z", 3))

    posted = []
    up._post = lambda b: posted.append(b["readings"][0]["value"])
    up._flush_queue()

    assert sorted(posted) == [1, 2, 3], "good batches behind the poison were not sent"
    assert _names(q) == [], "queue did not fully drain"
    assert _names(q / "quarantine") == [
        "20260719T072833Z.json", "20260720T030010Z.json"]


def test_link_down_keeps_everything_queued(q):
    """A transient failure must never discard or quarantine — that would lose telemetry."""
    up = _load_uplink(q)
    _write(q, "a.json", _batch("2026-07-19T07:20:00Z", 1))
    _write(q, "b.json", _batch("2026-07-19T07:30:00Z", 2))

    def down(_):
        raise OSError("link down")

    up._post = down
    up._flush_queue()

    assert _names(q) == ["a.json", "b.json"]
    assert not (q / "quarantine").exists()


@pytest.mark.parametrize("code", [400, 413, 422])
def test_http_4xx_is_permanent_and_quarantined(q, code):
    """A payload the VPS refuses will be refused forever; it must not block the queue."""
    up = _load_uplink(q)
    _write(q, "a.json", _batch("2026-07-19T07:20:00Z", 1))
    _write(q, "b.json", _batch("2026-07-19T07:30:00Z", 2))

    def reject(_):
        raise urllib.error.HTTPError("u", code, "m", {}, None)

    up._post = reject
    up._flush_queue()

    assert _names(q) == []
    assert _names(q / "quarantine") == ["a.json", "b.json"]


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503])
def test_http_retry_later_and_5xx_are_transient_and_retained(q, code):
    """Server-side errors are the VPS's problem, not the batch's — keep the data."""
    up = _load_uplink(q)
    _write(q, "a.json", _batch("2026-07-19T07:20:00Z", 1))

    def boom(_):
        raise urllib.error.HTTPError("u", code, "m", {}, None)

    up._post = boom
    up._flush_queue()

    assert _names(q) == ["a.json"]
    assert not (q / "quarantine").exists()


def test_enqueue_is_atomic(q):
    """Rename-into-place, so an interrupted write leaves no truncated .json behind."""
    up = _load_uplink(q)
    up._enqueue(_batch("2026-09-01T00:00:00Z", 9))

    assert _names(q) == ["20260901T000000Z.json"]
    got = json.loads((q / "20260901T000000Z.json").read_text())
    assert got["readings"][0]["value"] == 9


def test_enqueued_batch_survives_a_round_trip_through_the_queue(q):
    """Enqueue then flush — the batch the VPS receives is the batch that was queued."""
    up = _load_uplink(q)
    original = _batch("2026-09-01T00:00:00Z", 9)
    up._enqueue(original)

    posted = []
    up._post = posted.append
    up._flush_queue()

    assert posted == [original]
    assert _names(q) == []


def test_two_overlapping_replay_passes_post_a_queued_file_once(q):
    """One replay pass at a time: overlapping passes must not both read and post the same
    queued file (it is only unlinked after its POST returns)."""
    import threading
    import time as _t
    up = _load_uplink(q)
    _write(q, "a.json", _batch("2026-07-19T07:20:00Z", 1))
    posted, lock = [], threading.Lock()

    def slow_post(batch):
        _t.sleep(0.2)                   # both passes are inside the queue walk at once
        with lock:
            posted.append(batch["readings"][0]["value"])

    up._post = slow_post
    ts = [threading.Thread(target=up.drain_held) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert posted == [1], posted


def test_a_live_send_never_replays_the_queue(q):
    """The live path posts its own batch only; the replayer owns the backlog (#184-8)."""
    up = _load_uplink(q)
    _write(q, "a.json", _batch("2026-07-19T07:20:00Z", 1))
    posted = []
    up._post = lambda b: posted.append(b["readings"][0]["value"])
    up.send([{"time": "2026-11-09T12:00:00Z", "source": "s", "path": "p", "value": 5}])
    assert posted == [5] and _names(q) == ["a.json"]


class _Clock:
    def __init__(self, trusted):
        self.trusted = trusted
        self.seen = []

    def note_gnss(self, value):
        self.seen.append(value)


def test_untrusted_clock_holds_batches_undated_then_dates_them(q, monkeypatch):
    """#184-9 / #240: before the clock is trusted nothing timestamped is posted, but nothing
    is lost either; once trusted the held batch goes out with a date from the monotonic clock."""
    import asyncio
    up = _load_uplink(q)
    monkeypatch.setattr(up.clock_trust, "boot_id", lambda: "boot-1")
    up.CLOCK = _Clock(False)
    up.AGG_SECONDS = 0.01
    posted = []
    up._post = posted.append

    async def one_window(win):
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(up.flusher(win, loop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not win:
                break
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(one_window({("gps", "navigation.speedOverGround"): ("num", 3.0)}))
    assert posted == []
    held = _names(q / "undated")
    assert len(held) == 1 and _names(q) == []
    assert json.loads((q / "undated" / held[0]).read_text())["readings"][0]["time"] is None

    up.CLOCK.trusted = True
    asyncio.run(one_window({("gps", "navigation.speedOverGround"): ("num", 4.0)}))
    vals = [b["readings"][0]["value"] for b in posted]
    assert sorted(vals) == [3.0, 4.0]
    times = [b["readings"][0]["time"] for b in posted]
    assert all(t and t.endswith("Z") for t in times)
    assert _names(q / "undated") == []


def test_batches_held_in_an_earlier_boot_are_kept_not_sent(q, monkeypatch):
    up = _load_uplink(q)
    monkeypatch.setattr(up.clock_trust, "boot_id", lambda: "boot-2")
    (q / "undated").mkdir()
    _write(q / "undated", "00000000000000000001.json",
           {"boat_id": "sr33", "mono": 1.0, "boot": "boot-1",
            "readings": [{"time": None, "source": "s", "path": "p", "value": 1}]})
    up._date_held()
    assert _names(q / "undated") == ["00000000000000000001.json"]
    assert _names(q) == []


def test_gnss_datetime_is_offered_to_the_clock(q):
    up = _load_uplink(q)
    clock = _Clock(False)
    msg = json.dumps({"updates": [{"$source": "gps", "values": [
        {"path": "navigation.datetime", "value": "2026-11-06T12:00:00.000Z"}]}]})
    up.parse_delta(msg, {}, {}, {"self": None, "clock": clock})
    assert clock.seen == ["2026-11-06T12:00:00.000Z"]


def test_ais_is_not_sent_while_the_clock_is_untrusted(q):
    import asyncio
    up = _load_uplink(q)
    up.CLOCK = _Clock(False)
    up.AGG_SECONDS = 0.01
    sent = []
    up._post_ais = sent.append

    async def go():
        win = {1: {"lat": 1.0, "lon": 2.0}}
        task = asyncio.create_task(up.ais_flusher(win, asyncio.get_running_loop()))
        await asyncio.sleep(0.1)
        task.cancel()
    asyncio.run(go())
    assert sent == []


def test_a_failing_hold_does_not_kill_the_flusher_or_lose_the_batch(q):
    import asyncio
    up = _load_uplink(q)
    up.CLOCK = _Clock(False)
    up.AGG_SECONDS = 0.01
    real_hold, calls = up.hold, {"n": 0}

    def flaky_hold(readings, mono=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_hold(readings, mono)
    up.hold = flaky_hold

    async def go():
        win = {("gps", "navigation.speedOverGround"): ("num", 3.0)}
        task = asyncio.create_task(up.flusher(win, asyncio.get_running_loop()))
        await asyncio.sleep(0.2)
        alive = not task.done()
        task.cancel()
        return alive
    alive = asyncio.run(go())
    assert alive
    held = _names(q / "undated")
    assert len(held) == 1
    assert json.loads((q / "undated" / held[0]).read_text())["readings"][0]["value"] == 3.0


def test_promotion_is_a_single_move_and_never_queues_twice(q, monkeypatch):
    """A crash after dating but before the move: the next pass just moves it, once."""
    up = _load_uplink(q)
    monkeypatch.setattr(up.clock_trust, "boot_id", lambda: "boot-1")
    up.hold([{"time": None, "source": "s", "path": "p", "value": 7}])
    real_promote = up._promote

    def crash(f, stamp):
        raise OSError("power cut")
    up._promote = crash
    try:
        up._date_held()
    except OSError:
        pass
    assert _names(q) == []                           # nothing queued yet
    held = _names(q / "undated")
    assert len(held) == 1
    assert json.loads((q / "undated" / held[0]).read_text())["readings"][0]["time"]
    up._promote = real_promote
    up._date_held()
    up._date_held()
    queued = _names(q)
    assert len(queued) == 1 and _names(q / "undated") == []
    body = json.loads((q / queued[0]).read_text())
    assert set(body) == {"boat_id", "readings"} and body["readings"][0]["value"] == 7


def test_retry_list_is_bounded(q):
    import asyncio
    up = _load_uplink(q)
    up.CLOCK = _Clock(False)
    up.AGG_SECONDS = 0.005
    up.RETRY_MAX = 3

    def broken(readings, mono=None):
        raise OSError(28, "No space left on device")
    up.hold = broken
    retry = []

    async def go():
        win = {}
        task = asyncio.create_task(up.flusher(win, asyncio.get_running_loop(), retry))
        for k in range(40):
            win[("gps", "p")] = ("num", float(k))
            await asyncio.sleep(0.01)
        task.cancel()
        return win
    win = asyncio.run(go())
    assert len(retry) == 3
    assert win                                        # newest values still held in the window


def test_held_batches_go_out_after_trust_even_on_a_quiet_bus(q, monkeypatch):
    import asyncio
    up = _load_uplink(q)
    monkeypatch.setattr(up.clock_trust, "boot_id", lambda: "boot-1")
    up.hold([{"time": None, "source": "s", "path": "p", "value": 5}])
    up.CLOCK = _Clock(True)
    up.AGG_SECONDS = 0.01
    posted = []
    up._post = posted.append

    async def go():
        task = asyncio.create_task(up.flusher({}, asyncio.get_running_loop()))
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(go())
    assert [b["readings"][0]["value"] for b in posted] == [5]
    assert _names(q / "undated") == [] and _names(q) == []


def test_queued_batches_are_retried_on_a_quiet_bus(q):
    """The link was down when held batches were queued; when it comes back, the queue drains
    even with no new telemetry."""
    import asyncio
    up = _load_uplink(q)
    _write(q, "20261106T120000Z.json", _batch("2026-11-06T12:00:00Z", 8))
    up.CLOCK = _Clock(True)
    up.AGG_SECONDS = 0.01
    posted = []
    up._post = posted.append

    async def go():
        task = asyncio.create_task(up.flusher({}, asyncio.get_running_loop()))
        await asyncio.sleep(0.2)
        task.cancel()
    asyncio.run(go())
    assert [b["readings"][0]["value"] for b in posted] == [8]
    assert _names(q) == []


def test_replay_is_paged_so_a_backlog_never_holds_up_the_live_window(q):
    """#184-8: the queue has no cap, so after a long outage it can hold ~170k batches. Replaying
    them all in one pass blocks the flusher for hours, and meanwhile the window keeps only the
    latest value per reading: hours of windows would fold into one. A pass stops after its time
    budget; the rest stays queued, in order, for the next pass."""
    up = _load_uplink(q)
    for k in range(50):
        _write(q, f"20261106T12{k:04d}Z.json", _batch("2026-11-06T12:00:00Z", k))
    now = [0.0]
    up._clock = lambda: now[0]
    up.REPLAY_BUDGET_S = 5.0
    posted = []

    def post(batch):                                  # each POST takes one "second"
        now[0] += 1.0
        posted.append(batch["readings"][0]["value"])
    up._post = post
    up._flush_queue()
    assert posted == [0, 1, 2, 3, 4]
    assert len(_names(q)) == 45                       # the rest is still queued, not dropped
    for _ in range(20):
        up._flush_queue()
    assert posted == list(range(50)) and _names(q) == []


def test_a_long_outage_keeps_every_batch_no_cap(q):
    """Cole on #243: no cap on the queue, nothing evicted. A link down for a long stretch keeps
    every batch on disk, and every one goes out, oldest first, when it comes back."""
    up = _load_uplink(q)
    up.CLOCK = _Clock(True)

    def down(batch):
        raise urllib.error.URLError("no route")
    up._post = down
    for k in range(3000):                             # one batch a minute, ~2 days
        up.send([{"time": f"2026-10-{1 + k // 1440:02d}T{k % 1440 // 60:02d}:{k % 60:02d}:00Z",
                  "source": "s", "path": "p", "value": k}])
    assert len(_names(q)) == 3000
    posted = []
    up._post = lambda b: posted.append(b["readings"][0]["value"])
    up.REPLAY_BUDGET_S = 1e9
    up._flush_queue()
    assert posted == list(range(3000))
    assert _names(q) == [] and _names(q / "quarantine") == []


def test_live_windows_keep_their_cadence_while_a_backlog_replays(q):
    """Codex on #283: replay that spends its budget must not stretch the live window. Windows
    are cut on a fixed cadence; with a big backlog each pass replays for up to its budget and
    the next window is still cut on time."""
    import asyncio
    import time as _time
    up = _load_uplink(q)
    for k in range(5000):
        _write(q, f"20261001T{k:06d}Z.json", _batch("2026-10-01T00:00:00Z", -1 - k))
    up.CLOCK = _Clock(True)
    up.AGG_SECONDS = 0.1
    up.REPLAY_BUDGET_S = 0.06
    live = []

    def post(batch):
        v = batch["readings"][0]["value"]
        if v >= 0:
            live.append(v)
        else:
            _time.sleep(0.01)                         # a queued batch takes a while to send
    up._post = post

    async def go():
        win = {}
        task = asyncio.create_task(up.flusher(win, asyncio.get_running_loop()))
        t0 = _time.monotonic()
        k = 0
        while _time.monotonic() - t0 < 1.55:
            win[("gps", "p")] = ("num", float(k))
            k += 1
            await asyncio.sleep(0.005)
        task.cancel()
    asyncio.run(go())
    # 1.55 s at a 0.1 s cadence is ~15 windows; stretched by the 0.06 s budget it was ~9.
    assert len(live) >= 12, live
    assert len(_names(q)) < 5000                      # and the backlog did drain meanwhile


def test_a_slow_live_link_does_not_strand_the_backlog(q):
    """Codex on #283: with every live POST longer than a window and a busy bus, the backlog
    must still drain (the replayer runs beside the live flusher, not after it)."""
    import asyncio
    import time as _time
    up = _load_uplink(q)
    for k in range(20):
        _write(q, f"20261001T{k:06d}Z.json", _batch("2026-10-01T00:00:00Z", -1 - k))
    up.CLOCK = _Clock(True)
    up.AGG_SECONDS = 0.05
    replayed = []

    def post(batch):
        v = batch["readings"][0]["value"]
        if v >= 0:
            _time.sleep(0.08)                         # live POSTs slower than a window
        else:
            replayed.append(v)
    up._post = post

    async def go():
        win = {}
        task = asyncio.create_task(up.flusher(win, asyncio.get_running_loop()))
        t0 = _time.monotonic()
        k = 0
        while _time.monotonic() - t0 < 0.6:
            win[("gps", "p")] = ("num", float(k))
            k += 1
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert replayed == [-1 - k for k in range(20)] and _names(q) == []
