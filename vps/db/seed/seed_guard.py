"""Keep the seed scripts' fake data away from a real boat's record (#39).

The seed scripts post made-up telemetry and AIS. Written under a real boat id, that fake sailing
sits in the live record next to the real thing (the 2026-09-09 bench incident). So they default
to DEMO_BOAT and refuse anything that looks real unless the caller passes --allow-real:

- a boat id that isn't a demo id (DEMO_BOAT, or one starting with "demo-"), or
- an ingestion URL that isn't on this machine (prod, or the VPS seen from elsewhere).

A local URL is not proof of a dev database: on the VPS, localhost:8101 is the stack the boat
posts to. The boat id is what keeps the data apart there.
"""
from __future__ import annotations

from urllib.parse import urlparse

DEMO_BOAT = "demo-boat"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_demo_boat(boat_id: str) -> bool:
    return boat_id == DEMO_BOAT or boat_id.startswith("demo-")


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "") in _LOCAL_HOSTS


def refusal(boat_id: str, url: str, allow_real: bool) -> str | None:
    """Why this write is refused, or None when it may go ahead."""
    if allow_real:
        return None
    why = []
    if not is_demo_boat(boat_id):
        why.append(f"boat id {boat_id!r} is not a demo boat (use {DEMO_BOAT!r} or 'demo-...')")
    if not is_local_url(url):
        why.append(f"{url} is not on this machine (prod or a remote stack)")
    if not why:
        return None
    return ("refusing to write fake data: " + "; ".join(why)
            + ". Pass --allow-real only if you really mean to.")


def add_args(ap) -> None:
    """The --boat-id / --allow-real pair every seed script takes."""
    ap.add_argument("--boat-id", default=DEMO_BOAT,
                    help=f"boat to write under (default {DEMO_BOAT}; anything else needs --allow-real)")
    ap.add_argument("--allow-real", action="store_true",
                    help="write fake data to a real boat id or a non-local URL anyway")


def check(ap, args) -> None:
    """Exit with an error (argparse style) if the write is refused."""
    msg = refusal(args.boat_id, args.url, args.allow_real)
    if msg:
        ap.error(msg)
