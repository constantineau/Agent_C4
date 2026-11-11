/* RACE LOG button — the words and the default pick, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4RecLog) and required by
   pi/console/test_reclog.js under node.

   Frozen at the gun: starting a RACE session starts racing (the boat refuses shore homework until
   the crew taps Race over, #241). So starting the
   log asks Race or Practice (one extra tap, two big buttons), pre-selecting Race when Deploy
   homework (a course, fleet or playbook) is aboard, and always sends the kind it got. */
(function (root) {
  "use strict";

  const PICK_MS = 8000;        // the Race / Practice choice stays open this long

  /* which kind to pre-select: Race when shore homework is aboard, else Practice */
  function preselect(session) {
    return session && session.homework_aboard ? "race" : "practice";
  }

  /* the LOG button's label: idle, choosing, running (with the kind), or the end confirmation */
  function label(active, opts) {
    const o = opts || {};
    if (active && o.confirming) return "TAP AGAIN TO END";
    if (active) {
      const kind = String(active.kind || "").toLowerCase() === "race" ? "RACE" : "PRACTICE";
      const min = Math.max(0, Math.round(((o.nowS || 0) - (active.start_ts || 0)) / 60));
      return "LOG · " + kind + " · " + min + "m";
    }
    if (o.picking) return "LOG · PICK ONE";
    return "LOG";
  }

  /* the /session/start body: always the kind the crew tapped, and the boat picked (#320): a
     library boat's id, or a typed name for an "Other boat". No pick → the engine uses the last
     boat used. */
  function startBody(kind, boat) {
    if (kind !== "race" && kind !== "practice") throw new Error("kind must be race or practice");
    const b = { kind: kind };
    if (boat && boat.boat_id) b.boat_id = boat.boat_id;
    else if (boat && boat.name) b.boat_name = String(boat.name).trim().slice(0, 40);
    return b;
  }

  /* BOAT PICKER (#320). The picker starts on the last boat used (the engine's current boat); one
     tap changes it. The LOG button shows the boat name big, so a wrong boat is obvious. */
  function defaultBoat(session) {
    const c = ((session || {}).boats || {}).current;
    return c && (c.boat_id || c.name) ? { boat_id: c.boat_id || null, name: c.name || c.boat_id } : null;
  }
  /* the list to pick from: the library aboard, then "Other boat" */
  function boatChoices(session) {
    const rows = ((session || {}).boats || {}).boats || [];
    return rows.map(function (b) { return { boat_id: b.boat_id, name: b.name || b.boat_id }; });
  }
  /* the boat name on the LOG button: the running session's, else the one the picker will use */
  function boatName(active, pick) {
    if (active) return active.boat_name || active.boat_id || "";
    return pick ? (pick.name || pick.boat_id || "") : "";
  }
  /* one plain line when the picked boat isn't the deployed playbook's (never blocks recording) */
  function playbookNote(pick, session) {
    const bs = (session || {}).boats || {};
    if (!(session || {}).homework_aboard || !bs.playbook_boat || !pick) return null;
    if (pick.boat_id && pick.boat_id === bs.playbook_boat) return null;
    return "Playbook is for " + (bs.playbook_boat_name || bs.playbook_boat) + ".";
  }
  /* the boat's polar, in one line, when it has none (an Other boat, or a library boat without) */
  const NO_POLAR = "No polar for this boat.";
  function polarNote(pick, session) {
    if (!pick) return null;
    if (!pick.boat_id) return NO_POLAR;
    const bs = (session || {}).boats || {};
    const c = bs.current || {};
    if (c.boat_id === pick.boat_id) return c.has_polar === false ? NO_POLAR : null;
    const row = (bs.boats || []).filter(function (b) { return b.boat_id === pick.boat_id; })[0];
    return row && row.has_polar === false ? NO_POLAR : null;
  }

  /* RACE OVER button (#241). Racing (no shore loading) is its own state, apart from LOG: only
     this button ends it. Hidden unless racing. The boat may ask "Race over?" after a long stop;
     it is still the crew's two taps that end it. */
  const OVER_ARM_MS = 5000;    // the second tap must come within this
  function raceOverLabel(race, opts) {
    const o = opts || {};
    // hidden when not racing, and when the boat can't say which race (since unknown): a tap
    // without the race start could end a newer race
    if (!race || !race.racing || race.since == null) return null;
    if (o.armed) return "Tap again to end";
    return race.suggest_over ? "Race over?" : "Race over";
  }
  /* the /race/over body: the race start this button shows, so a newer race is never ended */
  function raceOverBody(race) {
    return { since: race && race.since != null ? race.since : null };
  }

  const api = { preselect: preselect, label: label, startBody: startBody, PICK_MS: PICK_MS,
    defaultBoat: defaultBoat, boatChoices: boatChoices, boatName: boatName,
    playbookNote: playbookNote, polarNote: polarNote, OTHER_MS: 60000,
    raceOverLabel: raceOverLabel, raceOverBody: raceOverBody, OVER_ARM_MS: OVER_ARM_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.C4RecLog = api;
})(typeof window !== "undefined" ? window : this);
