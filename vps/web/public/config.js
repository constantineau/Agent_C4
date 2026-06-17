/* Runtime config — overridden per deployment by the web server (no build step).
   Cloud (vps/web) serves this file verbatim → ONBOARD=false → full app (auth + LLM chat +
   the race/practice gate). The Pi race console (pi/console) overrides /config.js to set
   ONBOARD=true → the app talks ONLY to the onboard engine over boat-local Wi-Fi, with no
   auth, no chat, and every panel available (the boat's own computer is legal in-race). */
window.SR33_ONBOARD = false;
