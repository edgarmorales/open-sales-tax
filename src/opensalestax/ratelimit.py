"""The process-wide rate limiter, and how it finds a caller's IP.

Module level on purpose. ``slowapi``'s ``@limiter.limit`` decorators bind at
**import** time, so the limiter has to exist before any route module is
imported -- earlier the limiter was built inside ``create_app()``, which meant
decorators were not an option and the only enforcement came from
``SlowAPIMiddleware``.

That mattered more than it sounds. The middleware finds the route handler by
walking ``app.routes`` and taking the first full match that has an
``.endpoint``. FastAPI 0.138 stopped flattening ``include_router`` into
individual ``APIRoute`` objects and now inserts one opaque ``_IncludedRouter``
instead, which matches but carries no ``.endpoint``. The middleware therefore
found no handler for any ``/v1`` path, skipped the check, and the entire API
served unlimited while ``/`` -- declared directly on the app, so still a real
``APIRoute`` -- kept working and kept the test suite green.

Decorators do not go through that discovery at all: they register a limit
against the endpoint function itself. That is why the fix is decorators rather
than teaching the middleware about ``_IncludedRouter``, which would only swap
one dependency on framework internals for another.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request as StarletteRequest

from opensalestax.settings import get_settings

__all__ = ["client_ip", "default_limit", "limiter"]


def client_ip_from_headers(request: StarletteRequest, trusted_proxy_hops: int = 0) -> str:
    """Return the real client IP, preferring proxy-injected headers.

    Cloudflare-fronted deployments see ``request.client.host`` as the CF edge
    IP, which rotates across requests and would split each user's quota across
    many edges. ``CF-Connecting-IP`` (Cloudflare-set, unspoofable from outside
    CF) carries the real client IP.

    Without that header we fall back to ``X-Forwarded-For``, and which END of
    it to trust depends on the proxy in front:

    * A proxy that **replaces** the header (Cloudflare) leaves exactly one
      trustworthy value, so the first hop is right. This is the default
      (``trusted_proxy_hops = 0``).
    * A proxy that **appends** (AWS ALB, nginx, Traefik) adds the peer it saw
      to the END. A caller who sends their own ``X-Forwarded-For: 1.2.3.4``
      gets ``1.2.3.4, <real client>``, so the first hop is attacker-chosen and
      every caller could pick their own rate-limit bucket. Set
      ``trusted_proxy_hops`` to the number of appending proxies (1 for a
      single ALB) and the IP is counted from the RIGHT instead -- the part a
      caller cannot forge, because each proxy appends after whatever it was
      given.

    Takes the hop count as an argument rather than reading settings, so it
    stays testable without a configured environment.
    """
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        hops = trusted_proxy_hops
        if hops:
            parts = [part.strip() for part in xff.split(",")]
            # Too few entries means the request did not traverse the proxies
            # we were told to expect. Rather than guess at a shallower index
            # -- which is exactly the value a caller would forge -- fall back
            # to the immediate peer.
            if len(parts) >= hops and parts[-hops]:
                return parts[-hops]
            return get_remote_address(request)
        first_hop = xff.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    return get_remote_address(request)


def client_ip(request: StarletteRequest) -> str:
    """Rate-limit bucket key for a request.

    Reads settings per call rather than closing over them, because the limiter
    is constructed at import time and settings are not necessarily configured
    yet. ``get_settings`` is a process singleton, so this is a dict lookup.
    """
    settings = get_settings()
    if not settings.trust_forwarded_for:
        return get_remote_address(request)
    return client_ip_from_headers(request, settings.trusted_proxy_hops)


def default_limit() -> str:
    """The configured limit, as slowapi's limit string.

    A callable rather than a string so it is read per request: the decorators
    are evaluated at import, long before ``OPENSALESTAX_RATE_LIMIT_PER_MINUTE``
    has necessarily been seen.
    """
    return f"{get_settings().rate_limit_per_minute}/minute"


limiter = Limiter(
    key_func=client_ip,
    # Belt and braces. The v1 routes carry explicit decorators; these cover
    # anything declared directly on the app (the root redirect) and anything
    # a contributor adds without a decorator. The middleware applies them --
    # which works for app-level routes even though FastAPI 0.138 hides
    # router-included ones behind _IncludedRouter.
    default_limits=[default_limit],
    # Surface X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset so
    # clients can pace themselves without probing for 429s.
    headers_enabled=True,
)
