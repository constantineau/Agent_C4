# Onboard Pi setup runbook (Raspberry Pi 4 + PICAN-M)

How to bring up the boat computer from a blank SD card to the full onboard stack (Signal K +
archiver + onboard engine + race console + uplink). The Pi is a **deploy target, not a dev host**
— code is edited on the VPS and shipped here (`deploy/push_pi.sh`) or pulled via git.

Target hardware: **Raspberry Pi 4** + **PICAN-M** CAN HAT (MCP2515, 16 MHz crystal, INT on GPIO25),
microSD. Power note in §6.

---

## 1. Flash the OS — Raspberry Pi OS Lite (64-bit), Trixie/Debian 13

In **Raspberry Pi Imager**: *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*
(`…-raspios-trixie-arm64-lite`). Lite = headless (no desktop); 64-bit = our Docker images + the
bench↔boat portability rule (the only difference is `CAN_IFACE`).

Before writing, open **⚙ Edit Settings** and pre-configure (so it boots headless):
- **Hostname:** `sr33-pi`  (matches `deploy/push_pi.sh`'s default `PI_HOST`)
- **Enable SSH** (use your public key)
- **Wi-Fi** SSID + password (your dock/home net for setup; the boat-local SSID later) + country
- **Locale / timezone**

Write, boot the Pi, then `ssh sr33-pi`.

## 2. Base system

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y can-utils
curl -fsSL https://get.docker.com | sudo sh          # Docker engine + compose plugin
sudo usermod -aG docker "$USER"                       # then log out/in (or: newgrp docker)
```

## 3. Remote admin over Starlink CGNAT — Tailscale

Starlink is CGNAT (no inbound), so admin is via Tailscale; the boat only ever pushes out.
```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up        # approve the node in your tailnet; gives the Pi a stable 100.x address
```

## 4. PICAN-M → `can0`

The PICAN-M is an MCP2515 SPI CAN controller. Enable SPI + the CAN overlay once in firmware,
then let the `sr33-can0` unit raise the interface at the NMEA-2000 bitrate on every boot.

**a. Edit `/boot/firmware/config.txt`** (Trixie path) and add at the end:
```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```
(16 MHz = the PICAN-M crystal; INT on GPIO25. If a future board uses an 8 MHz crystal, set
`oscillator=8000000` — a wrong value = no CAN. Confirm against your board if unsure.)

**b. Reboot**, then confirm the kernel created the interface:
```bash
sudo reboot
# after reboot:
ip -details link show can0      # should list the can0 device (state DOWN until the unit raises it)
dmesg | grep -i mcp251          # should show the mcp251x driver bound
```

**c. Install the boot bring-up unit** (raises can0 at 250 kbit/s, before Docker):
```bash
sudo cp ~/Agent_C4/pi/systemd/sr33-can0.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sr33-can0
ip -details link show can0      # now: state UP, bitrate 250000
```

**d. Sanity-check live N2K traffic** (with the boat bus connected — see §6 power):
```bash
candump can0                    # should scroll raw N2K frames when instruments are on
```

## 5. Deploy the onboard stack

Get the repo and run `compose.pi.yml` with the real CAN interface and your cloud URL. **Do NOT**
use the `compose.pi.sample.yml` override — that injects fake N2K data and is bench-only.

```bash
git clone https://github.com/constantineau/Agent_C4 ~/Agent_C4      # or: deploy/push_pi.sh from the VPS
cd ~/Agent_C4

# point the uplink/archiver at your cloud ingestion (token from the cloud .env)
export CAN_IFACE=can0
export VPS_URL=https://<your-cloud-host>          # e.g. the TLS domain, or the Tailscale URL
export INGEST_TOKEN=<the ingestion token>
export BOAT_ID=sr33

docker compose -f compose.pi.yml up -d --build
docker compose -f compose.pi.yml ps
```

This starts: **signalk** (:3010, host net, reads can0), the auto **signalk-derived-data** install
(true wind / VMG), **archiver** (full-res local SQLite log), **engine** (:8200, onboard deterministic
modules), **console** (:8091, the iPad race app), and **uplink** (15-s aggregates → cloud, disk-backed
store-and-forward). Persisting env: put the four vars in `~/Agent_C4/.env` (compose reads it).

**Verify:**
```bash
curl -s localhost:8200/health                      # onboard engine
curl -s localhost:8200/conditions | head -c 200    # live channels off the real bus
curl -s localhost:8091/dashboard/ -o /dev/null -w '%{http_code}\n'   # console serves the dashboard
docker logs -f sr33-pi-uplink-1                     # 15-s aggregates POSTing to the cloud
docker logs -f sr33-pi-archiver-1                   # full-res rows landing in the local archive
```

## 6. Power & safety

- The PICAN-M can take 12 V boat power on its terminals to power the whole Pi. **During
  bench/dockside setup keep the 12 V terminals disconnected and power the Pi from USB-C only —
  never feed both at once.** On the boat, pick one supply.
- The CAN bus needs proper 120 Ω termination at both ends of the backbone (that's the N2K network's
  job, not the Pi's) — the PICAN-M can add a termination jumper if it sits at a bus end.

## 7. Race-day networking

In a race the iPad joins the **boat-local Wi-Fi** (no WAN) and opens
`http://sr33-pi:8091/dashboard/` (or the Pi's LAN IP) — the console talks only to the onboard
engine, never the cloud (RRS-41-clean). The optional Orin copilot (Tier 2) runs on a second box on
the same boat-local network; point the console's `COPILOT_UPSTREAM` at the Orin's boat-Wi-Fi address.

## 8. Updating later

From the VPS: `PI_HOST=sr33-pi deploy/push_pi.sh` (rsyncs `pi/` + restarts the uplink unit), or on
the Pi: `cd ~/Agent_C4 && git pull && docker compose -f compose.pi.yml up -d --build`.

## Follow-ups
- Record ~1 h of `candump -l can0` dockside and commit it under `pi/logs/` as the gold-standard
  `canplayer` replay fixture (replaces the canned `--sample-n2k-data` for a true day-length soak).
- Confirm Signal K decodes every device on the real bus (`http://sr33-pi:3010` data browser);
  calibrate Garmin sensors from the GPSMAP 943 (the Pi/Signal K cannot calibrate them — see
  `pi/sensors.md`).
