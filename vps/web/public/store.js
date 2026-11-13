/* Browser storage for the iPad web app, under "c4.*" keys (#314).
   The keys used to start with the project's old name. A key that is not yet under the new name
   is read from the old one and copied over, so a deploy keeps the iPad's login, theme, mode, route
   and hoisted sail. The old key is left in place (a rollback still finds it); writes go to the new
   key only; a remove clears BOTH, so the old key can't bring back a token after a sign-out.
   Every access is best effort: private mode or a full quota reads as "nothing stored".
   Loaded before app.js, sail.js and plot.js. */
"use strict";
var C4Store = (function () {
  const NEW = "c4.";
  const OLD = "sr33.";   // the old prefix: read once to carry a value over, never written

  function area(kind) {
    try { return kind === "session" ? globalThis.sessionStorage : globalThis.localStorage; }
    catch (e) { return null; }
  }
  function get(kind, name) {
    const s = area(kind);
    if (!s) return null;
    try {
      const v = s.getItem(NEW + name);
      if (v !== null && v !== undefined) return v;
      const old = s.getItem(OLD + name);
      if (old === null || old === undefined) return null;
      try { s.setItem(NEW + name, old); } catch (e) { /* quota: still return it */ }
      return old;
    } catch (e) { return null; }
  }
  function set(kind, name, value) {
    const s = area(kind);
    if (!s) return;
    try { s.setItem(NEW + name, value); } catch (e) { /* private mode / quota */ }
  }
  function remove(kind, name) {
    const s = area(kind);
    if (!s) return;
    try { s.removeItem(NEW + name); } catch (e) { /* private mode */ }
    try { s.removeItem(OLD + name); } catch (e) { /* private mode */ }
  }
  function api(kind) {
    return { get: (n) => get(kind, n), set: (n, v) => set(kind, n, v), remove: (n) => remove(kind, n) };
  }
  return { local: api("local"), session: api("session") };
})();
