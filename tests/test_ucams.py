""" Tests for the ucams module """
import pytest
from aioresponses import aioresponses

AUTH_FAKE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
CAMERA_FAKE_INFO = {
    'address': 'г Москва, ул Тверская, д 1',
    'analytics': ['motion_alarm'],
    'inactivity_period': None,
    'is_embed': False,
    'is_fav': True,
    'is_public': False,
    'latitude': 55.755826,
    'longitude': 37.6173,
    'number': '1234567890ABCDEF',
    'permission': 20,
    'record_disable_period': None,
    'server': {'domain': 'flussonic-msk-1.cams.example.com',
               'screenshot_domain': 'ucams-screen-1.example.com',
               'vendor_name': 'Flussonic'},
    'tariff': {'dvr_hours': 120,
               'dvr_size': None,
               'name': 'Видеоархив 5'},
    'title': 'камера1_фасад1',
    'token_l': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3MzczNzg3ODIsImlhdCI6MTczNzI5MjM4MiwidSI6IjEyMzQ1Njc4OSIsIm4iOiIxMjM0NTY3ODkwQUJDREVGIn0.76svm7tzgZHOLGqJp7_wjKWJ0HY51xi3k3f3HoNEvPc'
}


@pytest.mark.asyncio
async def test_get_cameras_info(ucams_api):
    """Test getting cameras info"""
    with aioresponses() as m:
        # Мок ответа на запрос авторизации
        m.post(
            "https://cams.example.com/api/v0/auth/?ttl=20800",
            payload={
                "token": AUTH_FAKE_TOKEN
            }
        )
        # Мок ответа на запрос информации о камерах
        m.post(
            "https://cams.example.com/api/v0/cameras/my/",
            payload={
                "results": [CAMERA_FAKE_INFO]
            }
        )

        cameras_info = await ucams_api.get_cameras_info()

        # Проверяем, что данные обработаны корректно
        assert "1234567890ABCDEF" in cameras_info
        assert cameras_info["1234567890ABCDEF"]["title"] == "камера1_фасад1"
        assert cameras_info["1234567890ABCDEF"]["url_video"] == f"rtsp://flussonic-msk-1.cams.example.com/1234567890ABCDEF?token={CAMERA_FAKE_INFO['token_l']}&tracks=v1a1"
        assert cameras_info["1234567890ABCDEF"]["url_screen"] == f"https://ucams-screen-1.example.com/api/v0/screenshots/1234567890ABCDEF~600.jpg?token={CAMERA_FAKE_INFO['token_l']}"


@pytest.mark.asyncio
async def test_authenticate(ucams_api):
    """Test authenticate method"""
    with aioresponses() as m:
        m.post("https://cams.example.com/api/v0/auth/?ttl=20800", payload={"token": AUTH_FAKE_TOKEN})
        await ucams_api._authenticate()
        assert ucams_api.token == AUTH_FAKE_TOKEN
        assert "Authorization" in ucams_api.session.headers
        assert ucams_api.session.headers["Authorization"] == f"Bearer {AUTH_FAKE_TOKEN}"


@pytest.mark.asyncio
async def test_get_camera_image(ucams_api):
    """Test getting camera image"""
    with aioresponses() as m:
        m.post("https://cams.example.com/api/v0/auth/?ttl=20800", payload={"token": AUTH_FAKE_TOKEN}, repeat=True)
        m.post("https://cams.example.com/api/v0/cameras/my/", payload={"results": [CAMERA_FAKE_INFO]})
        m.get(f"https://ucams-screen-1.example.com/api/v0/screenshots/{CAMERA_FAKE_INFO['number']}~600.jpg?token={CAMERA_FAKE_INFO['token_l']}",
              body=b"image_data")
        image_data = await ucams_api.get_camera_image(CAMERA_FAKE_INFO['number'])
        assert image_data == b"image_data"
