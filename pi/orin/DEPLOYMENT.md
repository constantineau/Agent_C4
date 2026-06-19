# Agent_C4 — Jetson Orin Nano Deployment (as-built)

Authoritative current-state record of the onboard inference appliance, as deployed
2026-06-19. For the historical, blow-by-blow bring-up narrative (including the
bricking incident and the CPU-fallback debugging) see `BRINGUP_STATE.md`. The
original `SETUP.md` describes the *superseded* JetPack-6.2 / MLC plan and is kept
only for reference.

> **TL;DR:** Headless Jetson Orin Nano Super (8GB) running a from-source build of
> Ollama with a `cuda_v13`@sm_87 GPU backend, serving `qwen2.5:7b-instruct-q4_K_M`
> 100% on the iGPU at ~12 tok/s on `127.0.0.1:11434`. Everything is systemd-managed
> and survives reboots with zero manual steps. Reachable from anywhere over Tailscale.

---

## 1. Hardware & platform

| Item | Value |
|---|---|
| Board | Jetson Orin Nano **Super** Developer Kit, 8 GB (no eMMC/SSD — boots from microSD) |
| Hostname | `agent-c4` |
| OS / L4T | **JetPack 7.2 / L4T R39.2.0**, kernel `6.8.12-tegra`, `aarch64` |
| CUDA | **13.2** (toolkit at `/usr/local/cuda-13.2`, `nvcc` V13.2.78) |
| GPU | Orin iGPU, **compute capability 8.7 (sm_87)**, ~7.3 GiB unified |
| Power mode | **MAXN_SUPER** (`nvpmodel` mode 2) — persists across reboot |

### ⚡ Power — IMPORTANT
- Power the board from the **DC barrel jack** using the **included 19 V supply**
  (barrel jack accepts 7–20 V; connector **5.5 mm OD × 2.5 mm ID, center-positive**).
- **Do NOT power from USB-C** for GPU work: USB-C is limited to 5 V / 15 W and
  **overcurrent-throttles the instant the GPU spikes** under MAXN_SUPER. This was
  diagnosed live — barrel jack = stable, USB-C = "system throttled due to overcurrent".
- **Boat deploy:** the 12 V→onboard converter feeding this box must supply ~19 V
  (7–20 V) sized for the GPU current transient, **not a 5 V rail**.

### ☠️ Never do this
Do **not** run the old MAXN_SUPER "enablement" hack
(`perl` edit of `/etc/nv_boot_control.conf` + `dpkg-reconfigure nvidia-l4t-bootloader`).
It reflashes the bootloader and **bricked this unit once** (boot loop). MAXN_SUPER is
already exposed natively after the clean JetPack 7.2 install; switch power modes only
via `nvpmodel` (safe, reversible).

---

## 2. Inference runtime (why it's a from-source build)

Stock Ollama on this box ran **100% on CPU (~5 tok/s)**. Root cause: Ollama's
prebuilt CUDA backends (`cuda_v12`, `cuda_v13`) are compiled for a set of archs
that **omits sm_87** (the Orin's embedded Ampere), so it logs
`skipping CUDA device — compute capability not in compiled architectures` and falls
back to CPU. (It is *not* a cudart/driver/power problem.)

**Fix:** rebuild Ollama's `cuda_v13` GGML backend from source with sm_87.

- Source: `~/ollama-src` (git tag **v0.30.10**, matching the stock binary's ABI).
- Build env: `CUDA_HOME=/usr/local/cuda-13.2`, Go 1.24.5 (`/usr/local/go`).
- Configure: `cmake -B build . -DOLLAMA_LLAMA_BACKENDS=cuda_v13 -DCMAKE_CUDA_ARCHITECTURES=87 -DGGML_CPU_ALL_VARIANTS=OFF -DGGML_NATIVE=ON`
  (`GGML_CPU_ALL_VARIANTS=OFF` avoids a GCC-13 `sme` build failure on the armv9 variant).
- Build: `cmake --build build --parallel $(nproc)` → produces the `ollama` Go binary +
  `build/lib/ollama/cuda_v13/` with **`libggml-cuda.so` (60 MB, sm_87)** and CUDA-13.2 runtime libs.

**Installed to standard locations** (so the systemd service finds them via default search):
- Binary → `/usr/local/bin/ollama` (identical SHA to `~/ollama-src/ollama`).
- Libs → `/usr/local/lib/ollama/` — the `cuda_v13/libggml-cuda.so` there is the
  **sm_87** build (same SHA as the from-source one). `cuda_v12` is still the stock
  one and is correctly *skipped* at runtime.

Verify GPU is engaged (in the service log):
```
skipping CUDA device … device=Orin cc=870 … libDirs=[… cuda_v12]   <- expected (stock v12, no sm_87)
inference compute … library=CUDA compute=8.7 name=CUDA0 description=Orin libdirs=ollama,cuda_v13 type=iGPU
```

---

## 3. Model & performance

- Model: **`qwen2.5:7b-instruct-q4_K_M`** (~4.6 GB), stored in `/home/agent-c4/.ollama/models`.
- Loads **100% GPU** (`ollama ps` → `PROCESSOR = 100% GPU`).
- Throughput vs power mode (all 100% GPU): 15W ≈ 7.1 · 25W ≈ 9.7 · **MAXN_SUPER ≈ 12** tok/s.
- **This is a memory-bandwidth ceiling**, not a config issue: 7B-q4 (~4.5 GB) decode on
  the Orin's ~102 GB/s LPDDR5 tops out ~12–15 tok/s. `OLLAMA_FLASH_ATTENTION` + q8 KV
  made it *slightly worse* — not used.
- **Decision (user, 2026-06-19): keep the 7B for quality over speed.** ~8 s / 100 tokens
  is acceptable for the nav-copilot. To hit ~20+ tok/s you'd drop to `qwen2.5:3b` (not done).

---

## 4. systemd services (the "permanent" layer)

### `/etc/systemd/system/ollama.service`
Runs as **`agent-c4`** (not the stock `ollama` user — that user can't see the model in
`/home/agent-c4`). Uses the GPU binary, binds localhost, 2048 ctx.
```ini
[Unit]
Description=Ollama (from-source GPU build, cuda_v13 sm_87)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=agent-c4
Group=agent-c4
Restart=always
RestartSec=3
Environment="HOME=/home/agent-c4"
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_CONTEXT_LENGTH=2048"
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/jetson-clocks.service`
`nvpmodel` mode **persists** across reboot, but `jetson_clocks` (which pins clocks to the
max allowed by that mode) does **not** — so it's re-applied each boot.
```ini
[Unit]
Description=Pin Jetson max clocks at boot
After=nvpmodel.service
Wants=nvpmodel.service

[Service]
Type=oneshot
ExecStart=/usr/bin/jetson_clocks
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Both are `enable`d. **Reboot-verified 2026-06-19:** box returns in ~28 s, both services
active, MAXN_SUPER restored, model serves 100% GPU @ ~11.5 tok/s — **no manual steps**.

Headless: the system boots to `multi-user.target` (no desktop) to free ~2 GB for the
model. A monitor will show only a text `agent-c4 login:` on tty1 — that's expected, not a fault.

---

## 5. Networking — Tailscale infrastructure

The Orin has **no public IP** and moves between home / boat networks, so access is over a
**Tailscale tailnet** (account `constantineau@`), which NAT-traverses and survives reboots.

| Node | Tailscale IP | Role |
|---|---|---|
| `agent-c4` (the Orin) | **100.70.110.72** | inference appliance; Tailscale **SSH server** enabled (`tailscale up --ssh`) |
| `agent-c4-devvm` (dev VM) | 100.67.228.63 | where Claude / development runs |

- Tailscale IPs are **stable per node** — `100.70.110.72` won't change on reboot/DHCP.
- **Connect from any tailnet device:**
  ```
  ssh agent-c4@100.70.110.72
  ```
  Auth is via Tailscale SSH (tailnet identity) — no SSH key/password prompt. Use the plain
  `ssh` client, **not** `tailscale ssh` (its wrapper rejects `-o` flags); both nodes are in
  TUN mode so direct-IP ssh works.
- **Ollama is bound to `127.0.0.1`** (not exposed on the tailnet). To reach the API from
  another machine, either SSH in and curl localhost, or change `OLLAMA_HOST` to
  `0.0.0.0:11434` (then it's reachable at `100.70.110.72:11434` over the tailnet only).
- **Re-joining if a node was reset:** `sudo tailscale up --ssh` prints a
  `https://login.tailscale.com/a/…` URL to approve in a browser. For unattended re-join,
  mint a reusable **auth key** (Tailscale admin → Settings → Keys) and
  `sudo tailscale up --authkey <key>`.
- Revoke access anytime from the Tailscale admin console (remove the device).

---

## 6. Access & sudo

- **Login user:** `agent-c4`. Credentials are stored off-repo on the dev VM at
  `~/.agent_c4_orin.env` (mode 600).
- **Passwordless sudo is scoped to reboot/shutdown only** — `/etc/sudoers.d/010-agent-c4-reboot`:
  ```
  agent-c4 ALL=(ALL) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff, /usr/sbin/reboot, /usr/sbin/poweroff, /usr/sbin/shutdown
  ```
  All other `sudo` still requires the password (`echo "$PW" | sudo -S -p '' <cmd>`).
  (A stray world-readable `NOPASSWD: ALL` file was found and removed on 2026-06-19; don't
  recreate one unless full passwordless root is genuinely wanted.)

---

## 7. Operations runbook

```bash
# --- status ---
systemctl is-active ollama jetson-clocks
nvpmodel -q                       # expect: MAXN_SUPER / 2
ollama ps                         # expect: 100% GPU when a model is loaded

# --- logs ---
sudo journalctl -u ollama -b --no-pager | grep -iE 'compute=8.7|skipping CUDA'

# --- restart / reboot ---
sudo systemctl restart ollama
sudo systemctl reboot             # passwordless

# --- smoke test (clean tok/s via API, no TUI spinner) ---
curl -s http://127.0.0.1:11434/api/generate -d '{
  "model":"qwen2.5:7b-instruct-q4_K_M",
  "prompt":"Explain tacking in one sentence.","stream":false,
  "options":{"num_predict":80}}' \
| python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["response"]);ec,ed=d["eval_count"],d["eval_duration"];print(f"{ec/(ed/1e9):.2f} tok/s")'
```

## 8. Known constraints / gotchas
- USB-C power → overcurrent under GPU load. Use the 19 V barrel jack. (§1)
- GPU only works because of the from-source **sm_87** `cuda_v13` backend; a stock Ollama
  upgrade would overwrite `/usr/local/{bin,lib}/ollama` and **regress to CPU**. Pin the
  version / re-install the sm_87 backend after any Ollama update. (§2)
- `ollama --version` reports `0.0.0` (the from-source Go build didn't stamp a version) —
  cosmetic; it is the v0.30.10 source.
- ~12 tok/s is the hardware bandwidth ceiling for a 7B model here. (§3)
