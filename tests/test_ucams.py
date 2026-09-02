"""Tests for the ucams module."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ucams.ucams import disambiguate_titles, filter_public_cameras

LIVE_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJleHAiOjE4NTAwMDAwMDAsImlhdCI6MTczNzI5MjM4MiwidSI6IjEyMzQ1Njc4OSIs"
    "Im4iOiIxMjM0NTY3ODkwQUJDREVGIiwidCI6IkwifQ."
    "9fBxSbTIULv-SGBeBors_Ym8wxTRzcw-WX1jWVc_2AM"
)
EXPIRED_LIVE_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJleHAiOjE3MzczNzg3ODIsImlhdCI6MTczNzI5MjM4MiwidSI6IjEyMzQ1Njc4OSIs"
    "Im4iOiIxMjM0NTY3ODkwQUJDREVGIiwidCI6IkwifQ."
    "76svm7tzgZHOLGqJp7_wjKWJ0HY51xi3k3f3HoNEvPc"
)

CCTV_FAKE_ITEM = {
    "address": "г Москва, ул Тверская, д 1",
    "inactivity_period": None,
    "latitude": 55.755826,
    "longitude": 37.6173,
    "number": "1234567890ABCDEF",
    "type": 1,
    "servers": {
        "domain": "flussonic-msk-1.cams.example.com",
        "screenshot_domain": "ucams-screen-1.example.com",
        "vendor_name": "Flussonic",
        "server": True,
    },
    "title": "камера1_фасад1",
    "token_l": LIVE_TOKEN,
    "token_r": LIVE_TOKEN,  # in production t:R but harmless to reuse here
}


@pytest.mark.asyncio
async def test_get_cameras_info_uses_v1_cctv(ucams_api, mock_ufanet_api):
    """get_cameras_info now reads from dom /api/v1/cctv via DomApi — no cams_server hit."""
    mock_ufanet_api.cctv_payload = [CCTV_FAKE_ITEM]
    cameras_info = await ucams_api.get_cameras_info()

    assert "1234567890ABCDEF" in cameras_info
    assert cameras_info["1234567890ABCDEF"]["title"] == "камера1_фасад1"
    assert (
        cameras_info["1234567890ABCDEF"]["url_video"]
        == f"rtsp://flussonic-msk-1.cams.example.com/1234567890ABCDEF?token={LIVE_TOKEN}&tracks=v1a1"
    )
    assert (
        cameras_info["1234567890ABCDEF"]["url_screen"]
        == f"https://ucams-screen-1.example.com/api/v0/screenshots/1234567890ABCDEF~600.jpg?token={LIVE_TOKEN}"
    )


@pytest.mark.asyncio
async def test_get_camera_image(ucams_api, mock_ufanet_api):
    """Screenshot fetch goes through the dom session (no cams_server bootstrap).

    Uses a hand-rolled async-context-manager mock instead of a real
    aiohttp.ClientSession — the latter spawns a daemon thread on close
    that pytest-homeassistant-custom-component flags as a lingering thread
    on 3.12.
    """
    mock_ufanet_api.cctv_payload = [CCTV_FAKE_ITEM]
    expected_url = (
        f"https://ucams-screen-1.example.com/api/v0/screenshots/"
        f"{CCTV_FAKE_ITEM['number']}~600.jpg?token={LIVE_TOKEN}"
    )

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.read = AsyncMock(return_value=b"image_data")

    @asynccontextmanager
    async def fake_get(url):
        assert url == expected_url
        yield response

    fake_session = MagicMock()
    fake_session.get = fake_get

    async def get_authed():
        return fake_session

    mock_ufanet_api.get_authenticated_session = get_authed

    image_data = await ucams_api.get_camera_image(CCTV_FAKE_ITEM["number"])
    assert image_data == b"image_data"


@pytest.mark.asyncio
async def test_token_expiry_triggers_cctv_refresh(ucams_api, mock_ufanet_api):
    """When token_l is near expiry, the camera list is re-pulled from dom."""
    expired_item = {**CCTV_FAKE_ITEM, "token_l": EXPIRED_LIVE_TOKEN}
    fresh_item = CCTV_FAKE_ITEM
    # First fetch hands back the expired token; second hands back a fresh one.
    payloads = [[expired_item], [fresh_item]]

    async def get_cctv_list_seq():
        return payloads.pop(0) if len(payloads) > 1 else payloads[0]

    mock_ufanet_api.get_cctv_list = get_cctv_list_seq

    camera_url = await ucams_api.get_camera_stream_url(CCTV_FAKE_ITEM["number"])
    assert (
        camera_url
        == f"rtsp://flussonic-msk-1.cams.example.com/{CCTV_FAKE_ITEM['number']}?token={LIVE_TOKEN}&tracks=v1a1"
    )


@pytest.mark.asyncio
async def test_get_camera_archive_uses_cams_server(ucams_api, mock_ufanet_api):
    """Archive still goes through cams_server (only place token_d is issued).

    Stubs `_ensure_cams_session` to skip building a real aiohttp.ClientSession
    (that path leaves a `_run_safe_shutdown_loop` daemon thread on close,
    which pytest-homeassistant-custom-component flags as lingering on 3.12).
    """
    mock_ufanet_api.cctv_payload = [CCTV_FAKE_ITEM]

    cams_response_data = {
        "count": 1,
        "results": [{"number": CCTV_FAKE_ITEM["number"], "token_d": "token_d"}],
    }

    response = MagicMock()
    response.status = 200
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value=cams_response_data)

    @asynccontextmanager
    async def fake_post(url, params=None, json=None):
        yield response

    fake_session = MagicMock()
    fake_session.post = fake_post

    async def ensure_cams_session():
        ucams_api.cams_server = "https://cams.example.com"
        return fake_session

    ucams_api._ensure_cams_session = ensure_cams_session

    archive_url = await ucams_api.get_camera_archive(CCTV_FAKE_ITEM["number"], 0, 3600)
    assert (
        archive_url
        == f"https://flussonic-msk-1.cams.example.com/{CCTV_FAKE_ITEM['number']}/archive-0-3600.mp4?token=token_d"
    )

    archive_url = await ucams_api.get_camera_archive(CCTV_FAKE_ITEM["number"], 0, 3700)
    assert (
        archive_url
        == f"https://flussonic-msk-1.cams.example.com/{CCTV_FAKE_ITEM['number']}/archive-0-3700.ts?token=token_d"
    )


PUBLIC_FAKE_ITEM = {
    "number": "1712127689UPE47",
    "title": "Сенная площадь",
    "address": "г Нижний Новгород, ул Максима Горького, д 262",
    "latitude": 56.322613,
    "longitude": 44.034414,
    "server": {
        "domain": "manul-nn-12.cams.example.com",
        "screenshot_domain": "ucams-screen-1.example.com",
        "vendor_name": "UMS",
        "server": True,
    },
    "token_l": LIVE_TOKEN,
}


def _fake_cams_session(responses):
    """A stand-in for the cams_server aiohttp session.

    Hands back queued payloads and records what was posted, without building a
    real ClientSession — same reasoning as test_get_camera_archive_uses_cams_server.
    """
    calls = []

    @asynccontextmanager
    async def fake_post(url, params=None, json=None):
        calls.append((url, json))
        response = MagicMock()
        response.status = 200
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(return_value=responses.pop(0))
        yield response

    session = MagicMock()
    session.post = fake_post
    return session, calls


def _attach_cams_session(ucams_api, responses):
    session, calls = _fake_cams_session(responses)

    async def ensure_cams_session():
        ucams_api.cams_server = "https://cams.example.com"
        return session

    ucams_api._ensure_cams_session = ensure_cams_session
    return calls


@pytest.mark.asyncio
async def test_get_public_cameras_info(ucams_api):
    """City cameras come from cams_server search, tagged and street-suffixed."""
    calls = _attach_cams_session(
        ucams_api,
        [{"count": 1, "page": {"all": 1}, "results": [PUBLIC_FAKE_ITEM]}],
    )

    public = await ucams_api.get_public_cameras_info(query="Сенная")
    number = PUBLIC_FAKE_ITEM["number"]

    assert list(public) == [number]
    camera = public[number]
    assert camera["is_public"] is True
    # Raw title is "Сенная площадь"; the street keeps look-alike names apart.
    assert camera["title"] == "Сенная площадь, ул Максима Горького, д 262"
    assert (
        camera["url_video"]
        == f"rtsp://manul-nn-12.cams.example.com/{number}?token={LIVE_TOKEN}&tracks=v1a1"
    )
    assert (
        camera["url_screen"]
        == f"https://ucams-screen-1.example.com/api/v0/screenshots/{number}~600.jpg?token={LIVE_TOKEN}"
    )

    url, payload = calls[0]
    assert url == "https://cams.example.com/api/v0/cameras/search/"
    assert payload["public_cameras"] is True
    assert payload["user_cameras"] is False
    assert payload["query"] == "Сенная"


@pytest.mark.asyncio
async def test_get_public_cameras_info_pages(ucams_api):
    """The search endpoint is paged through until the last page."""
    second = {**PUBLIC_FAKE_ITEM, "number": "1712127689UPE48"}
    calls = _attach_cams_session(
        ucams_api,
        [
            {"count": 2, "page": {"all": 2}, "results": [PUBLIC_FAKE_ITEM]},
            {"count": 2, "page": {"all": 2}, "results": [second]},
        ],
    )

    public = await ucams_api.get_public_cameras_info()

    assert len(public) == 2
    assert [payload["page"] for _, payload in calls] == [1, 2]


@pytest.mark.asyncio
async def test_public_camera_token_refresh_uses_this_endpoint(ucams_api):
    """An expiring live token is re-minted by number, not by re-searching."""
    expired = {**PUBLIC_FAKE_ITEM, "token_l": EXPIRED_LIVE_TOKEN}
    calls = _attach_cams_session(
        ucams_api,
        [
            {"count": 1, "page": {"all": 1}, "results": [expired]},
            {"count": 1, "results": [PUBLIC_FAKE_ITEM]},
        ],
    )
    await ucams_api.get_public_cameras_info()

    number = PUBLIC_FAKE_ITEM["number"]
    url = await ucams_api.get_camera_stream_url(number)

    assert url == (f"rtsp://manul-nn-12.cams.example.com/{number}?token={LIVE_TOKEN}&tracks=v1a1")
    refresh_url, refresh_payload = calls[1]
    assert refresh_url == "https://cams.example.com/api/v0/cameras/this/"
    assert refresh_payload["numbers"] == [number]


@pytest.mark.asyncio
async def test_public_camera_has_no_archive(ucams_api):
    """Public cameras get null token_d upstream, so we refuse before requesting."""
    calls = _attach_cams_session(
        ucams_api,
        [{"count": 1, "page": {"all": 1}, "results": [PUBLIC_FAKE_ITEM]}],
    )
    await ucams_api.get_public_cameras_info()

    assert await ucams_api.get_camera_archive(PUBLIC_FAKE_ITEM["number"], 0, 3600) is None
    assert len(calls) == 1  # no archive round-trip was attempted


def test_filter_public_cameras_by_radius():
    """Radius is measured from home; cameras without coordinates drop out."""
    near = {"number": "near", "latitude": 56.3226, "longitude": 44.0344}
    far = {"number": "far", "latitude": 54.7734, "longitude": 56.0614}  # Уфа
    nowhere = {"number": "nowhere", "latitude": None, "longitude": None}

    kept = filter_public_cameras([far, near, nowhere], home=(56.3230, 44.0350), radius_km=5)

    assert [item["number"] for item in kept] == ["near"]


def test_filter_public_cameras_caps_and_sorts_by_distance():
    """Without a radius everything matches, so the closest ones win the cap."""
    items = [{"number": str(i), "latitude": 56.0 + i / 100, "longitude": 44.0} for i in range(5)]

    kept = filter_public_cameras(items[::-1], home=(56.0, 44.0), limit=3)

    assert [item["number"] for item in kept] == ["0", "1", "2"]


def test_disambiguate_titles_only_touches_duplicates():
    """Two "Камера 1" at one address must not become two identical devices."""
    cameras = {
        "1739433311PPS0": {"title": "Камера 1, ул Маршала Баграмяна, д 4"},
        "1739433265XQF33": {"title": "Камера 1, ул Маршала Баграмяна, д 4"},
        "1712224251NKN5": {"title": "Метромост, ул Дальняя, д 8"},
    }

    disambiguate_titles(cameras)

    assert cameras["1739433311PPS0"]["title"] == "Камера 1, ул Маршала Баграмяна, д 4 #PPS0"
    assert cameras["1739433265XQF33"]["title"] == "Камера 1, ул Маршала Баграмяна, д 4 #QF33"
    assert cameras["1712224251NKN5"]["title"] == "Метромост, ул Дальняя, д 8"


def test_build_display_name_keeps_city_cameras_in_russian(ucams_api):
    """City cameras read as Russian in the UI; contract cameras keep the
    transliterated name their entities have always had."""
    public = {"title": "Метромост, ул Дальняя, д 8", "is_public": True}
    own = {"title": "камера1_фасад1", "is_public": False}

    assert ucams_api.build_display_name(public) == "Метромост, ул Дальняя, д 8"
    assert ucams_api.build_display_name(own) == "Test config.kamera1_fasad1"
    # The entity_id slug is unaffected — it still comes from build_device_name.
    assert (
        ucams_api.build_device_name(public["title"]) == "Test config.metromost, ul dal'njaja, d 8"
    )
