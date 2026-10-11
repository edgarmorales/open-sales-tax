# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eric Osterberg and OpenSalesTax contributors
"""App-level security middleware: rate limiting + response headers.

These tests exist to lock the wiring in place. v0.54.x shipped a
broken rate limiter (slowapi was registered but ``SlowAPIMiddleware``
was never added to the app, so ``default_limits`` never enforced).
A regression that re-introduces that bug should fail
``test_rate_limit_fires_after_threshold`` immediately.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import opensalestax.settings as settings_module
from opensalestax.app import _client_ip_proxy_aware, create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Build a fresh app with a deliberately low per-IP rate limit."""
    monkeypatch.setenv(
        "OPENSALESTAX_DATABASE_URL",
        "postgresql+asyncpg://u:p@localhost:5432/db",
    )
    monkeypatch.setenv("OPENSALESTAX_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setattr(settings_module, "_settings", None)
    app = create_app()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    monkeypatch.setattr(settings_module, "_settings", None)


def test_response_carries_security_headers(client: TestClient) -> None:
    """Every response should carry the defense-in-depth headers."""
    response = client.get("/")
    assert response.status_code == 307
    headers = {k.lower(): v for k, v in response.headers.items()}
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=31536000" in headers["strict-transport-security"]
    assert "geolocation=()" in headers["permissions-policy"]


def test_rate_limit_fires_after_threshold(client: TestClient) -> None:
    """slowapi default_limits should return 429 once the per-IP cap is hit."""
    # The 3/minute limit means the first 3 succeed, the 4th must be denied.
    for _ in range(3):
        assert client.get("/").status_code == 307
    blocked = client.get("/")
    assert blocked.status_code == 429
    body = blocked.json()
    assert "Rate limit exceeded" in body["detail"]


def test_rate_limit_response_also_carries_security_headers(
    client: TestClient,
) -> None:
    """Defense-in-depth headers must apply to 429s, not just successful responses."""
    for _ in range(3):
        client.get("/")
    blocked = client.get("/")
    assert blocked.status_code == 429
    headers = {k.lower(): v for k, v in blocked.headers.items()}
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


def test_rate_limit_headers_present_on_success(client: TestClient) -> None:
    """X-RateLimit-* headers should appear on every response so clients can self-pace."""
    response = client.get("/")
    assert response.status_code == 307
    headers = {k.lower(): v for k, v in response.headers.items()}
    assert headers["x-ratelimit-limit"] == "3"
    # First request: 2 remaining out of 3.
    assert headers["x-ratelimit-remaining"] == "2"
    # Reset is a unix timestamp; just sanity-check it parses as a number.
    float(headers["x-ratelimit-reset"])


def test_rate_limit_headers_decrement_per_request(client: TestClient) -> None:
    """X-RateLimit-Remaining should count down across requests."""
    remaining = []
    for _ in range(3):
        r = client.get("/")
        remaining.append(int(r.headers["x-ratelimit-remaining"]))
    assert remaining == [2, 1, 0]


def _mock_request(headers: dict[str, str], client_host: str = "127.0.0.1"):
    req = MagicMock()
    req.headers = headers
    req.client = MagicMock(host=client_host)
    return req


def test_client_ip_proxy_aware_prefers_cf_connecting_ip() -> None:
    """CF-Connecting-IP wins over X-Forwarded-For and request.client.host."""
    req = _mock_request(
        headers={
            "CF-Connecting-IP": "203.0.113.5",
            "X-Forwarded-For": "198.51.100.10, 172.69.1.1",
        },
        client_host="172.69.1.1",
    )
    assert _client_ip_proxy_aware(req) == "203.0.113.5"


def test_client_ip_proxy_aware_falls_back_to_xff_first_hop() -> None:
    """Without CF-Connecting-IP, the first X-Forwarded-For hop is used."""
    req = _mock_request(
        headers={"X-Forwarded-For": "198.51.100.10, 172.69.1.1"},
        client_host="172.69.1.1",
    )
    assert _client_ip_proxy_aware(req) == "198.51.100.10"


def test_client_ip_proxy_aware_falls_back_to_remote_address() -> None:
    """Without either header, the immediate peer IP is used."""
    req = _mock_request(headers={}, client_host="10.32.161.42")
    assert _client_ip_proxy_aware(req) == "10.32.161.42"


def test_xff_first_hop_is_forgeable_behind_an_appending_proxy() -> None:
    """The bug this parameter exists for.

    An AWS ALB APPENDS the peer it saw, so a caller who sends their own
    X-Forwarded-For keeps the first hop under their control. With the
    default hop count we return the forged value -- which is why the
    default is unsafe behind an ALB and the parameter is needed.
    """
    req = _mock_request(
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.10"},
        client_host="10.0.0.1",
    )
    assert _client_ip_proxy_aware(req) == "1.2.3.4"


def test_one_trusted_hop_ignores_a_forged_first_hop() -> None:
    """With one ALB in front, the real client is the LAST entry."""
    req = _mock_request(
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.10"},
        client_host="10.0.0.1",
    )
    assert _client_ip_proxy_aware(req, trusted_proxy_hops=1) == "198.51.100.10"


def test_one_trusted_hop_with_no_forgery() -> None:
    """The ordinary case: the ALB appended the only entry there is."""
    req = _mock_request(
        headers={"X-Forwarded-For": "198.51.100.10"},
        client_host="10.0.0.1",
    )
    assert _client_ip_proxy_aware(req, trusted_proxy_hops=1) == "198.51.100.10"


def test_two_trusted_hops_counts_from_the_right() -> None:
    """CDN -> ALB: the client sits two from the right."""
    req = _mock_request(
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.10, 172.16.0.9"},
        client_host="10.0.0.1",
    )
    assert _client_ip_proxy_aware(req, trusted_proxy_hops=2) == "198.51.100.10"


def test_fewer_hops_than_configured_falls_back_to_peer() -> None:
    """A short header means the request skipped a proxy we expected.

    Indexing shallower would return exactly the entry a caller forges,
    so this falls back to the immediate peer instead.
    """
    req = _mock_request(
        headers={"X-Forwarded-For": "1.2.3.4"},
        client_host="10.32.161.42",
    )
    assert _client_ip_proxy_aware(req, trusted_proxy_hops=2) == "10.32.161.42"


def test_cf_connecting_ip_still_wins_over_hops() -> None:
    """Cloudflare deployments are unaffected by the hop count."""
    req = _mock_request(
        headers={
            "CF-Connecting-IP": "203.0.113.5",
            "X-Forwarded-For": "1.2.3.4, 198.51.100.10",
        },
        client_host="10.0.0.1",
    )
    assert _client_ip_proxy_aware(req, trusted_proxy_hops=1) == "203.0.113.5"


def test_client_ip_proxy_aware_ignores_blank_xff() -> None:
    """A leading-empty X-Forwarded-For falls through to remote address."""
    req = _mock_request(
        headers={"X-Forwarded-For": "  ,198.51.100.10"},
        client_host="10.32.161.42",
    )
    assert _client_ip_proxy_aware(req) == "10.32.161.42"


def test_rate_limit_keys_per_cf_connecting_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """When trust_forwarded_for=true, two callers with different CF-Connecting-IPs
    accumulate separate buckets even from the same TCP peer."""
    monkeypatch.setenv("OPENSALESTAX_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENSALESTAX_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("OPENSALESTAX_TRUST_FORWARDED_FOR", "true")
    monkeypatch.setattr(settings_module, "_settings", None)

    app = create_app()
    with TestClient(app, follow_redirects=False) as c:
        # Caller A burns its quota.
        for _ in range(2):
            assert c.get("/", headers={"CF-Connecting-IP": "203.0.113.5"}).status_code == 307
        assert c.get("/", headers={"CF-Connecting-IP": "203.0.113.5"}).status_code == 429
        # Caller B (different real IP) should still have quota despite
        # sharing the same TCP peer (the test client).
        assert c.get("/", headers={"CF-Connecting-IP": "203.0.113.99"}).status_code == 307

    monkeypatch.setattr(settings_module, "_settings", None)


def test_rate_limit_keys_per_real_client_behind_an_alb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end shape of the ALB fix.

    Two callers sharing one TCP peer (the ALB) get separate buckets,
    and a caller who forges a first hop cannot escape their own bucket
    by varying it -- which is what makes the limit meaningful behind a
    proxy that appends rather than replaces.
    """
    monkeypatch.setenv("OPENSALESTAX_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENSALESTAX_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("OPENSALESTAX_TRUST_FORWARDED_FOR", "true")
    monkeypatch.setenv("OPENSALESTAX_TRUSTED_PROXY_HOPS", "1")
    monkeypatch.setattr(settings_module, "_settings", None)

    app = create_app()
    with TestClient(app, follow_redirects=False) as c:
        # Caller A burns its quota. The ALB appended its IP last.
        for _ in range(2):
            assert c.get("/", headers={"X-Forwarded-For": "198.51.100.10"}).status_code == 307
        assert c.get("/", headers={"X-Forwarded-For": "198.51.100.10"}).status_code == 429

        # Same caller, now forging a different first hop. Under the old
        # first-hop behaviour this bought a fresh bucket; counting from
        # the right, it is still caller A and still spent.
        assert (
            c.get(
                "/",
                headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.10"},
            ).status_code
            == 429
        )

        # A genuinely different client still has its own quota.
        assert c.get("/", headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 307

    monkeypatch.setattr(settings_module, "_settings", None)


# --- every v1 route is limited -------------------------------------------
#
# The bug this guards against: FastAPI 0.138 stopped flattening
# include_router into individual APIRoutes, so SlowAPIMiddleware could no
# longer find a handler for any /v1 path and silently skipped the check. The
# whole API served unlimited while the suite stayed green, because the only
# rate-limit test hit "/" -- a route declared directly on the app, and the one
# route still covered.
#
# So these assert on the real surface, and the inventory test below makes a
# newly added route fail until someone decides which list it belongs in.

# Requests must be VALID. The decorator wraps the endpoint, so FastAPI
# resolves dependencies and validates the body FIRST -- a malformed request
# 422s before the limiter is ever consulted.
_RATE_LIMITED: list[tuple[str, str, dict]] = [
    ("GET", "/v1/capabilities", {}),
    ("GET", "/v1/states", {}),
    ("GET", "/v1/rates", {"params": {"zip5": "10001"}}),
    (
        "POST",
        "/v1/calculate",
        {
            "json": {
                "address": {"zip5": "10001"},
                "line_items": [{"id": "i", "amount": "1.00", "quantity": 1}],
                "shipping": {"amount": "0.00", "separately_stated": True},
            }
        },
    ),
]

# Monitors must be able to poll liveness without consuming quota.
_EXEMPT: list[tuple[str, str]] = [("GET", "/v1/health")]


def _app_with_limit(monkeypatch: pytest.MonkeyPatch, limit: int):
    monkeypatch.setenv("OPENSALESTAX_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENSALESTAX_RATE_LIMIT_PER_MINUTE", str(limit))
    monkeypatch.setattr(settings_module, "_settings", None)
    return create_app()


@pytest.mark.parametrize(("method", "path", "kwargs"), _RATE_LIMITED)
def test_v1_route_is_rate_limited(
    method: str, path: str, kwargs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the threshold the route must refuse, whatever it returns below it.

    Earlier responses may be 500s here -- /v1/rates and /v1/calculate reach
    for a database this unit test has not got. That is fine and deliberate:
    the limiter runs in the decorator, ahead of the endpoint body, so the
    429 arrives without the route ever touching the database.
    """
    app = _app_with_limit(monkeypatch, 2)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        codes = [c.request(method, path, **kwargs).status_code for _ in range(4)]
    assert codes[-1] == 429, f"{method} {path} never refused: {codes}"


@pytest.mark.parametrize(("method", "path"), _EXEMPT)
def test_v1_route_is_exempt(method: str, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exempt routes must keep answering well past the threshold."""
    app = _app_with_limit(monkeypatch, 2)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        codes = [c.request(method, path).status_code for _ in range(6)]
    assert 429 not in codes, f"{method} {path} was throttled: {codes}"


def test_route_inventory_covers_every_v1_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new v1 route must be classified, not silently unprotected.

    Without this, adding a route is enough to reopen the original hole: it
    would simply not be covered by any test and nobody would notice.
    """
    app = _app_with_limit(monkeypatch, 60)
    declared = {
        (method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops
    }
    classified = {(m, p) for m, p, _ in _RATE_LIMITED} | set(_EXEMPT)
    assert declared == classified, (
        "v1 route inventory is out of date. Add the new route to _RATE_LIMITED, "
        "or to _EXEMPT with a reason.\n"
        f"  undeclared here : {declared - classified}\n"
        f"  gone from app   : {classified - declared}"
    )
