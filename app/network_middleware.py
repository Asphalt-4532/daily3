"""ASGI middleware enforcing the trusted-hostname allowlist managed via
Settings -> Network (see app/services/network.py for the "why").

Deliberately reads app.services.network.get_active_hostnames() on every
request rather than once at startup - a Starlette app's middleware stack is
built once when the FastAPI app object is constructed, so anything baked in
at that point (like the built-in TrustedHostMiddleware, or SessionMiddleware's
https_only flag) can't be changed without restarting the process. Querying
the database per request is what makes this one actually live-editable from
the web UI. For a low-traffic homelab app, one extra local SQLite query per
request is not a meaningful cost.

Registered in app.main - added AFTER SessionMiddleware so it ends up
OUTERMOST (Starlette applies middleware in reverse registration order),
running before anything else touches the request, including sessions.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.services import network as network_service

# Always accepted regardless of what's configured, so a manager can never
# lock themselves out of local access by mis-configuring the allowlist -
# "testserver" is what Starlette's own TestClient sends as its Host header,
# so the test suite exercises the same code path a browser would.
_ALWAYS_ALLOWED = {"localhost", "127.0.0.1", "testserver"}


def _host_without_port(host_header: str) -> str:
    host_header = (host_header or "").strip().lower()
    if host_header.startswith("["):  # IPv6 literal, e.g. [::1]:8000
        end = host_header.find("]")
        return host_header[1:end] if end != -1 else host_header
    if ":" in host_header:
        return host_header.rsplit(":", 1)[0]
    return host_header


class TrustedHostFromDBMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        allowed = network_service.get_active_hostnames()
        if allowed:  # empty allowlist = unrestricted, today's default behavior
            host = _host_without_port(request.headers.get("host", ""))
            if host not in allowed and host not in _ALWAYS_ALLOWED:
                return PlainTextResponse("Invalid host header", status_code=400)
        return await call_next(request)
