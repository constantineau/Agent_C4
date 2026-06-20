/* feedback.js — a small, self-contained "Feedback" widget that files a GitHub issue.

   Drop into any page (one <script> tag). Injects its own button + modal + styles (theme-neutral,
   high z-index) so it works in both the Lab and the crew dashboard without touching their CSS.
   Config via a global set BEFORE this script:
     window.FEEDBACK_CFG = { endpoint: "/api/feedback", source: "lab" }
   Defaults: endpoint = same-origin "/api/feedback"; source = location.hostname. */
(function () {
  var CFG = window.FEEDBACK_CFG || {};
  var ENDPOINT = CFG.endpoint || "/api/feedback";
  var SOURCE = CFG.source || location.hostname || "web";

  var css = `
  .fbw-btn{position:fixed;right:16px;bottom:16px;z-index:99998;background:#0b5bd3;color:#fff;
    border:none;border-radius:22px;padding:10px 16px;font:600 13px system-ui,sans-serif;cursor:pointer;
    box-shadow:0 2px 10px rgba(0,0,0,.35)}
  .fbw-btn:hover{background:#0a4fb8}
  .fbw-ov{position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.5);display:flex;
    align-items:center;justify-content:center}
  .fbw-card{background:#fff;color:#16202b;width:min(440px,92vw);border-radius:12px;padding:18px 18px 16px;
    box-shadow:0 8px 40px rgba(0,0,0,.4);font:14px system-ui,sans-serif}
  .fbw-card h3{margin:0 0 4px;font-size:16px}
  .fbw-card p.sub{margin:0 0 12px;color:#5a6b7b;font-size:12px}
  .fbw-row{display:flex;gap:8px;margin-bottom:10px}
  .fbw-row label{flex:1;display:flex;align-items:center;gap:5px;border:1px solid #d4dbe2;border-radius:8px;
    padding:7px 9px;cursor:pointer;font-size:13px}
  .fbw-row label.on{border-color:#0b5bd3;background:#eef4ff}
  .fbw-card input[type=text],.fbw-card textarea{width:100%;box-sizing:border-box;border:1px solid #d4dbe2;
    border-radius:8px;padding:8px 10px;font:14px system-ui;margin-bottom:10px}
  .fbw-card textarea{min-height:96px;resize:vertical}
  .fbw-acts{display:flex;justify-content:flex-end;gap:8px;align-items:center}
  .fbw-acts .grow{flex:1}
  .fbw-acts button{border:none;border-radius:8px;padding:8px 14px;font:600 13px system-ui;cursor:pointer}
  .fbw-cancel{background:#eceff3;color:#16202b}
  .fbw-send{background:#0b5bd3;color:#fff}
  .fbw-send:disabled{opacity:.6;cursor:default}
  .fbw-msg{font-size:12px;margin-top:8px}
  .fbw-msg.ok{color:#1a7f37}.fbw-msg.err{color:#c9302c}
  .fbw-msg a{color:#0b5bd3}`;
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement("button");
  btn.className = "fbw-btn"; btn.type = "button"; btn.textContent = "💬 Feedback";
  btn.onclick = open; document.body.appendChild(btn);

  var ov = null, kind = "bug";
  function close() { if (ov) { ov.remove(); ov = null; } }
  function open() {
    kind = "bug";
    ov = document.createElement("div"); ov.className = "fbw-ov";
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ov.innerHTML =
      '<div class="fbw-card" role="dialog" aria-modal="true">' +
        '<h3>Send feedback</h3>' +
        '<p class="sub">Files an issue on the C4 repo. No personal info needed.</p>' +
        '<div class="fbw-row" id="fbwKind">' +
          '<label data-k="bug" class="on"><input type="radio" name="fbwk" checked>🐛 Bug</label>' +
          '<label data-k="feature"><input type="radio" name="fbwk">✨ Feature</label>' +
          '<label data-k="idea"><input type="radio" name="fbwk">💡 Idea</label>' +
        '</div>' +
        '<input type="text" id="fbwTitle" maxlength="160" placeholder="Short summary (required)">' +
        '<textarea id="fbwBody" maxlength="8000" placeholder="What happened / what would you like? Steps, what you expected, etc."></textarea>' +
        '<div class="fbw-acts">' +
          '<div class="grow"><div class="fbw-msg" id="fbwMsg"></div></div>' +
          '<button class="fbw-cancel" id="fbwCancel">Cancel</button>' +
          '<button class="fbw-send" id="fbwSend">Send</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);
    ov.querySelectorAll("#fbwKind label").forEach(function (l) {
      l.addEventListener("click", function () {
        kind = l.getAttribute("data-k");
        ov.querySelectorAll("#fbwKind label").forEach(function (x) { x.classList.remove("on"); });
        l.classList.add("on");
      });
    });
    ov.querySelector("#fbwCancel").onclick = close;
    ov.querySelector("#fbwSend").onclick = send;
    ov.querySelector("#fbwTitle").focus();
  }

  function send() {
    var title = ov.querySelector("#fbwTitle").value.trim();
    var bodyEl = ov.querySelector("#fbwBody");
    var msg = ov.querySelector("#fbwMsg");
    var sendBtn = ov.querySelector("#fbwSend");
    msg.className = "fbw-msg";
    if (!title) { msg.className = "fbw-msg err"; msg.textContent = "Please add a short summary."; return; }
    sendBtn.disabled = true; sendBtn.textContent = "Sending…";
    var payload = {
      type: kind, title: title, body: bodyEl.value, source: SOURCE,
      context: { app: SOURCE, page: location.hash || location.pathname, url: location.href,
        viewport: innerWidth + "x" + innerHeight },
    };
    fetch(ENDPOINT, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.ok && res.d.ok) {
          var link = res.d.url ? ' <a href="' + res.d.url + '" target="_blank" rel="noopener">#' + res.d.number + "</a>" : "";
          msg.className = "fbw-msg ok"; msg.innerHTML = "Thanks! Filed" + link + ".";
          bodyEl.value = ""; ov.querySelector("#fbwTitle").value = "";
          sendBtn.textContent = "Sent ✓";
          setTimeout(close, 1800);
        } else {
          msg.className = "fbw-msg err"; msg.textContent = (res.d && res.d.error) || "Could not send — try again.";
          sendBtn.disabled = false; sendBtn.textContent = "Send";
        }
      })
      .catch(function (e) {
        msg.className = "fbw-msg err"; msg.textContent = "Network error — " + e;
        sendBtn.disabled = false; sendBtn.textContent = "Send";
      });
  }
})();
