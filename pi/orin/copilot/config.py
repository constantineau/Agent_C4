"""Copilot configuration — all from env so the same code runs on the bench and the Orin.

On the boat this service runs ON THE ORIN, co-located with the LLM (so `LLM_BASE_URL`
points at localhost), and reaches the Pi-4 deterministic engine over boat-local Wi-Fi
(so `ENGINE_URL` is the Pi's address). On the bench everything is reachable from one host.

Reality check vs the older `pi/orin/` runbook: we serve the LLM with **Ollama on :11434**
(OpenAI `/v1`), not MLC on :9000. The copilot only sees the OpenAI contract, so the runtime
is swappable; the default base-url just reflects what actually runs.
"""
import os


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Where the deterministic engine lives (Tier 1, Pi 4). The engine does ALL the math.
ENGINE_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:8200").rstrip("/")
# Boat-local-first engine addressing (locked 2026-07-07): in a race the Pi<->Orin link is a direct
# ethernet cable (10.10.10.1 Pi / 10.10.10.2 Orin) — NEVER Tailscale. ENGINE_URL should point at
# the boat-local address; ENGINE_URL_FALLBACK (e.g. the Tailscale IP) keeps DEV working while the
# cable is down. The client health-probes the primary once and fails over transparently.
ENGINE_URL_FALLBACK = os.environ.get("ENGINE_URL_FALLBACK", "")
# The fallback is used only when its host is ON the boat: the Pi<->Orin cable subnet or loopback
# (#53). Anything else (a Tailscale IP, the VPS bench engine on sample data, a hostname) is ignored,
# so a dead cable in a race gives "engine unreachable", never advice from an off-boat engine.
# Shore-side dev that really wants a Tailscale fallback sets ENGINE_FALLBACK_OFFBOAT=1. Never on the boat.
ENGINE_BOAT_NETS = os.environ.get("ENGINE_BOAT_NETS", "10.10.10.0/24,127.0.0.0/8")
ENGINE_FALLBACK_OFFBOAT = _b("ENGINE_FALLBACK_OFFBOAT", False)


def engine_url_on_boat(url: str) -> bool:
    """True when `url`'s host is a literal IP inside ENGINE_BOAT_NETS (the cable or loopback)."""
    import ipaddress
    import urllib.parse
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                      # a hostname can resolve anywhere: not provably on the boat
    for net in ENGINE_BOAT_NETS.split(","):
        try:
            if net.strip() and ip in ipaddress.ip_network(net.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def engine_fallback_url() -> str:
    """The fallback engine address the client may use, or "" when none is allowed."""
    fb = (ENGINE_URL_FALLBACK or "").strip().rstrip("/")
    if fb and (ENGINE_FALLBACK_OFFBOAT or engine_url_on_boat(fb)):
        return fb
    return ""
ENGINE_TIMEOUT = float(os.environ.get("ENGINE_TIMEOUT", "12"))

# Where the local LLM lives (Tier 2, this Orin). OpenAI-compatible /v1.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct-q4_K_M")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# The model's context window in tokens. MUST equal what the server really runs: Ollama takes it from
# OLLAMA_CONTEXT_LENGTH (ollama.service, pi/orin/DEPLOYMENT.md) or a Modelfile `PARAMETER num_ctx`,
# and its OpenAI /v1 endpoint accepts no per-request num_ctx. Ollama does NOT error on an over-long
# prompt — it drops the middle and answers anyway, so the copilot's fallback never fires (#54). The
# client therefore refuses a request whose estimated size would not fit (llm.py), and the LoRA
# training max length uses the same number (training/matcher_lora/train_qlora.py --max-len).
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "6144"))
# Tokens kept free for the reply (the strategy JSON is ~150-400 tokens).
LLM_REPLY_RESERVE = int(os.environ.get("LLM_REPLY_RESERVE", "512"))
# Size estimate without a tokenizer on the box: Qwen2.5 runs ~3.6-4 chars/token on this
# English + JSON text; 3.5 errs toward refusing rather than truncating.
LLM_CHARS_PER_TOKEN = float(os.environ.get("LLM_CHARS_PER_TOKEN", "3.5"))
# Cap on the bounded tool-calling loop — how many rounds of engine-fact requests the LLM
# may make before we force it to conclude. Keeps a confused 7B from looping forever.
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "4"))

# A frozen playbook bundle (Lab-2 output) loaded onboard pre-start. Optional today; when
# present the copilot leans on its pre-authored variants as a strong prior — onboard it may
# depart from them (legal). Absent → the copilot reasons from live engine facts only and says so.
PLAYBOOK_PATH = os.environ.get("PLAYBOOK_PATH", "").strip()
# Who may POST /playbook/stage + /playbook/commit: the Pi engine's cable address only (it forwards the Lab's bundle
# under its race-log lock). Comma list; the bench sets the Pi's Tailscale IP.
PLAYBOOK_LOAD_FROM = frozenset(x.strip() for x in os.environ.get(
    "PLAYBOOK_LOAD_FROM", "10.10.10.1").split(",") if x.strip())
# A staged playbook must be committed within this many seconds of the stage's RECEIPT (Orin
# monotonic clock), checked again right before the file goes live. Keep it <= the Pi engine's
# COPILOT_COMMIT_TIMEOUT_S (4 s), so a write can only land while the Pi still holds its lock.
PLAYBOOK_COMMIT_TTL_S = float(os.environ.get("PLAYBOOK_COMMIT_TTL_S", "3"))

# The active route the engine should compute against (the loaded homework course).
DEFAULT_ROUTE = os.environ.get("COPILOT_ROUTE", "").strip() or None

# Service
COPILOT_PORT = int(os.environ.get("COPILOT_PORT", "8300"))
# Listen on the boat cable interface + loopback only, and answer only these callers (the Pi's end
# of the cable, loopback). See copilot/serve.py.
COPILOT_BIND = os.environ.get("COPILOT_BIND", "10.10.10.2,127.0.0.1")
COPILOT_ALLOW_FROM = os.environ.get("COPILOT_ALLOW_FROM", "10.10.10.1,127.0.0.1")
# BENCH override (added to the boat defaults above, never replacing them): the bench console
# (compose.pi.sample.yml) reaches the Orin over Tailscale, so the bench adds the Orin's Tailscale
# address to listen on and the bench host's Tailscale address as a caller. In
# /etc/sr33/copilot.env on a bench Orin:
#   COPILOT_BENCH_BIND=<orin-tailscale-ip>         (tailscale ip -4, on the Orin)
#   COPILOT_BENCH_ALLOW_FROM=<bench-host-tailscale-ip>   (tailscale ip -4, on the bench host)
# Leave both unset on the boat.
COPILOT_BENCH_BIND = os.environ.get("COPILOT_BENCH_BIND", "")
COPILOT_BENCH_ALLOW_FROM = os.environ.get("COPILOT_BENCH_ALLOW_FROM", "")

# If false, never call the LLM — always return the deterministic brief. Useful when the
# Orin is busy/serving something else, or to prove the engine-only baseline.
USE_LLM = _b("COPILOT_USE_LLM", True)

# Proactive auto-coach: a background timer that runs the narration engine on a cadence, so the
# copilot VOLUNTEERS coaching (and the time-driven callouts — rounding prep, branch triggers — fire
# on the clock), not only when the iPad polls. Read the held result from GET /coach. The LLM only
# phrases NEW callouts (most ticks are deterministic + cheap), following USE_LLM.
COACH_ENABLED = _b("COPILOT_COACH", True)
COACH_INTERVAL_S = float(os.environ.get("COPILOT_COACH_INTERVAL_S", "30"))

# Scheduled CREW BRIEFS (briefs.py) riding the same timer: the watch-handover brief at T-minus
# BRIEF_HANDOVER_T_MIN to a rotation, the hourly recap (only while a watch plan is active or a
# race-log session is open — no recap noise at the dock), the mark pre-brief inside
# BRIEF_MARK_T_MIN of the next mark (but not during the rounding itself — narrate owns that).
BRIEFS_ENABLED = _b("COPILOT_BRIEFS", True)
BRIEF_HANDOVER_T_MIN = float(os.environ.get("COPILOT_BRIEF_HANDOVER_T_MIN", "15"))
BRIEF_RECAP_S = float(os.environ.get("COPILOT_BRIEF_RECAP_S", "3600"))
BRIEF_MARK_T_MIN = float(os.environ.get("COPILOT_BRIEF_MARK_T_MIN", "20"))
BRIEF_MARK_MIN_T_MIN = float(os.environ.get("COPILOT_BRIEF_MARK_MIN_T_MIN", "4"))
