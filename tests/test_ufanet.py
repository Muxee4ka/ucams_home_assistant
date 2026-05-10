"""Tests for the DomApi (Ufanet) auth and token caching."""

import time

import pytest
from aioresponses import aioresponses
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

AUTH_URL = "https://dom.example.com/api/v1/auth/auth_by_contract/"
REFRESH_URL = "https://dom.example.com/api/v1/auth/refresh/"

# Header { "typ": "JWT", "alg": "HS256" }, payload { "exp": 1850000000 } — a far-future expiry.
FRESH_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4NTAwMDAwMDB9.fake-sig"
# Distinct fresh JWTs so we can tell post-refresh tokens apart.
FRESH_JWT_2 = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4NTAwMDAwMDB9.fake-sig-2"
REFRESH_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4NjAwMDAwMDB9.fake-refresh"
EXPIRED_REFRESH_JWT = (
    # exp = 1577836800 (2020-01-01)
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE1Nzc4MzY4MDB9.fake-old-refresh"
)
EXPIRED_ACCESS_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE1Nzc4MzY4MDB9.fake-old"


@pytest.mark.asyncio
async def test_authenticate_persists_token_and_exp(dom_api):
    with aioresponses() as m:
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT}})
        await dom_api._authenticate()

    assert dom_api.token == FRESH_JWT
    assert dom_api.token_expiration == 1850000000
    assert dom_api.session.headers["Authorization"] == f"JWT {FRESH_JWT}"


@pytest.mark.asyncio
async def test_get_authenticated_session_caches_token(dom_api):
    """Second call must not re-hit the auth endpoint while the JWT is fresh."""
    with aioresponses() as m:
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT}})

        await dom_api.get_authenticated_session()
        await dom_api.get_authenticated_session()

        # aioresponses raises if a registered URL is hit more than once and the
        # mock isn't marked repeat=True, so a second auth round-trip would fail
        # the test loudly. Belt-and-braces: also assert the request count.
        requests = [k for k in m.requests if k[1].path.endswith("auth_by_contract/")]
        assert sum(len(m.requests[k]) for k in requests) == 1


@pytest.mark.asyncio
async def test_get_authenticated_session_refreshes_when_expired(dom_api):
    """An expired exp must trigger re-authentication."""
    expired_token = (
        # exp = 1577836800 (2020-01-01) — well in the past
        "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE1Nzc4MzY4MDB9.fake-sig"
    )
    with aioresponses() as m:
        m.post(AUTH_URL, payload={"token": {"access": expired_token}})
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT}})

        await dom_api.get_authenticated_session()
        # Sanity: first round set an expired token.
        assert dom_api.token_expiration < int(time.time())

        await dom_api.get_authenticated_session()
        assert dom_api.token == FRESH_JWT


@pytest.mark.asyncio
async def test_authenticate_raises_auth_failed_on_401(dom_api):
    with aioresponses() as m:
        m.post(AUTH_URL, status=401, payload={"detail": "bad credentials"})
        with pytest.raises(ConfigEntryAuthFailed):
            await dom_api._authenticate()


@pytest.mark.asyncio
async def test_authenticate_raises_not_ready_on_network_error(dom_api):
    import aiohttp

    with aioresponses() as m:
        m.post(AUTH_URL, exception=aiohttp.ClientConnectionError("boom"))
        with pytest.raises(ConfigEntryNotReady):
            await dom_api._authenticate()


@pytest.mark.asyncio
async def test_login_stores_refresh_token(dom_api):
    """auth_by_contract returns token.refresh; we stash it for later renewals."""
    with aioresponses() as m:
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT, "refresh": REFRESH_JWT}})
        await dom_api._authenticate()

    assert dom_api.refresh_token == REFRESH_JWT
    assert dom_api.refresh_token_expiration == 1860000000


@pytest.mark.asyncio
async def test_expired_access_uses_refresh_endpoint(dom_api):
    """Expired access + valid refresh: hit /refresh/, no contract+password resend."""
    with aioresponses() as m:
        m.post(
            AUTH_URL,
            payload={"token": {"access": EXPIRED_ACCESS_JWT, "refresh": REFRESH_JWT}},
        )
        m.post(REFRESH_URL, payload={"access": FRESH_JWT_2, "refresh": REFRESH_JWT})

        await dom_api.get_authenticated_session()  # primes the expired token + refresh
        await dom_api.get_authenticated_session()  # should renew via refresh

        assert dom_api.token == FRESH_JWT_2
        # auth_by_contract was called exactly once
        auth_keys = [k for k in m.requests if k[1].path.endswith("auth_by_contract/")]
        assert sum(len(m.requests[k]) for k in auth_keys) == 1
        # refresh was called exactly once
        refresh_keys = [k for k in m.requests if k[1].path.endswith("auth/refresh/")]
        assert sum(len(m.requests[k]) for k in refresh_keys) == 1


@pytest.mark.asyncio
async def test_refresh_token_rotates_on_renewal(dom_api):
    """Each refresh response carries a new refresh token; we must use the new one."""
    rotated = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE5MDAwMDAwMDB9.rotated"
    with aioresponses() as m:
        m.post(
            AUTH_URL,
            payload={"token": {"access": EXPIRED_ACCESS_JWT, "refresh": REFRESH_JWT}},
        )
        m.post(REFRESH_URL, payload={"access": FRESH_JWT_2, "refresh": rotated})

        await dom_api.get_authenticated_session()
        await dom_api.get_authenticated_session()

        assert dom_api.refresh_token == rotated
        assert dom_api.refresh_token_expiration == 1900000000


@pytest.mark.asyncio
async def test_refresh_failure_falls_back_to_full_login(dom_api):
    """If /refresh/ rejects us (e.g. invalid/revoked refresh), do a full re-auth."""
    with aioresponses() as m:
        m.post(
            AUTH_URL,
            payload={"token": {"access": EXPIRED_ACCESS_JWT, "refresh": REFRESH_JWT}},
        )
        m.post(REFRESH_URL, status=401, payload={"detail": "Не валидный токен"})
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT_2, "refresh": REFRESH_JWT}})

        await dom_api.get_authenticated_session()  # primes expired access + refresh
        await dom_api.get_authenticated_session()  # tries refresh, falls through to login

        assert dom_api.token == FRESH_JWT_2
        # auth_by_contract was called twice (initial + fallback)
        auth_keys = [k for k in m.requests if k[1].path.endswith("auth_by_contract/")]
        assert sum(len(m.requests[k]) for k in auth_keys) == 2


@pytest.mark.asyncio
async def test_expired_refresh_skips_refresh_endpoint(dom_api):
    """If the refresh JWT itself is expired, don't waste a request — full login."""
    with aioresponses() as m:
        m.post(
            AUTH_URL,
            payload={"token": {"access": EXPIRED_ACCESS_JWT, "refresh": EXPIRED_REFRESH_JWT}},
        )
        m.post(AUTH_URL, payload={"token": {"access": FRESH_JWT_2, "refresh": REFRESH_JWT}})

        await dom_api.get_authenticated_session()
        await dom_api.get_authenticated_session()

        assert dom_api.token == FRESH_JWT_2
        # No refresh call at all
        refresh_keys = [k for k in m.requests if k[1].path.endswith("auth/refresh/")]
        assert sum(len(m.requests[k]) for k in refresh_keys) == 0
