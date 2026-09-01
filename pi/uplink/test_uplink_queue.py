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


@pytest.mark.parametrize("code", [500, 502, 503])
def test_http_5xx_is_transient_and_retained(q, code):
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
