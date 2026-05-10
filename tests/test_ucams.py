"""Tests for the ucams module."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

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
