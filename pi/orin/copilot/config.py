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
ENGINE_TIMEOUT = float(os.environ.get("ENGINE_TIMEOUT", "12"))

# Where the local LLM lives (Tier 2, this Orin). OpenAI-compatible /v1.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct-q4_K_M")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# Cap on the bounded tool-calling loop — how many rounds of engine-fact requests the LLM
# may make before we force it to conclude. Keeps a confused 7B from looping forever.
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "4"))

# A frozen playbook bundle (Lab-2 output) loaded onboard pre-start. Optional today; when
# present the copilot leans on its pre-authored variants as a strong prior — onboard it may
# depart from them (legal). Absent → the copilot reasons from live engine facts only and says so.
PLAYBOOK_PATH = os.environ.get("PLAYBOOK_PATH", "").strip()

# The active route the engine should compute against (the loaded homework course).
DEFAULT_ROUTE = os.environ.get("COPILOT_ROUTE", "").strip() or None

# Service
COPILOT_PORT = int(os.environ.get("COPILOT_PORT", "8300"))

# If false, never call the LLM — always return the deterministic brief. Useful when the
# Orin is busy/serving something else, or to prove the engine-only baseline.
USE_LLM = _b("COPILOT_USE_LLM", True)

# Proactive auto-coach: a background timer that runs the narration engine on a cadence, so the
# copilot VOLUNTEERS coaching (and the time-driven callouts — rounding prep, branch triggers — fire
# on the clock), not only when the iPad polls. Read the held result from GET /coach. The LLM only
# phrases NEW callouts (most ticks are deterministic + cheap), following USE_LLM.
COACH_ENABLED = _b("COPILOT_COACH", True)
COACH_INTERVAL_S = float(os.environ.get("COPILOT_COACH_INTERVAL_S", "30"))
