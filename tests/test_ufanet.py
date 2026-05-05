"""Tests for the DomApi (Ufanet) auth and token caching."""

import time

import pytest
from aioresponses import aioresponses
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

AUTH_URL = "https://dom.example.com/api/v1/auth/auth_by_contract/"

# Header { "typ": "JWT", "alg": "HS256" }, payload { "exp": 1850000000 } — a far-future expiry.
FRESH_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4NTAwMDAwMDB9.fake-sig"


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
