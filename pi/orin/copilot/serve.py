"""Run the copilot API on the boat cable interface and loopback only, for allow-listed callers.

    cd pi/orin && python3 -m copilot.serve

The copilot used to listen on every interface (:8300 on 0.0.0.0). Its one client on the boat is
the Pi's race console, whose nginx proxies the iPad's /copilot calls over the direct Pi↔Orin cable
(COPILOT_UPSTREAM=http://10.10.10.2:8300/, so they arrive from the Pi's cable address 10.10.10.1).
The Lab's Deploy push reaches it the same way, through the Pi console. Loopback is for tools run on
the Orin itself (bench_copilot, smoke_api). Same shape as pi/orin/reroute/service.py.

  COPILOT_BIND        addresses to listen on      (default 10.10.10.2,127.0.0.1)
  COPILOT_ALLOW_FROM  caller addresses allowed     (default 10.10.10.1,127.0.0.1)
  COPILOT_PORT        port                         (default 8300)

Each address is bound with IP_FREEBIND, so the service starts even when the cable is unplugged at
boot and answers on it once the link comes up. Anyone else gets 403.
"""
import asyncio
import json
import socket

from . import config

IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)        # Linux


def _addrs(raw):
    return [a.strip() for a in (raw or "").split(",") if a.strip()]


class AllowList:
    """ASGI wrapper: only callers whose address is in `allow` reach the app; the rest get 403."""

    def __init__(self, app, allow):
        self.app, self.allow = app, set(allow)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = (scope.get("client") or ("",))[0]
            if host not in self.allow:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"detail": "Not allowed."}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def bind_sockets(addrs, port):
    """One listening TCP socket per address. FREEBIND lets an address that isn't up yet (the cable
    unplugged) bind now and work once the interface appears."""
    socks = []
    for a in addrs:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
        except OSError:
            pass
        s.bind((a, port))
        s.listen(128)
        s.setblocking(False)
        socks.append(s)
    return socks


def main():
    import uvicorn

    from .app import app
    socks = bind_sockets(_addrs(config.COPILOT_BIND), config.COPILOT_PORT)
    guarded = AllowList(app, _addrs(config.COPILOT_ALLOW_FROM))
    # proxy_headers off: the allow-list judges the real peer address, never an X-Forwarded-For
    server = uvicorn.Server(uvicorn.Config(guarded, lifespan="on", log_level="info",
                                           proxy_headers=False))
    print(f"[copilot] listening on {config.COPILOT_BIND} :{config.COPILOT_PORT}; "
          f"callers {config.COPILOT_ALLOW_FROM}", flush=True)
    asyncio.run(server.serve(sockets=socks))


if __name__ == "__main__":
    main()
