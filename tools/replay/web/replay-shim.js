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

  /* ---- fault injection ----
   * Reproduces the race-day failure mode so a fix can be measured rather than asserted. The
   * engine does not stall one endpoint at a time — a slow archive read or a heavy /strategy
   * blocks the lot — so this models WINDOWS in which every /api call fails, which is what
   * fetchJSON's 5 s abort looks like from the console's side.
   *   { stallMs, periodMs }  ->  fail everything for stallMs out of every periodMs
   */
  var FAULTS = null;
  window.addEventListener("message", function (ev) {
    if (ev.data && ev.data.type === "replay:faults") FAULTS = ev.data.faults || null;
  });
  function faulting() {
    if (!FAULTS || !FAULTS.periodMs) return false;
    return (Date.now() % FAULTS.periodMs) < (FAULTS.stallMs || 0);
  }

  /* ---- link overrides (the review page's "what if" panel) ----
   *   { mode: "normal" | "slow" | "lost", delayS, fail: ["/fleet", ...] }
   *   slow -> every /api answer waits delayS seconds, and the console's own AbortController
   *           (5-12 s per endpoint) still fires, so a 6 s delay kills the 5 s calls only
   *   lost -> every /api call fails like a dropped Wi-Fi link (fetch rejects)
   *   fail -> those endpoints answer 503; the rest are normal
   */
  var LINK = { mode: "normal", delayS: 0, fail: [] };
  window.addEventListener("message", function (ev) {
    if (ev.data && ev.data.type === "replay:link") {
      var l = ev.data.link || {};
      LINK = { mode: l.mode || "normal", delayS: Number(l.delayS) || 0, fail: l.fail || [] };
    }
  });
  function abortError() {
    try { return new DOMException("The operation was aborted.", "AbortError"); }
    catch (e) { var err = new Error("aborted"); err.name = "AbortError"; return err; }
  }
  function delayed(make, ms, signal) {
    return new Promise(function (resolve, reject) {
      if (signal && signal.aborted) return reject(abortError());
      var t = setTimeout(function () { resolve(make()); }, ms);
      if (signal) signal.addEventListener("abort", function () { clearTimeout(t); reject(abortError()); });
    });
  }

  window.fetch = function (url, opts) {
    var ep = endpointOf(url);
    if (ep !== null && faulting()) {
      // what the console sees when its own AbortController fires: a failed request -> null
      return Promise.resolve(jsonResponse({ replay: "injected stall" }, 504));
    }
    if (ep !== null && LINK.mode === "lost") {
      return Promise.reject(new TypeError("replay: link lost (override)"));
    }
    if (ep !== null && LINK.fail.indexOf(ep.split("?")[0]) >= 0) {
      return Promise.resolve(jsonResponse({ replay: "endpoint failed (override)", endpoint: ep }, 503));
    }
    if (ep !== null && LINK.mode === "slow" && LINK.delayS > 0) {
      return delayed(function () { return answer(ep); }, LINK.delayS * 1000, opts && opts.signal);
    }
    if (ep === null) {
      // /copilot/* and anything else: let it go to the server, which 404s. dashboard.js
      // already treats a failed copilot poll as "keep the deterministic text".
      return realFetch(url, opts);
    }
    return Promise.resolve(answer(ep));
  };

  /* the frame's answer for one endpoint, as a Response */
  function answer(ep) {
    var val = lookup(ep);
    if (val === undefined) {
      // Not captured in this timeline (e.g. /series, or a network-dependent endpoint). The
      // console's fetchJSON maps a non-OK response to null, which every tile already handles.
      return jsonResponse({ replay: "not captured", endpoint: ep }, 404);
    }
    if (val === null) return jsonResponse({ replay: "null in frame" }, 404);
    return jsonResponse(val, 200);
  }

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
