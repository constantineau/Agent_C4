/* COURSE CHANGES aboard (#406) — in the Time-to-Mark detail, under the mark controls.

   The race committee shortens the course, skips a mark or changes a side, and there is no
   internet. The crew picks the mark, then a change; the second tap saves it (a stray tap costs
   nothing). The Pi keeps it as a new course revision (POST /api/course/edit) and the next
   re-route uses it. GET /api/course/revision says which revision is aboard.

   Pure parts (html, state, the request body) are tested under node: pi/console/test_coursedit.js.
   `attach` wires the clicks in the browser; dashboard.js calls it once and puts `html()` in the
   detail. */
(function (root) {
  "use strict";

  const ARM_MS = 5000;               // the second tap must come within this
  const SIDE = { port: "port", starboard: "stbd", none: "no side", gate: "gate" };
  const ACTIONS = {
    skip_to: "Skip to",
    finish_at: "Finish here",
    "side:port": "Port",
    "side:starboard": "Stbd",
    undo: "Undo change",
  };

  const S = { rev: null, pos: null, armed: null, note: "", busy: false };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[<>&"]/g, (c) =>
      ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  }
  function hhmm(epochS) {
    const d = new Date(epochS * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }

  /* "Course rev 4 · changed aboard 14:32" / "Course rev 1 · from the Lab" */
  function revText(r) {
    if (!r || !r.number) return "Course rev ?";
    const when = r.at ? " " + hhmm(r.at) : "";
    return "Course rev " + r.number + (r.origin === "boat" ? " · changed aboard" + when
      : r.origin === "lab" ? " · from the Lab" + when : "");
  }

  /* the mark picked, else the first one that isn't an island */
  function picked(rev) {
    const ms = (rev && rev.marks) || [];
    return ms.find((m) => m.pos === S.pos) || ms.find((m) => m.index != null) || ms[0] || null;
  }

  /* which changes the picked mark allows */
  function allowed(action, m, rev) {
    if (action === "undo") return !!(rev && (rev.edits || []).length);
    if (!m) return false;
    if (action === "skip_to" || action === "finish_at") return m.index != null;
    if (action.indexOf("side:") === 0) return m.type !== "gate" && m.rounding !== action.slice(5);
    return false;
  }

  /* the POST body for a change on mark `m` */
  function request(action, m, device) {
    if (action === "undo") return { op: "undo", device: device || null };
    const b = { op: action.indexOf("side:") === 0 ? "rounding" : action, pos: m.pos, name: m.name,
      device: device || null };
    if (b.op === "rounding") b.side = action.slice(5);
    return b;
  }

  function html(nowMs) {
    const rev = S.rev;
    if (!rev || rev.available === false) return "";
    const now = nowMs || Date.now();
    const m = picked(rev);
    const armed = S.armed && now < S.armed.until ? S.armed.action : null;
    const opts = (rev.marks || []).map((x) =>
      '<option value="' + x.pos + '"' + (m && x.pos === m.pos ? " selected" : "") + ">" +
      (x.index != null ? x.index + ". " : "") + esc(x.name) +
      (x.type === "island" ? " (island, " : " (") + (SIDE[x.rounding] || esc(x.rounding)) + ")" +
      "</option>").join("");
    const btns = Object.keys(ACTIONS).map((a) =>
      '<button class="cw-btn ce-btn' + (armed === a ? " armed" : "") + '" data-ce="' + a + '"' +
      (S.busy || !allowed(a, m, rev) ? " disabled" : "") + ">" +
      (armed === a ? "Tap again" : ACTIONS[a]) + "</button>").join("");
    return '<div class="rc-title" style="margin-top:10px">' + esc(revText(rev.revision)) +
      (rev.shortened ? " · shortened" : "") + "</div>" +
      '<div class="cw-btns ce-ctl"><select class="ce-mark" aria-label="Mark to change">' + opts +
      "</select>" + btns + "</div>" +
      (S.note ? '<div class="dc-foot">' + esc(S.note) + "</div>" : "");
  }

  /* a tap on `action` → null (armed, nothing sent) or the body to POST */
  function tap(action, nowMs) {
    const now = nowMs || Date.now();
    const rev = S.rev, m = picked(rev);
    if (!allowed(action, m, rev)) return null;
    if (!S.armed || S.armed.action !== action || now >= S.armed.until) {
      S.armed = { action: action, until: now + ARM_MS };
      S.note = "";
      return null;
    }
    S.armed = null;
    return request(action, m, null);
  }

  /* the Pi's answer → the note under the buttons */
  function answered(res) {
    S.note = !res ? "No answer from the boat. Try again."
      : res.ok ? (res.note || "Saved.") : (res.note || "Not changed.");
    return S.note;
  }

  function setRevision(r) { if (r) S.rev = r; }
  function pick(pos) { S.pos = pos; S.armed = null; }
  function reset() { S.rev = null; S.pos = null; S.armed = null; S.note = ""; S.busy = false; }

  /* browser wiring: io = {post(path, body, ms), get(path, ms), redraw(), after(), device()} */
  function attach(io) {
    const doc = root.document;
    if (!doc) return;
    async function refresh() {
      const r = await io.get("/course/revision", 8000);
      if (r) setRevision(r);
      io.redraw();
    }
    doc.addEventListener("click", async (e) => {
      const b = e.target.closest && e.target.closest(".ce-btn[data-ce]");
      if (!b || b.disabled) return;
      const body = tap(b.dataset.ce, Date.now());
      io.redraw();
      if (!body) {
        setTimeout(io.redraw, ARM_MS + 100);       // the armed label times out
        return;
      }
      if (io.device) body.device = io.device() || null;
      S.busy = true;
      io.redraw();
      const res = await io.post("/course/edit", body, 8000);
      S.busy = false;
      answered(res);
      if (res && res.ok && io.after) await io.after();
      await refresh();
    });
    doc.addEventListener("change", (e) => {
      const sel = e.target.closest && e.target.closest(".ce-mark");
      if (!sel) return;
      pick(parseInt(sel.value, 10));
      io.redraw();
    });
    api.refresh = refresh;
  }

  const api = { html: html, tap: tap, answered: answered, request: request, allowed: allowed,
    revText: revText, setRevision: setRevision, pick: pick, reset: reset, attach: attach,
    refresh: null, ARM_MS: ARM_MS, state: S };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.C4CourseEdit = api;
})(typeof window !== "undefined" ? window : this);
