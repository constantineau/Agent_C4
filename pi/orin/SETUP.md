# Orin Nano bring-up runbook (Phase 9.4, Tier 2)

> **SUPERSEDED (historical record).** This is the original **MLC-on-:9000 / JetPack-6.2** plan;
> the unit was actually brought up on **JetPack 7.2 + from-source Ollama on :11434** — the
> as-built runbook is **`DEPLOYMENT.md`**. §9's `sr33-orin-llm.service` was removed from the
> repo. Kept because the flashing/Super-mode/thermal steps and the research remain useful.


Get a **fresh Jetson Orin Nano 8GB (Super)** from box → flashed → Super mode → a benchmarked
local LLM serving an **OpenAI-compatible API** that the SR33 copilot service will later call.
This milestone is **runtime/model bring-up only** — no SR33-specific wiring yet (that's the next
9.4 increment). See `docs/ONBOARD_ENGINE_SCOPING.md` §3 for the design and `README.md` here for the
architecture/contract.

> **Why this is its own box.** Locked decision: the **Pi 4 runs the deterministic engine**
> (`pi/engine`, Tier 1) and the **Orin is dedicated to the LLM** (Tier 2). They talk over
> boat-local Wi-Fi. Nothing here touches the Pi.

> **Verified vs. confirm-on-unit.** Commands marked ✅ are confirmed against NVIDIA / dusty-nv
> docs (June 2026). Commands marked ⚠️ are the documented *form* but a model-id string or a flag
> should be confirmed on the live unit (`--help` / the jetson-ai-lab model table) before you trust
> it — I can't reach the Orin from the dev VM, so nothing here has been run on real hardware yet.

---

## 0. Hardware checklist (before you start)

- [ ] **Jetson Orin Nano 8GB Developer Kit** (the "Super" is a JetPack-6.2 software mode on the same
      board — no special SKU needed; the 8GB is required, 4GB can't hold a 7B at INT4).
- [ ] **Active cooling** — the Super (25 W / MAXN_SUPER) tok/s numbers assume the fan + heatsink are
      working. A hot, enclosed nav box **will** thermal-throttle. Budget a fan; we monitor with
      `tegrastats` (§10).
- [ ] **NVMe SSD** (strongly recommended) — model weights + containers are tens of GB; the MLC image
      alone is large. Flash to NVMe, or flash to SD then move docker's data-root to NVMe.
- [ ] **Adequate PSU** — use a supply that can hold 25 W mode (the official barrel-jack/USB-C PD per
      the dev-kit guide; an underpowered supply silently drops you out of Super mode).
- [ ] Ethernet (for the bring-up; the boat-local Wi-Fi/SSID comes later) + a host on the same LAN to
      run the API smoke test from.

---

## 1. Flash JetPack 6.2 (L4T R36.4.x) ✅

JetPack **6.2** is what introduced Super mode for the Orin Nano. Two routes:

- **SDK Manager** (from an x86 Ubuntu host over USB-C recovery) — preferred; it also flashes the
  **bootloader/QSPI firmware**, which is the part that matters (see the warning below).
- **SD-card image** — fine, but a from-an-older-JetPack board may still need the QSPI/firmware
  update before Super mode is selectable.

> ⚠️ **Firmware/QSPI prerequisite for Super mode.** Super mode requires the updated Orin Nano
> bootloader firmware. A unit that shipped on JetPack 5.x / 6.0 must have its **QSPI firmware
> updated to the 6.2 line** or `nvpmodel -m 2` won't exist / won't hold. The clean path is to flash
> the whole thing with SDK Manager (firmware + rootfs together). Confirm after boot:
> ```bash
> sudo apt-cache show nvidia-jetpack | grep Version   # expect 6.2.x
> cat /etc/nv_tegra_release                            # expect R36 (release), REVISION: 4.x
> ```

After first boot, finish setup and update:
```bash
sudo apt update && sudo apt -y full-upgrade
sudo reboot
```

---

## 2. Enable Super mode (MAXN_SUPER) ✅

On the **8GB Orin Nano**, Super mode is power-model **2**:
```bash
sudo nvpmodel -m 2          # MAXN_SUPER (8GB Orin Nano)  ✅ from NVIDIA JetPack 6.2 blog
sudo jetson_clocks          # pin clocks to max (recommended for steady benchmarking)
```
Verify it took and survives reboot:
```bash
sudo nvpmodel -q            # should report: NV Power Mode: MAXN_SUPER  (mode 2)
sudo reboot
sudo nvpmodel -q            # confirm it's still MAXN_SUPER after reboot
```
`nvpmodel` persists the mode in `/etc/nvpmodel.conf`'s default, so it sticks across reboots.
`jetson_clocks` does **not** persist — re-run it after boot (the systemd unit in §9 handles this for
the appliance).

---

## 3. Container runtime (NVIDIA docker) ✅

JetPack ships Docker + the NVIDIA container runtime. Make `nvidia` the **default** runtime so
`jetson-containers` (and our launch script) get the GPU without extra flags:
```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER            # log out/in so non-root docker works
# set the default runtime to nvidia:
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "default-runtime": "nvidia",
  "runtimes": { "nvidia": { "path": "nvidia-container-runtime", "runtimeArgs": [] } }
}
JSON
sudo systemctl restart docker
docker info | grep -i 'default runtime'  # expect: Default Runtime: nvidia
```

> ⚠️ **If you put docker on NVMe** (recommended): also set `"data-root": "/mnt/nvme/docker"` in that
> same `daemon.json` (with the SSD mounted at `/mnt/nvme`) **before** pulling any images, or you'll
> fill the SD card.

---

## 4. Install jetson-containers ✅

dusty-nv's `jetson-containers` is the official harness behind NVIDIA's own benchmark numbers — it
picks the right image tag for your L4T version via `autotag`.
```bash
git clone https://github.com/dusty-nv/jetson-containers
bash jetson-containers/install.sh        # adds the `jetson-containers` + `autotag` commands to PATH
```

---

## 5. Run MLC + benchmark Qwen2.5-7B at INT4 ✅⚠️

**Use MLC, not llama.cpp/Ollama, for 7–8B.** ✅ A known CUDA memory-allocator regression in the
JetPack R36.4.x line broke >1B models under llama.cpp (which Ollama wraps); NVIDIA's published 7–8B
numbers (Qwen2.5-7B = **21.75 tok/s**, Llama-3.1-8B = 19.1 tok/s) come from **MLC + INT4**. So MLC is
our runtime for the 7B. (A 3–4B model under Ollama may be fine — that's a fallback to A/B in §6, not
the primary path.)

Launch the MLC container (downloads/builds on first run — can take a while):
```bash
jetson-containers run $(autotag mlc)      # ✅ documented launch form
```

NVIDIA's reproducible benchmark sweep (downloads + builds + times a set of models):
```bash
bash jetson-containers/packages/llm/mlc/benchmarks.sh   # ✅ NVIDIA's own benchmark script
```

To benchmark **just Qwen2.5-7B** and see prefill/decode tok/s, use `pi/orin/bench.sh` (wraps the
MLC `benchmark.sh` for one model id — see that script). Expected ballpark on Super mode:
prefill ~285–300 tok/s, **decode ~21–22 tok/s**, ~4.8 GB resident. A ~100-token answer ≈ 5 s.

> ⚠️ **Confirm the exact MLC model id on-unit.** The pre-quantized MLC builds live on HuggingFace
> (dusty-nv hosts `*-q4f16_ft-MLC` repos) and the exact repo string can drift. Before scripting it,
> list what the model table / `sudonim` knows and copy the verbatim id — see `pi/orin/models.md` for
> the candidate matrix and the lookup commands.

---

## 6. A/B a faster small model (latency floor) ⚠️

The 7B is the capability pick, but on a boat **latency** matters. A/B it against a 3–4B so you know
the tradeoff before committing (numbers from NVIDIA's table; confirm on-unit):

| Model | decode tok/s (Super) | Mem | Note |
|---|---|---|---|
| **Qwen2.5-7B** | ~21.8 | ~4.8 GB | primary — best capability + function-calling at 8GB |
| Phi-3.5 3.8B | ~38 | ~2.3 GB | fast, big headroom |
| Llama-3.2-3B | ~43 | ~2 GB | fastest |

`pi/orin/bench.sh <model-id>` runs each and prints tok/s; record the results in `pi/orin/models.md`.
Decision rule for this milestone: **go with the 7B unless a ~100-token tactical answer exceeds ~6 s
under sustained thermal load**, in which case fall back to a 3–4B for the latency-critical path.

---

## 7. Start the OpenAI-compatible API server ⚠️

The whole point of "a clean local inference API" is that the future copilot service talks to a
stable, boring contract: **OpenAI `/v1/chat/completions`**. MLC serves this via dusty-nv's `sudonim`
launcher inside the MLC container. The documented form (confirm exact flags with `sudonim serve
--help` on-unit):
```bash
jetson-containers run -d --name sr33-orin-llm --restart unless-stopped \
  -p 9000:9000 $(autotag mlc) \
  sudonim serve \
    --model <Qwen2.5-7B q4f16_ft MLC id from §5>  \
    --quantization q4f16_ft \
    --host 0.0.0.0 --port 9000
```
`pi/orin/serve.sh` wraps exactly this with a `MODEL`/`PORT` variable so you only edit one line.
Once up, the endpoint is `http://<orin-ip>:9000/v1`.

---

## 8. Smoke-test the API ✅

From the Orin (or any host on the LAN), confirm a real grounded answer + measure latency:
```bash
python3 pi/orin/smoke_api.py --base-url http://<orin-ip>:9000/v1
```
It sends a short "narrate these engine facts" prompt (the shape the copilot will use), prints the
answer, and reports first-token + total latency and the effective tok/s. **Exit test for this
milestone:** a coherent answer, fully offline from any cloud, at usable latency (~5 s for ~100
tokens). No SR33 tool-calling yet — that's the next increment.

---

## 9. Autostart as an appliance (systemd) ⚠️

For the boat, the server should come up on power-on with clocks pinned. Install the unit:
```bash
sudo mkdir -p /opt/sr33 && sudo cp -r <repo>/pi /opt/sr33/pi
sudo cp /opt/sr33/pi/systemd/sr33-orin-llm.service /etc/systemd/system/
sudo mkdir -p /etc/sr33 && sudoedit /etc/sr33/orin.env   # set MODEL, PORT (see serve.sh)
sudo systemctl daemon-reload
sudo systemctl enable --now sr33-orin-llm
journalctl -u sr33-orin-llm -f
```
The unit re-applies `nvpmodel -m 2` + `jetson_clocks` on boot (via `ExecStartPre`) then runs
`serve.sh`. Confirm after a reboot that `/health` (or `/v1/models`) answers.

---

## 10. Thermal + power monitoring ✅

Watch for throttle while benchmarking and on the water:
```bash
sudo tegrastats          # GPU%/EMC/temps/power — watch GR3D_FREQ throttling + thermal zones
```
If temps climb toward throttle and tok/s sags vs §5, improve airflow before blaming the model.

---

## Known issues / gotchas

- ✅ **llama.cpp / Ollama on R36.4.x + >1B models** — CUDA-alloc regression; use MLC for the 7B.
  (Ollama is fine for a quick 3B A/B, not for the primary path.)
- **8GB is tight.** Qwen2.5-7B INT4 ≈ 4.8 GB leaves room for KV-cache + the desktop; keep context
  modest and don't also run a VLM. If you hit OOM, drop to a 3–4B.
- **`jetson_clocks` doesn't persist** — the systemd unit re-applies it; if you run manually, re-run
  after every reboot or your benchmark numbers will be low.
- **First MLC run is slow** (download + compile of the model). Subsequent runs are fast (cached in
  the container's model dir — keep it on NVMe).
- **Model-id strings drift.** Always copy the verbatim MLC repo id from the live unit; don't trust a
  hard-coded string in a script across jetson-containers updates.

## Sources
- [NVIDIA: JetPack 6.2 brings Super Mode to Orin Nano / NX](https://developer.nvidia.com/blog/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules/)
- [NVIDIA Jetson AI Lab — benchmarks](https://www.jetson-ai-lab.com/archive/benchmarks.html)
- [dusty-nv/jetson-containers — MLC package](https://github.com/dusty-nv/jetson-containers/tree/master/packages/llm/mlc)
