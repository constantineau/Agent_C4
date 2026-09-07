/* Race Rewind — console shim.
 *
 * Loaded INSIDE the dashboard iframe, ahead of dashboard.js, so it can patch the two browser
 * APIs the console builds on before a single line of app code runs. Nothing in dashboard.js is
 * modified: the console still thinks it is polling a live engine over /api.
 *
 *   fetch()       -> answered from the current replay frame instead of the network.
 *   setInterval() -> the app's 3 s / 15 s poll timers are compressed, because every "request"
 *                    is now a memory lookup. Without this, scrubbing to a new moment would take
 *                    up to 3 s to appear and the rig would feel broken.
 *
 * The parent page owns the clock and pushes each frame in by postMessage.
 */
(function () {
  "use strict";

  var FRAME = null;             // { t, data: { "/navigator": {...}, ... } }
  var POLLERS = [];             // the app's own poll callbacks, so a scrub can fire them at once
  var realFetch = window.fetch.bind(window);

  /* ---- frames in from the parent ---- */
  window.addEventListener("message", function (ev) {
    var m = ev.data;
    if (!m || m.type !== "replay:frame") return;
    FRAME = m.frame || null;
    // Nudge every registered poller so the new moment paints immediately rather than on its
    // next natural tick. The app's callbacks are idempotent reads, so calling them is safe.
    POLLERS.forEach(function (fn) { try { fn(); } catch (e) { /* app handles its own errors */ } });
  });

  /* ---- fetch -> frame lookup ---- */
  function endpointOf(url) {
    var s = String(url);
    var i = s.indexOf("/api/");
    if (i < 0) return null;
    return "/" + s.slice(i + 5);          // "/api/navigator?x=1" -> "/navigator?x=1"
  }

  function lookup(ep) {
    if (!FRAME || !FRAME.data) return undefined;
    if (ep in FRAME.data) return FRAME.data[ep];
    var bare = ep.split("?")[0];          // "/forecast?hours=6" -> "/forecast"
    if (bare in FRAME.data) return FRAME.data[bare];
    return undefined;
  }

  function jsonResponse(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { "Content-Type": "application/json" }
    });
  }

  window.fetch = function (url, opts) {
    var ep = endpointOf(url);
    if (ep === null) {
      // /copilot/* and anything else: let it go to the server, which 404s. dashboard.js
      // already treats a failed copilot poll as "keep the deterministic text".
      return realFetch(url, opts);
    }
    var val = lookup(ep);
    if (val === undefined) {
      // Not captured in this timeline (e.g. /series, or a network-dependent endpoint). The
      // console's fetchJSON maps a non-OK response to null, which every tile already handles.
      return Promise.resolve(jsonResponse({ replay: "not captured", endpoint: ep }, 404));
    }
    if (val === null) return Promise.resolve(jsonResponse({ replay: "null in frame" }, 404));
    return Promise.resolve(jsonResponse(val, 200));
  };

  /* ---- timers: compress the poll loops ---- */
  var nativeSetInterval = window.setInterval;
  window.setInterval = function (fn, ms) {
    if (typeof fn === "function" && ms >= 1000) {
      POLLERS.push(fn);                    // so a scrub can fire it at once
      ms = 400;                            // memory lookups — no reason to wait 3 s
    }
    return nativeSetInterval.call(window, fn, ms);
  };

  /* ---- tell the parent which tile is open, so a note can be filed against it ---- */
  document.addEventListener("click", function (ev) {
    var el = ev.target && ev.target.closest && ev.target.closest("[data-tile]");
    if (!el) return;
    parent.postMessage({ type: "replay:tile", tile: el.getAttribute("data-tile") }, "*");
  }, true);

  parent.postMessage({ type: "replay:ready" }, "*");
})();
