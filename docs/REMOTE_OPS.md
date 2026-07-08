# Remote operations — the boat lives in Sarnia, we work from anywhere

As of 2026-07-08 the Pi + Orin stay aboard long-term (Sarnia, ON), powered by the owner, on
Starlink. This is the runbook for working on them remotely, what's already hardened, and the
rules that keep a remote session from stranding the boat.

## Access paths (in order)

| Path | How | Notes |
|---|---|---|
| Pi via Tailscale | `ssh sr33-pi@100.79.180.102` | Tailscale SSH; the tailnet may ask for a **browser check approval** (~12 h validity) — the link prints in the terminal; whoever owns the tailnet clicks it |
| Orin via Tailscale | `ssh agent-c4@100.70.110.72` | same |
| **Cross-hop (the backup)** | from the Orin: `ssh sr33-pi@10.10.10.1` · from the Pi: `ssh agent-c4@10.10.10.2` | over the direct Pi↔Orin ethernet, ed25519 keys exchanged + verified 2026-07-08 — if ONE box drops off the tailnet, reach it through the other |
| Last resort | the owner power-cycles | everything below is built to come back on its own |

## Already hardened (verified 2026-07-08)

- **Tailscale key expiry: DISABLED on both nodes** — the classic silent killer for unattended
  boxes (default ~180-day node keys) does not apply.
- **Power-cycle survival**: every `compose.pi.yml` service is `restart: unless-stopped`;
  `docker` + `tailscaled` enabled at boot on the Pi; `docker`, `tailscaled`, `ollama`,
  `sr33-orin-copilot` enabled on the Orin (reboot-verified earlier — turnkey appliance).
- **Storage self-limits**: the archiver's retention prune (14 d outside race sessions,
  fail-safe) keeps the SD card from filling; the archive DB is WAL + `synchronous=FULL`
  (power-loss-safe writes).
- **No WAN dependency in-race**: the iPad → console → copilot → engine loop rides the
  boat-local ethernet/Wi-Fi only; Starlink outages cost remote ACCESS, never race function.

## Rules for a remote session (how not to strand the boat)

1. **Never touch `wlan0`, `tailscale0`, or the default route** on either box. The Pi↔Orin
   ethernet (`eth0` / `enP8p1s0`, 10.10.10.0/24) is also load-bearing — don't re-address it.
2. **Deploys are always**: `cd ~/Agent_C4 && git fetch && git reset --hard origin/main &&
   docker compose -f compose.pi.yml up -d --build <changed services>`. Never `compose down`
   remotely (a failed rebuild after a down = dead stack); `up -d --build` replaces in place.
3. **One box at a time.** Update + verify the Pi, then the Orin — never both in one shot, so
   the untouched box stays a working hop.
4. **Verify before you leave**: `curl localhost:8200/health` (engine), `:8091/dashboard/`
   (console), `:8300/health` on the Orin (copilot + its engine reachability), and
   `tailscale status` on whichever box you touched.
5. **Kernel/OS updates**: don't. Nothing here needs them; an unbootable kernel 3,000 km away
   needs hands. (Unattended-upgrades is not configured on either box — leave it that way.)
6. **The Orin bootloader is fragile**: never re-run the MAXN_SUPER perl/dpkg bootloader hack
   (it bricked the unit once — `pi/orin/DEPLOYMENT.md` §8). Runtime `nvpmodel` is safe.

## Known residual risks (accepted / owner-assisted)

- **Tailnet check-mode approval**: remote SSH may need a browser click from the tailnet owner.
  If that's you and you're reachable, it's a 10-second step. To remove it entirely, relax the
  `check` action for these nodes in the Tailscale admin ACLs (admin console — a tailnet
  setting, not a boat setting).
- **SD card mortality (Pi)**: the retention prune limits wear, but SD cards die eventually.
  Race data leaves the boat via the session backfill, so a dead card costs configuration, not
  history. Recovery = the owner flashes a spare card per `pi/SETUP.md` (a fresh clone + one
  `compose up` rebuilds everything; the engine store re-learns its kv on the next fleet/
  playbook load).
- **Starlink power posture**: if the owner powers Starlink down between visits, the boxes are
  simply unreachable until it's back — nothing breaks; the stack doesn't need the WAN.
- **No automatic reboot-on-lost-WAN watchdog — deliberately.** A connectivity watchdog that
  reboots would loop at anchor with Starlink off. Restart policies + enabled units cover every
  recoverable failure that doesn't need hands.

## Quick health check (paste-able)

```bash
ssh sr33-pi@100.79.180.102 'tailscale status | head -3; docker ps --format "{{.Names}} {{.Status}}"; curl -s localhost:8200/health; df -h / | tail -1'
ssh agent-c4@100.70.110.72 'tailscale status | head -3; systemctl is-active ollama sr33-orin-copilot; curl -s localhost:8300/health | head -c 200; tegrastats --interval 1000 | head -1'
```
