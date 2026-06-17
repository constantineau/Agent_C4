"""Shared team login for the C4 Performance Lab (browser app, used anywhere).

One shared team password → a stateless signed bearer token, mirroring the boat's web auth
(vps/agent/app/auth.py). Per-user identity/roles are deferred (decision 2026-06-17). The static
shell is public; only the /api/* data routes are gated.
"""
import hashlib
import hmac
import os
import time

PASSWORD = os.environ.get("LAB_PASSWORD", "lab-dev")
SECRET = os.environ.get("LAB_AUTH_SECRET") or ("lab-" + hashlib.sha256(PASSWORD.encode()).hexdigest())
TTL_HOURS = float(os.environ.get("LAB_AUTH_TTL_HOURS", "720"))   # 30 days
ENABLED = os.environ.get("LAB_AUTH_ENABLED", "true").strip().lower() != "false"
OPEN_PATHS = {"/api/health", "/api/auth"}


def check_password(pw) -> bool:
    return bool(pw) and hmac.compare_digest(str(pw), PASSWORD)


def issue_token() -> str:
    exp = str(int(time.time() + TTL_HOURS * 3600))
    sig = hmac.new(SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(tok) -> bool:
    if not ENABLED:
        return True
    if not tok or "." not in tok:
        return False
    exp, sig = tok.rsplit(".", 1)
    good = hmac.new(SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return time.time() < float(exp)
    except ValueError:
        return False
