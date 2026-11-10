"""Which boat this install is: read from the environment, never assumed (#307).

Every service that labels or reads a boat's data (`BOAT_ID` on the Pi and the VPS agent,
`REROUTE_BOAT` on the Orin, the crew password `BOAT_PASSWORD`) gets it from here. There is no
default: a missing setting stops the service at start with one plain line saying which setting
to add, instead of quietly writing another boat's data under sr33's name.
"""
import os


def missing_message(name):
    """The one line a service prints when `name` is not set."""
    return f"{name} is not set. Add {name}=... to this install's .env (or unit env). Not starting."


def require(name="BOAT_ID", environ=None):
    """The value of setting `name` (stripped). Missing or blank → exit; Python prints the one
    plain line (the SystemExit message) to stderr, which is the service's log."""
    env = os.environ if environ is None else environ
    value = (env.get(name) or "").strip()
    if not value:
        raise SystemExit(missing_message(name))
    return value
