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

  function renderSituation() {
    var s = snapshot;
    $("scenarioTag").textContent = s.scenario_tag || "situation";
    var pb = $("playbookBadge");
    if (s.has_playbook) { pb.textContent = "playbook aboard"; pb.className = "badge has"; }
    else { pb.textContent = "NO playbook"; pb.className = "badge no"; }

    $("situation").textContent = s.situation || "";

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
