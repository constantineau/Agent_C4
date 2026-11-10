"""Re-score the archived recordings with today's evidence code (#245) — a versioned run.

The refinements applied to the boat on 2026-09-16 (helm 0.931 + 24 overlay cells) were proposed
from bins that the evidence bugs fixed since (#49 SOG as boat speed, #50 track/debrief matching,
#63 binning, #68 fields for the recording's own time) helped shape. Cole, 2026-11-09: keep what is
applied, re-score the archive with a versioned run, compare, and he approves any replacement
(ADR-0003). This module does the re-scoring. It reads the archive and the stored tracks, fetches a
recording the Lab no longer stores from the boat log IN MEMORY (nothing is saved to the track
store), and files one `rescore_runs` row via `learning.save_rescore`. It never writes perf_bins,
proposals or the boat profile.
"""
from . import learning, track

# Bump when the code that turns a recording into bins changes in a way that should be re-scored.
EVIDENCE_VERSION = "2026-11-09 (#49 #50 #63 #68)"


def log_body_for(window_start, sessions):
    """The boat-log request for an archived recording: the session whose DERIVED window (or, for
    an old session, marker) starts at the archived window start. None when the boat log no longer
    has it."""
    near = lambda ts: ts is not None and abs(float(ts) - float(window_start)) < 1.0
    for s in sessions or ():
        if near((s.get("window") or {}).get("start_ts")):
            return {"session_id": s.get("id"), "start_ts": s.get("start_ts")}
    for s in sessions or ():
        if near(s.get("start_ts")):
            # archived on the marker, and the derived window no longer starts there: re-score the
            # SAME interval, or its bins would be filed under a recording they are not
            return {"session_id": s.get("id"), "start_ts": s.get("start_ts"), "use_marker": True}
    return None


def _track_for(rec, fetch_track):
    """(track, how) for one archived recording, or (None, why)."""
    ws, rid = rec.get("window_start"), rec["race_id"]
    if ws is None:
        # An uploaded GPX/YB track has no recording window, so nothing ties the stored file to
        # the one that debrief scored (a later upload replaces it). It also carries no
        # instrument wind: its cells are forecast-based and never teach the boat model.
        return None, "uploaded track: no instrument wind, never used for the boat model"
    t = track.load_track(rid, ws)
    if t is not None and track._near(track.recording_of(t), ws):
        return t, "stored"
    if fetch_track is None:
        return None, "not stored in the Lab"
    try:
        t, why = fetch_track(rid, ws)
    except Exception as exc:       # noqa: BLE001 — one missing recording must not stop the run
        return None, f"boat log fetch failed: {type(exc).__name__}"
    if not t:
        return None, why or "not on the boat log"
    if not track._near(track.recording_of(t), ws):
        return None, "the boat log now cuts this recording differently"
    return t, "boat log"


def run(boat_id, fetch_track=None, score=None, on_progress=None):
    """Re-score every archived recording of `boat_id` → the saved run (see learning.get_rescore).

    `fetch_track(race_id, window_start)` → (track, None) or (None, why) gets a recording the Lab
    does not store; `score(track, wave_coeffs, log)` defaults to `judge.score_recording`."""
    log = on_progress or (lambda *_: None)
    if score is None:
        from .judge import score_recording as score
    # what the boat uses NOW, read once before scoring: the bins are scored on these wave
    # coefficients and the comparison is against this model, even if the boat changes meanwhile
    against = learning.applied_model(boat_id)
    wave_coeffs = against.get("boat_wave_coeffs")
    recs = learning.archived_recordings(boat_id)
    bins, notes = [], []
    for r in recs:
        ws = r.get("window_start")
        log(f"re-scoring {r.get('race_name') or r['race_id']}"
            f"{' · recording ' + str(int(ws)) if ws else ''}…")
        note = {"race_id": r["race_id"], "race_name": r.get("race_name"), "window_start": ws,
                "debrief_id": r.get("id"), "old_polar_pct": r.get("polar_pct"),
                "track_source": r.get("track_source")}
        t, how = _track_for(r, fetch_track)
        if t is None:
            notes.append({**note, "status": "left_out", "note": how})
            continue
        s = score(t, wave_coeffs, log) or {}
        if not s.get("available"):
            notes.append({**note, "status": "left_out", "note": s.get("note") or "could not score"})
            continue
        got = [{**b, "race_id": r["race_id"], "window_start": ws} for b in s.get("perf_bins") or []]
        bins.extend(got)
        tr = s.get("trust") or {}
        notes.append({**note, "status": "scored", "track": how,
                      "hours": s.get("elapsed_hours"), "polar_pct": s.get("polar_pct"),
                      "n_bins": len(got),
                      "measured_bins": sum(1 for b in got if b.get("wind_source") == "measured"),
                      "samples": sum(b.get("samples") or 0 for b in got
                                     if b.get("wind_source") == "measured"),
                      "current_corrected": bool(s.get("current_corrected")),
                      "wave_corrected": bool(s.get("wave_corrected")),
                      "trust_refused": tr.get("refused_samples")})
    log("comparing with the boat model in use…")
    return learning.save_rescore(boat_id, EVIDENCE_VERSION, notes, bins, against=against)
