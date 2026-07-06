/* C4 tactics labeling — vanilla-JS single-page ranker.
   Login -> rank candidate briefs best->worst + flag calibration -> submit -> next.
   Candidates are BLIND (no origin shown/inferred). Same-origin JSON API. */
(function () {
  "use strict";

  var LS_KEY = "c4label";               // {labeler_id, name}
  var $ = function (id) { return document.getElementById(id); };

  // ---- view refs ----
  var loginView = $("login"), rankerView = $("ranker"), doneView = $("done");

  // ---- app state ----
  var session = null;      // {labeler_id, name}
  var snapshot = null;     // current snapshot object
  var candMap = {};        // candidate_id -> candidate object
  var order = [];          // candidate_ids, best-first (the ranking)
  var calibration = {};    // candidate_id -> "right"|"too_high"|"too_low"
  var loadedAt = 0;        // ms timestamp when the current task loaded
  var dragId = null;       // candidate_id currently being dragged

  // ============================================================= helpers
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(readJson);
  }
  function get(path) { return fetch(path).then(readJson); }
  function readJson(r) {
    return r.json().catch(function () { return {}; }).then(function (data) {
      return { ok: r.ok, status: r.status, data: data };
    });
  }
  function showView(v) {
    loginView.hidden = (v !== "login");
    rankerView.hidden = (v !== "ranker");
    doneView.hidden = (v !== "done");
  }
  function banner(msg) {
    var b = $("errBanner");
    if (!msg) { b.hidden = true; b.textContent = ""; return; }
    b.hidden = false; b.textContent = msg;
  }

  // ============================================================= login
  function doLogin() {
    var name = $("loginName").value.trim();
    var password = $("loginPw").value;
    $("loginErr").textContent = "";
    if (!name) { $("loginErr").textContent = "Enter your name."; return; }
    $("loginBtn").disabled = true;
    post("api/login", { name: name, password: password }).then(function (res) {
      $("loginBtn").disabled = false;
      if (!res.ok) {
        $("loginErr").textContent = (res.data && res.data.detail) || "Login failed.";
        return;
      }
      session = { labeler_id: res.data.labeler_id, name: res.data.name };
      localStorage.setItem(LS_KEY, JSON.stringify(session));
      startRanking();
    }).catch(function () {
      $("loginBtn").disabled = false;
      $("loginErr").textContent = "Network error — try again.";
    });
  }

  function signOut(e) {
    if (e) e.preventDefault();
    localStorage.removeItem(LS_KEY);
    session = null; snapshot = null;
    $("loginPw").value = "";
    showView("login");
  }

  // ============================================================= task load
  function startRanking() {
    $("whoami").textContent = session.name;
    showView("ranker");
    loadNext();
  }

  function loadNext() {
    banner("");
    get("api/next?labeler_id=" + encodeURIComponent(session.labeler_id) + "&_=" + Date.now()).then(function (res) {
      if (!res.ok) {
        if (res.status === 400) { signOut(); return; }  // bad/expired labeler_id
        banner((res.data && res.data.detail) || "Could not load the next task.");
        return;
      }
      var d = res.data;
      renderProgress(d.progress, d.my_done);
      if (d.done) { showDone(d); return; }
      loadTask(d.snapshot, d.candidates);
    }).catch(function () {
      banner("Network error loading the next task — check the connection.");
    });
  }

  function loadTask(snap, candidates) {
    snapshot = snap;
    candMap = {};
    order = [];
    calibration = {};
    candidates.forEach(function (c) { candMap[c.candidate_id] = c; order.push(c.candidate_id); });
    loadedAt = Date.now();
    $("notes").value = "";
    renderSituation();
    renderCards();
    $("submitBtn").disabled = false;
    $("submitBtn").textContent = "Submit ranking → next";
    window.scrollTo(0, 0);
  }

  // ============================================================= progress
  function renderProgress(p, myDone) {
    if (!p) { $("progressLine").textContent = " "; return; }
    $("progressLine").textContent =
      "covered " + (p.covered || 0) + "/" + (p.snapshots || 0) + " snapshots" +
      "  ·  my rankings: " + (myDone || 0) +
      "  ·  overlap " + (p.double_labeled || 0) + "/" + (p.overlap_target || 0);
  }

  // ============================================================= left panel
  function confChip(conf) {
    if (!conf) return "";
    return '<span class="chip conf-' + esc(conf) + '">' + esc(conf) + "</span>";
  }

  // ============================================================= scene (SVG compass diagram)
  var CLR = { accent: "#43a7ff", warn: "#ffc24b", ok: "#37d39b", bad: "#ff5d5d",
              muted: "#8499ad", text: "#e7edf4", line: "#2c3f52", boat: "#e7edf4" };
  function _polar(cx, cy, r, deg) {
    var a = deg * Math.PI / 180; return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  }
  function _round(p) { return Math.round(p * 10) / 10; }
  function _arrow(x1, y1, x2, y2, color, w, dash) {
    var ang = Math.atan2(y2 - y1, x2 - x1), hl = 8.5, hw = 4.6;
    var bx = x2 - hl * Math.cos(ang), by = y2 - hl * Math.sin(ang);
    var ax = bx - hw * Math.sin(ang), ay = by + hw * Math.cos(ang);
    var cx = bx + hw * Math.sin(ang), cy = by - hw * Math.cos(ang);
    return '<line x1="' + _round(x1) + '" y1="' + _round(y1) + '" x2="' + _round(bx) + '" y2="' + _round(by) +
      '" stroke="' + color + '" stroke-width="' + w + '"' + (dash ? ' stroke-dasharray="5 3"' : "") +
      ' stroke-linecap="round"/><polygon points="' + _round(x2) + "," + _round(y2) + " " + _round(ax) + "," +
      _round(ay) + " " + _round(cx) + "," + _round(cy) + '" fill="' + color + '"/>';
  }
  function _sector(cx, cy, r, b1, b2, fill) {
    var p1 = _polar(cx, cy, r, b1), p2 = _polar(cx, cy, r, b2);
    var large = (((b2 - b1) % 360 + 360) % 360) > 180 ? 1 : 0;
    return '<path d="M' + cx + "," + cy + " L" + _round(p1[0]) + "," + _round(p1[1]) + " A" + r + "," + r +
      " 0 " + large + " 1 " + _round(p2[0]) + "," + _round(p2[1]) + ' Z" fill="' + fill + '"/>';
  }
  function _txt(x, y, s, color, size, anchor) {
    return '<text x="' + _round(x) + '" y="' + _round(y) + '" fill="' + color + '" font-size="' + (size || 10) +
      '" text-anchor="' + (anchor || "middle") + '" font-family="system-ui,sans-serif">' + esc(s) + "</text>";
  }
  // A wind blowing FROM `deg`: an arrow from the rim inward toward the boat (points downwind).
  function _wind(cx, cy, R, deg, color, w, dash) {
    var o = _polar(cx, cy, R - 4, deg), i = _polar(cx, cy, 40, deg);
    return _arrow(o[0], o[1], i[0], i[1], color, w, dash);
  }
  function drawScene(sc) {
    var W = 300, cx = 150, cy = 152, R = 118;
    var g = [];
    var rhumb = sc.rhumb_deg, w = sc.wind || {}, fc = sc.forecast, fav = sc.favored_side;
    // favoured-side wedge (subtle) — the side of the rhumb that pays
    if (fav === "left") g.push(_sector(cx, cy, R, rhumb - 82, rhumb, "rgba(55,211,155,.07)"));
    if (fav === "right") g.push(_sector(cx, cy, R, rhumb, rhumb + 82, "rgba(55,211,155,.07)"));
    // compass ring + cardinal ticks
    g.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + R + '" fill="none" stroke="' + CLR.line + '" stroke-width="1"/>');
    ["N", "E", "S", "W"].forEach(function (lbl, k) {
      var p = _polar(cx, cy, R - 11, k * 90);
      g.push(_txt(p[0], p[1] + 3.5, lbl, CLR.muted, 9));
    });
    // rhumb line + next mark
    var mk = sc.mark || {}, mp = _polar(cx, cy, R * 0.7, rhumb), bp = _polar(cx, cy, 20, rhumb);
    g.push('<line x1="' + _round(bp[0]) + '" y1="' + _round(bp[1]) + '" x2="' + _round(mp[0]) + '" y2="' + _round(mp[1]) +
      '" stroke="' + CLR.muted + '" stroke-width="1.3" stroke-dasharray="2 3"/>');
    g.push('<circle cx="' + _round(mp[0]) + '" cy="' + _round(mp[1]) + '" r="4.5" fill="' + CLR.warn + '"/>');
    var ml = _polar(cx, cy, R * 0.7 + 13, rhumb);
    g.push(_txt(ml[0], ml[1], (mk.name || "mark") + (mk.distance_nm != null ? " " + mk.distance_nm + " nm" : ""), CLR.text, 9.5));
    // wind: baseline (faded) -> now (bold); the shift
    if (w.base_deg != null && w.now_deg !== w.base_deg)
      g.push(_wind(cx, cy, R, w.base_deg, CLR.muted, 1.6, false));
    if (w.now_deg != null) g.push(_wind(cx, cy, R, w.now_deg, CLR.accent, 3, false));
    // forecast drift arrow (dashed, amber)
    if (fc && fc.now_deg != null) g.push(_wind(cx, cy, R, fc.now_deg, CLR.warn, 2.4, true));
    // boat glyph at centre, pointing at heading
    var b = sc.boat || {}, hd = b.heading_deg || 0;
    var bow = _polar(cx, cy, 15, hd), sl = _polar(cx, cy, 9, hd + 138), sr = _polar(cx, cy, 9, hd - 138);
    g.push('<polygon points="' + _round(bow[0]) + "," + _round(bow[1]) + " " + _round(sl[0]) + "," + _round(sl[1]) +
      " " + _round(sr[0]) + "," + _round(sr[1]) + '" fill="' + CLR.boat + '" stroke="#0b1017" stroke-width="1"/>');
    // labels (wind now / was / forecast) near the rim
    if (w.now_deg != null) { var lp = _polar(cx, cy, R + 12, w.now_deg); g.push(_txt(lp[0], lp[1] + 3, w.now_deg + "°", CLR.accent, 10)); }
    if (w.base_deg != null && w.now_deg !== w.base_deg) { var lb = _polar(cx, cy, R + 12, w.base_deg); g.push(_txt(lb[0], lb[1] + 3, "was " + w.base_deg + "°", CLR.muted, 8.5)); }
    if (fc && fc.now_deg != null) { var lf = _polar(cx, cy, R + 12, fc.now_deg); g.push(_txt(lf[0], lf[1] + 3, "fcst " + fc.now_deg + "°", CLR.warn, 8.5)); }
    // caption strip
    var shiftTxt = "";
    if (w.persistent && w.now_deg !== w.base_deg) {
      var d = ((w.now_deg - w.base_deg + 540) % 360) - 180;
      shiftTxt = "wind " + (d > 0 ? "right" : "left") + " " + Math.abs(Math.round(d)) + "°" + (fav ? " · " + fav + " favored" : "");
    } else if (w.oscillation_deg) { shiftTxt = "oscillating ±" + Math.round(w.oscillation_deg / 2) + "°"; }
    var cap = _txt(cx, W - 6, shiftTxt, CLR.muted, 10);
    return '<svg viewBox="0 0 ' + W + " " + (W + 4) + '" width="100%" role="img" aria-label="situation diagram">' +
      g.join("") + cap + "</svg>";
  }

  function renderSituation() {
    var s = snapshot;
    $("scenarioTag").textContent = s.scenario_tag || "situation";
    var pb = $("playbookBadge");
    if (s.has_playbook) { pb.textContent = "playbook aboard"; pb.className = "badge has"; }
    else { pb.textContent = "NO playbook"; pb.className = "badge no"; }

    $("situation").textContent = s.situation || "";

    var sceneEl = $("scene");
    if (s.scene) { sceneEl.innerHTML = drawScene(s.scene); sceneEl.hidden = false; }
    else { sceneEl.innerHTML = ""; sceneEl.hidden = true; }

    var gp = s.game_plan || {};
    var gpEl = $("gameplan");
    gpEl.textContent = gp.text || "—";
    gpEl.className = "gameplan" + (gp.has_playbook === false ? " noplan" : "");

    var board = $("board");
    board.innerHTML = "";
    (s.picture || []).forEach(function (item) {
      var li = document.createElement("li");
      li.innerHTML =
        '<span class="bsig">' + esc(item.signal) + "</span>" +
        '<span class="bread">' + esc(item.read) + "</span>" +
        confChip(item.confidence);
      board.appendChild(li);
    });

    var con = s.concordance || {};
    var conHtml = "";
    if (con.strength) conHtml += '<span class="cstrength">' + esc(con.strength) + "</span>";
    if (con.note) conHtml += (conHtml ? " — " : "") + esc(con.note);
    $("concordance").innerHTML = conHtml || '<span class="cstrength">—</span>';

    var cav = $("caveats");
    var caveats = s.caveats || [];
    if (caveats.length) {
      cav.innerHTML = "<strong>Caveats</strong><ul>" +
        caveats.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("") + "</ul>";
    } else { cav.innerHTML = ""; }
  }

  // ============================================================= cards
  function renderCards() {
    var wrap = $("cards");
    wrap.innerHTML = "";
    order.forEach(function (cid, idx) {
      wrap.appendChild(buildCard(candMap[cid], idx));
    });
  }

  function buildCard(c, idx) {
    var card = document.createElement("div");
    card.className = "card";
    card.setAttribute("draggable", "true");
    card.dataset.id = c.candidate_id;

    var chips = "";
    if (c.urgency)    chips += '<span class="chip urg-' + esc(c.urgency) + '">' + esc(c.urgency) + "</span>";
    if (c.confidence) chips += '<span class="chip conf-' + esc(c.confidence) + '">conf ' + esc(c.confidence) + "</span>";
    if (c.vs_playbook) chips += '<span class="chip pb-' + esc(c.vs_playbook) + '">' + esc(c.vs_playbook) + "</span>";
    if (c.grounded_ok === false) chips += '<span class="chip ungrounded" title="not fully grounded in engine facts">ungrounded</span>';

    var cur = calibration[c.candidate_id] || "";
    function seg(val, lbl) {
      return '<button type="button" data-cal="' + val + '"' + (cur === val ? ' class="on"' : "") + ">" + lbl + "</button>";
    }

    card.innerHTML =
      '<div class="rankcol">' +
        '<div class="rankno">' + (idx + 1) + "</div>" +
        '<div class="movebtns">' +
          '<button type="button" class="up" title="move up"' + (idx === 0 ? " disabled" : "") + ">↑</button>" +
          '<button type="button" class="down" title="move down"' + (idx === order.length - 1 ? " disabled" : "") + ">↓</button>" +
        "</div>" +
        '<div class="drag-handle" title="drag to reorder">☰</div>' +
      "</div>" +
      '<div class="body">' +
        '<div class="assessment">' + esc(c.assessment) + "</div>" +
        '<div class="action"><b>Action:</b> ' + esc(c.action) + "</div>" +
        '<div class="rationale">' + esc(c.rationale) + "</div>" +
        '<div class="cardchips">' + chips + "</div>" +
        '<div class="calib">' +
          '<span class="clabel">Confidence/urgency:</span>' +
          '<div class="seg">' +
            seg("right", "right") + seg("too_high", "too high") + seg("too_low", "too low") +
          "</div>" +
        "</div>" +
      "</div>";

    // move buttons
    card.querySelector(".up").addEventListener("click", function () { move(c.candidate_id, -1); });
    card.querySelector(".down").addEventListener("click", function () { move(c.candidate_id, +1); });

    // calibration segmented control (toggle off if re-clicked)
    card.querySelectorAll(".seg button").forEach(function (b) {
      b.addEventListener("click", function () {
        var val = b.dataset.cal;
        if (calibration[c.candidate_id] === val) delete calibration[c.candidate_id];
        else calibration[c.candidate_id] = val;
        renderCards();
      });
    });

    // drag & drop
    card.addEventListener("dragstart", function (e) {
      dragId = c.candidate_id; card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", c.candidate_id); } catch (_) {}
    });
    card.addEventListener("dragend", function () {
      dragId = null; card.classList.remove("dragging");
      document.querySelectorAll(".card.dragover").forEach(function (el) { el.classList.remove("dragover"); });
    });
    card.addEventListener("dragover", function (e) {
      e.preventDefault(); e.dataTransfer.dropEffect = "move";
      if (dragId && dragId !== c.candidate_id) card.classList.add("dragover");
    });
    card.addEventListener("dragleave", function () { card.classList.remove("dragover"); });
    card.addEventListener("drop", function (e) {
      e.preventDefault(); card.classList.remove("dragover");
      if (dragId && dragId !== c.candidate_id) reorder(dragId, c.candidate_id);
    });

    return card;
  }

  function move(cid, delta) {
    var i = order.indexOf(cid);
    var j = i + delta;
    if (i < 0 || j < 0 || j >= order.length) return;
    order.splice(i, 1);
    order.splice(j, 0, cid);
    renderCards();
  }

  function reorder(fromId, toId) {
    var i = order.indexOf(fromId), j = order.indexOf(toId);
    if (i < 0 || j < 0) return;
    order.splice(i, 1);
    order.splice(order.indexOf(toId) + (i < j ? 1 : 0), 0, fromId);
    renderCards();
  }

  // ============================================================= submit
  function submit() {
    if (!snapshot) return;
    $("submitBtn").disabled = true;
    $("submitBtn").textContent = "Submitting…";
    var body = {
      labeler_id: session.labeler_id,
      snapshot_id: snapshot.snapshot_id,
      order: order.slice(),
      calibration: calibration,
      notes: $("notes").value.trim(),
      elapsed_ms: Date.now() - loadedAt
    };
    post("api/rank", body).then(function (res) {
      if (!res.ok) {
        $("submitBtn").disabled = false;
        $("submitBtn").textContent = "Submit ranking → next";
        banner((res.data && res.data.detail) || "Could not submit — try again.");
        return;
      }
      loadNext();
    }).catch(function () {
      $("submitBtn").disabled = false;
      $("submitBtn").textContent = "Submit ranking → next";
      banner("Network error submitting — your ranking is still here, try again.");
    });
  }

  // ============================================================= done
  function showDone(d) {
    showView("done");
    var p = d.progress || {};
    var myDone = d.my_done || 0;
    var totalSnaps = p.snapshots || 0;
    // Only celebrate a genuine finish. my_done==0 means this labeler ranked nothing —
    // either the corpus isn't loaded yet (totalSnaps==0) or every snapshot is already
    // over-covered by others. Don't tell them "your rankings are in" when they aren't.
    if (myDone === 0) {
      if (totalSnaps === 0) {
        $("doneTitle").textContent = "Nothing to rank yet";
        $("doneMsg").textContent =
          "The task set isn't loaded on the server right now. Nothing was lost — check back shortly.";
      } else {
        $("doneTitle").textContent = "No tasks for you right now";
        $("doneMsg").textContent =
          "Every scenario already has enough rankings from other sailors. Thanks for stopping by!";
      }
      $("doneRetry").hidden = false;
    } else {
      $("doneTitle").textContent = "All done — thank you!";
      $("doneMsg").textContent = "Your rankings are in. Nice work.";
      $("doneRetry").hidden = true;
    }
    $("doneProgress").innerHTML =
      stat(myDone, "my rankings") +
      stat((p.covered || 0) + "/" + totalSnaps, "snapshots covered") +
      stat((p.double_labeled || 0) + "/" + (p.overlap_target || 0), "overlap / target") +
      stat(totalSnaps, "total snapshots");
  }
  function stat(n, l) {
    return '<div class="stat"><div class="n">' + esc(n) + '</div><div class="l">' + esc(l) + "</div></div>";
  }

  // ============================================================= wiring
  $("loginBtn").addEventListener("click", doLogin);
  $("loginPw").addEventListener("keydown", function (e) { if (e.key === "Enter") doLogin(); });
  $("loginName").addEventListener("keydown", function (e) { if (e.key === "Enter") $("loginPw").focus(); });
  function on(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); }
  on("signout", "click", signOut);
  on("doneSignout", "click", signOut);
  on("doneRetry", "click", function () {
    if (!session) { showView("login"); return; }
    showView("ranker");
    loadNext();
  });
  on("submitBtn", "click", submit);

  // auto-resume from a stored session
  (function boot() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (raw) {
        var s = JSON.parse(raw);
        if (s && s.labeler_id) { session = s; startRanking(); return; }
      }
    } catch (_) {}
    showView("login");
  })();
})();
