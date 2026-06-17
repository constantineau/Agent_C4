"""Phase 7 — server-side shared-password auth.

The crew shares ONE boat password (project decision). Until now the web gate was a client-side
stub (accept any non-empty entry) — bypassable, so the agent's data/chat endpoints were
effectively open. This adds a real server check:

  POST /auth {password}  →  verify against BOAT_PASSWORD  →  issue a signed, time-limited token.

The token is stateless (no DB / session store): `"<exp>.<hmac>"` where hmac = HMAC-SHA256 over the
expiry using AUTH_SECRET. Verification recomputes the HMAC (constant-time) and checks expiry — so
any agent replica validates it and a restart doesn't log everyone out. It's a bearer token behind a
single shared secret on a TLS endpoint (Phase 7 TLS), not per-user identity — appropriate for a
boat iPad, not a public multi-tenant app.

Transport: the web app sends `Authorization: Bearer <token>` on REST and `?token=<token>` on the
WebSocket (browsers can't set headers on a WS handshake from JS).

Config:
  BOAT_PASSWORD  shared crew password (dev default 'sr33-dev'; MUST be set in prod .env).
  AUTH_SECRET    HMAC signing secret; defaults to a value derived from BOAT_PASSWORD so tokens
                 rotate automatically when the password changes. Set explicitly in prod.
  AUTH_TTL_HOURS token lifetime (default 720h / 30 days — crew shouldn't re-auth mid-passage).
  AUTH_ENABLED   set 'false' to disable the check entirely (open bench); default enabled.
"""
import hashlib
import hmac
import os
import time

BOAT_PASSWORD = os.environ.get("BOAT_PASSWORD", "sr33-dev")
AUTH_TTL_HOURS = float(os.environ.get("AUTH_TTL_HOURS", "720"))
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").strip().lower() not in ("0", "false", "no")

# Derive a default signing secret from the password (so changing the password invalidates old
# tokens); allow an explicit override for defence in depth.
_SECRET = (os.environ.get("AUTH_SECRET", "").strip()
           or "sr33-auth:" + hashlib.sha256(BOAT_PASSWORD.encode()).hexdigest())
_SECRET_B = _SECRET.encode()

# Endpoints reachable without a token (health probe for monitoring; the login itself).
OPEN_PATHS = {"/health", "/auth"}


def _sign(exp: int) -> str:
    return hmac.new(_SECRET_B, str(exp).encode(), hashlib.sha256).hexdigest()


def issue_token(ttl_hours: float = None) -> str:
    exp = int(time.time() + (ttl_hours if ttl_hours is not None else AUTH_TTL_HOURS) * 3600)
    return f"{exp}.{_sign(exp)}"


def verify_token(token: str | None) -> bool:
    if not AUTH_ENABLED:
        return True
    if not token:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _sign(exp))


def check_password(password: str | None) -> bool:
    """Constant-time shared-password check."""
    if not password:
        return False
    return hmac.compare_digest(password.encode(), BOAT_PASSWORD.encode())
