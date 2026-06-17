# RRS 41 / Bayview Mackinac NOR — Compliance Review

**Status:** review complete 2026-06-17 against the **2026 Bayview Mackinac Race Notice of Race**
(`2026NOR V6 111925 Approved_Post`, approved by MRA/BYC Board 2025-11-19) and the **Racing Rules of
Sailing 2025–2028**. This is an engineering compliance read, **not a rules ruling** — confirm the
interpretation in writing with the Organizing Authority / Race Committee before relying on it, and
re-check the Sailing Instructions (published ~July 1, 2026), which can change the NOR and take
precedence (NOR §2.1(c)).

---

## 1. The governing text

**RRS 41 (Outside Help)** — a boat shall not receive help from any outside source, except:
- (a) help for a crew member who is ill, injured or in danger;
- (b) after a collision, help from the crew of the other vessel to get clear;
- (c) help in the form of information freely available to all boats;
- (d) unsolicited information from a disinterested source, which may be another boat in the same race.

**2026 NOR §2.1(d) — changes RRS 41(c)** (verbatim):

> "Help in the form of information available to all boats is permitted even if that information is
> only accessible at a cost; **however, such 'at cost' help shall not include private forecast or
> tactical advice or information customized for a particular boat or group of boats while
> underway.** This changes RRS 41(c)."

Other relevant NOR points: the race is governed by RRS 2025–2028 + the NOR + SIs (§2.1); ORC rating
applies (§2.1(i)); World Sailing **Appendix WP** (Racing Around Waypoints) applies (§2.1(m)); the
**Finish** and the **Cove Island Gate** are defined by the boat's **position transponder** plus a
**photo of the primary navigation GPS** at crossing (§2.1(f), §8). The NOR carries **no blanket ban
on carrying or using onboard electronics/communications** — the operative constraint is RRS 41 as
narrowed by §2.1(d): the issue is the *kind of help received from an outside source while underway*,
not the presence of gear.

---

## 2. What this means for the SR33 AI Navigator

The deciding question under RRS 41 is **"is the boat receiving help from an outside source while
racing?"** — and §2.1(d) makes explicit that **private/customized forecast or tactical advice
"while underway" is not permitted**, even if you pay for it. The line is *customized-for-this-boat
from outside* vs. *the boat's own equipment* vs. *info equally available to everyone*.

Mapping our features onto that line:

| Feature | In-race status (cloud agent) | Why |
|---|---|---|
| Passive telemetry collection + archive | ✅ Allowed | No help is *received*; pure logging. |
| Live instrument readout / `/conditions` strip | ✅ Allowed | The boat's **own** instruments — not an outside source. |
| AIS collision guard, depth / stale-data **safety** alerts | ✅ Defensible | Safety (RRS 41(a) spirit); own AIS + onboard-style CPA. Keep it strictly safety, not tactics. |
| Public forecast **verbatim** (e.g. a GRIB/NWS product available to all) | ✅ Allowed | "Information available to all boats" (§2.1(d) first clause). |
| **Tactics** (favored side, shifts, leverage) | ❌ **Prohibited in-race** | Tactical advice customized for this boat from a shore source while underway. |
| **Weather routing / isochrone optimal route** | ❌ **Prohibited in-race** | Customized routing/forecast advice for this boat while underway. |
| **Polar coaching, sail crossover/peel calls, fatigue rotation, "% of polar"** | ❌ **Prohibited in-race** | Performance/tactical advice customized for this boat from outside. |
| Navigator basics (mark bearing/distance/ETA, laylines) | ⚠️ Gray | Plain navigation off the boat's own GPS is normally fine; but delivered *from a shore source* it is still "outside." Treat as gated; safest to compute onboard. |
| Debriefs / summaries **after** racing, practice, deliveries | ✅ Allowed | Not "while racing." This is the system's unrestricted use. |

**Bottom line:** with the **current cloud architecture** (Pi → Starlink → shore VPS → Claude API →
crew), every *performance/tactical/routing/coaching* answer is **customized advice generated off the
boat and delivered while underway** — squarely what RRS 41 + NOR §2.1(d) prohibit during a race.
Safety, own-instrument readout, verbatim all-boats info, and all non-racing use remain fine.

---

## 3. Two compliant operating modes

**A. Cloud agent in a race → "compliant race mode" (restrict the advice channel).**
During racing, the shore agent must deliver only: (1) safety/collision information, (2) the boat's
own instrument data, and (3) information equally available to all boats *verbatim* (no customized
routing/forecast). It must **decline** tactics, routing, polar/sail coaching, and fatigue calls
while racing. The Phase-5 **Race/Practice toggle** is the natural control, but today it only gates
those panels in the **UI** — the chat/LLM can still answer a tactical question. **Recommended
follow-up:** enforce the gate **server-side** (in race mode the agent refuses customized
tactical/forecast/routing/coaching and answers only safety + own-data + all-boats-info), so
compliance doesn't depend on the crew avoiding a button. This is a concrete, buildable change.

**B. All-onboard for full in-race coaching (no shore loop).**
Run the agent **on the boat** (on the Pi) using a **local model with no external API call**, fed
only by the boat's own sensors. The boat's own equipment and onboard computation are **not an
"outside source"** — this is the same category as widely-used onboard nav/routing software
(Expedition, etc.). This is the only way to keep the full tactical/routing/coaching value **legally
in a race**. Note: an "onboard" agent that still calls the **cloud Claude API** is **not** clearly
compliant — the customized advice would originate from an outside source over the link. Local
inference is the compliant form; it's a larger build (model + hardware) and is deferred.

---

## 4. Action items

1. **Before any race use, confirm with the OA/RC in writing** how they read §2.1(d) for an
   onboard/AI navigator, and re-check the **Sailing Instructions** when published — they can change
   the NOR and take precedence.
2. **Default to safe:** in a race with the cloud agent, restrict to passive collection + safety +
   own-data; use full coaching only for **practice, deliveries, and debriefs**.
3. **Recommended code follow-up:** make Race mode a **server-side** compliance gate (see §3A), not
   just a UI gate, with an explicit "racing — outside tactical help withheld (RRS 41)" response.
4. **Longer term, if full in-race coaching is wanted:** pursue the **all-onboard, local-model**
   path (§3B).
5. Carriage of the **position transponder** and the **primary-nav-GPS finish/gate photos** are
   required by the NOR (§2.1(f), §8) — orthogonal to RRS 41, but note the boat must carry/operate
   them.

---

*Sources: World Sailing RRS 2025–2028 Rule 41 (racingrulesofsailing.org); 2026 Bayview Mackinac
Race Notice of Race, `2026NOR V6 111925 Approved_Post` (bycmack.com). Re-verify against the
as-published Sailing Instructions before the race.*
