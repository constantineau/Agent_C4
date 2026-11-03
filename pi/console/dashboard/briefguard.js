/* The copilot's dashboard brief laid over the engine's tiles, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4BriefGuard) and required by
   pi/console/test_briefguard.js under node.

   The engine owns every tile's status. The copilot LLM writes commentary and may suggest a status,
   under three rules (#176):
     1. It may only RAISE a tile (ok → watch → act). It can never turn a tile green, lower a
        warning, or touch an n/a tile.
     2. A raise lasts only while the tile is still at the status the model saw. Once the engine
        moves that tile, either way, the raise is gone for good (it does not come back if the tile
        returns to the old status; the brief's adjustment is marked `gone`). That includes moves
        made while the model was still answering: `track` counts every engine status change per
        tile, the brief carries the counts at its snapshot (`moves`), and a raise whose tile has
        moved since is dropped. It also expires ADJUST_TTL_S after the
        snapshot the model read, not after the slow reply arrived.
     3. The snapshot sent to the model is the engine's own statuses, never a status the model
        itself raised earlier. */
(function (root) {
  "use strict";

  const SEV = { ok: 0, watch: 1, act: 2 };
  const BRIEF_TTL_S = 300;     // commentary (focus + notes) stays up this long after its snapshot
  const ADJUST_TTL_S = 180;    // a status raise stays up at most this long after its snapshot

  /* the engine's own status for a tile, even after an overlay raised it */
  function engineStatus(t) {
    if (!t) return "na";
    return t.engineStatus || t.status || "na";
  }

  /* is this raise allowed on this tile right now? */
  function raiseOk(a, t, ageS) {
    if (!a || a.gone || !t || ageS > ADJUST_TTL_S) return false;
    const now = engineStatus(t);
    if (a.from !== now) { a.gone = true; return false; }             // engine moved: gone for good
    if (!(now in SEV) || !(a.status in SEV)) return false;          // n/a tile or junk status
    return SEV[a.status] > SEV[now];                                 // never down, no no-ops
  }

  /* overlay the brief `b` ({focus, notes, adjust, mode, t: snapshot epoch s}) on the engine read
     `d` ({tiles, focus, notes, ...}) at `nowS`. `d.tiles` must be the fresh engine build. Each
     tile is replaced by a copy carrying `engineStatus`; the engine commentary stands when the brief
     is missing, not an LLM brief, or older than BRIEF_TTL_S. */
  function overlay(d, b, nowS, track) {
    // count engine status changes per tile (across polls), before any raise is applied
    if (track) {
      for (const k in d.tiles) {
        const s = d.tiles[k] ? (d.tiles[k].status || "na") : "na";
        if (k in track.last && track.last[k] !== s) track.moves[k] = (track.moves[k] || 0) + 1;
        track.last[k] = s;
      }
    }
    // copy each tile (a build may reuse a source object; a raise must never stick to it) and
    // record the engine's status before anything else touches it
    for (const k in d.tiles) {
      const t = d.tiles[k];
      if (!t) continue;
      const c = Object.assign({}, t, { engineStatus: t.status || "na" });
      delete c.llmNote;
      d.tiles[k] = c;
    }
    if (!b || b.mode !== "llm" || !(nowS - b.t <= BRIEF_TTL_S)) return d;
    const ageS = nowS - b.t;
    for (const a of b.adjust || []) {
      const t = d.tiles[a && a.tile];
      if (a && track && b.moves && (track.moves[a.tile] || 0) !== (b.moves[a.tile] || 0)) a.gone = true;
      if (raiseOk(a, t, ageS)) { t.status = a.status; t.llmNote = a.reason; }
    }
    const notes = (b.notes || []).map((n) => ({ tile: n.tile, status: (d.tiles[n.tile] || {}).status || "ok",
      text: n.text, conf: n.conf || "med" }));
    return { tiles: d.tiles, focus: b.focus || d.focus, notes: notes.length ? notes : d.notes,
      confidence: "llm", mode: "llm-live" };
  }

  /* per-tile engine status change counts; keep one per dashboard and pass it to every overlay */
  function newTrack() { return { last: {}, moves: {} }; }

  const api = { overlay, engineStatus, raiseOk, newTrack, SEV, BRIEF_TTL_S, ADJUST_TTL_S };
  root.C4BriefGuard = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
