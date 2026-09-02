import logging
from collections import Counter
from time import time
from urllib.parse import urljoin

import aiohttp
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util.location import distance

from .ufanet import DomApi
from .utils import (
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    CONF_NAME,
    MAX_PUBLIC_CAMERAS,
    SCREEN,
    TOKEN_REFRESH_BUFFER,
    VIDEO,
    WS_VIDEO,
    decode_token,
    short_address,
    transliterate_ru,
)

_LOGGER = logging.getLogger(__name__)


HEADERS = {
    "Accept-Language": "ru_RU",
    "User-Agent": "OnePlus NE2211 Android app: Smarthome, OS: 9",
    "Content-Type": "application/json",
}

# Live TTL we ask cams_server for on public cameras. 86400 is the server-side
# maximum — larger values are rejected outright, the default is 3600.
PUBLIC_TOKEN_L_TTL = 86400
PUBLIC_PAGE_SIZE = 200
PUBLIC_CAMERA_FIELDS = [
    "number",
    "title",
    "address",
    "latitude",
    "longitude",
    "server",
    "token_l",
]


def _public_camera_title(title: str | None, address: str | None) -> str:
    """Disambiguate a city camera's name with its street.

    Ufanet names most public cameras "Камера 1", so the raw title alone would
    produce dozens of identically-named devices. The street+house suffix is
    what makes them tellable apart in the UI.
    """
    title = (title or "").strip()
    area = short_address(address)
    if not area or area.lower() in title.lower():
        return title or area or "camera"
    return f"{title}, {area}" if title else area


def disambiguate_titles(cameras: dict) -> dict:
    """Give same-named city cameras a short suffix so devices stay tellable apart.

    Two cameras at one address routinely share a title ("Камера 1" twice on
    ул Маршала Баграмяна, д 4). entity_ids already carry the camera number, but
    the device name is what a user actually reads.
    """
    counts = Counter(cam["title"] for cam in cameras.values())
    for cam_id, cam in cameras.items():
        if counts[cam["title"]] > 1:
            cam["title"] = f"{cam['title']} #{cam_id[-4:]}"
    return cameras


def filter_public_cameras(
    items: list[dict],
    home: tuple[float, float] | None = None,
    radius_km: float | None = None,
    limit: int = MAX_PUBLIC_CAMERAS,
) -> list[dict]:
    """Narrow the city-camera list down to what we're willing to create.

    `radius_km` is applied against `home` (HA's own coordinates); cameras
    without coordinates are dropped when a radius is in play, since there is
    no way to tell whether they are nearby. Results are ordered by distance
    when we can compute one, so truncating at `limit` keeps the closest.
    """
    scored: list[tuple[float | None, dict]] = []
    for item in items:
        lat, lon = item.get("latitude"), item.get("longitude")
        if home is None or lat is None or lon is None:
            if radius_km:
                continue
            scored.append((None, item))
            continue
        km = distance(home[0], home[1], float(lat), float(lon)) / 1000
        if radius_km and km > radius_km:
            continue
        scored.append((km, item))

    scored.sort(key=lambda pair: (pair[0] is None, pair[0]))
    if len(scored) > limit:
        _LOGGER.warning(
            "Public cameras: %s matched the filters, keeping the %s nearest. "
            "Narrow the search text or the radius to pick fewer.",
            len(scored),
            limit,
        )
    return [item for _, item in scored[:limit]]


class UcamsApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, ufanet_api: DomApi):
        self.hass = hass
        self._ufanet_api = ufanet_api
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.cameras = {}
        # City cameras live in their own dict: they come from a different
        # endpoint, have no archive, and must never leak into the flows that
        # assume a camera belongs to the contract (areas, archive buttons).
        self.public_cameras: dict = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]
        # cams_server is only needed for archive — discovered + authenticated lazily
        # when get_camera_archive is first called. Live streams + screenshots
        # come from dom.ufanet.ru/api/v1/cctv with the dom JWT alone.
        self.cams_server: str | None = None
        self.token: str | None = None
        self.token_expiration: int = 0
        self._cams_session: ClientSession | None = None

    async def _ensure_cams_session(self) -> ClientSession:
        """Lazily create + authenticate a session against cams_server.

        Only the archive flow needs this — see project_v1_cctv_hybrid_plan
        in the assistant's memory. cams_server URL is discovered from the
        contract response on first use.
        """
        if self._cams_session is None:
            self._cams_session = ClientSession(
                headers=HEADERS,
                connector=aiohttp.TCPConnector(
                    resolver=aiohttp.ThreadedResolver(),
                ),
            )
        now = int(time())
        if (
            self.token
            and now < self.token_expiration - TOKEN_REFRESH_BUFFER
            and self._ufanet_api.token_expiration > now
        ):
            return self._cams_session
        await self._authenticate_cams()
        return self._cams_session

    async def _authenticate_cams(self) -> None:
        if not self.cams_server:
            cams_servers = set()
            contracts = await self._ufanet_api.get_contract_info()
            for contract in contracts:
                cams_servers.add(contract.get("isp_org", {}).get("cams_server", {}).get("url"))
            cams_servers.discard(None)
            if not cams_servers:
                raise ConfigEntryNotReady("Cams server URL not available")
            if len(cams_servers) > 1:
                _LOGGER.warning("Multiple cams servers found: %s", cams_servers)
            self.cams_server = next(iter(cams_servers))

        assert self._cams_session is not None
        url = urljoin(self.cams_server, "api/v0/auth/?ttl=20800")
        # Bootstrap with dom JWT, then swap for cams_server bearer
        self._cams_session.headers["Authorization"] = self._ufanet_api.session.headers.get(
            "Authorization"
        )
        async with self._cams_session.post(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            self.token = data["token"]
            self.token_expiration = decode_token(self.token).get("exp", 0)
            self._cams_session.headers["Authorization"] = f"Bearer {self.token}"

    def _build_camera_entry(self, cam: dict, servers: dict, is_public: bool = False) -> dict | None:
        """Turn one API camera row into the dict every platform consumes.

        Shared by the three sources that hand back cameras: dom /api/v1/cctv
        (`servers` key), cams_server search and cams_server `this` (`server`
        key) — hence `servers` being passed in rather than looked up here.
        """
        cam_id = cam["number"]
        token_l = cam["token_l"]
        domain = servers.get("domain")
        screenshot_domain = servers.get("screenshot_domain")

        if not domain or not screenshot_domain:
            _LOGGER.warning("Camera %s missing domain info, skipping", cam_id)
            return None

        address = cam.get("address")
        title = cam.get("title")
        if is_public:
            title = _public_camera_title(title, address)

        rtsp_link = f"rtsp://{domain}/{cam_id}?token={token_l}&tracks=v1a1"
        ws_video = urljoin(
            f"wss://{domain}", f"{cam_id}/mse_ld?tracks=a1v1&realtime=true&token={token_l}"
        )
        url_screen = urljoin(
            f"https://{screenshot_domain}",
            f"api/v0/screenshots/{cam_id}~600.jpg?token={token_l}",
        )

        return {
            "id": cam_id,
            "title": title,
            "domain": domain,
            "url_video": rtsp_link,
            "url_ws_video": ws_video,
            "url_screen": url_screen,
            "token_l": token_l,
            "latitude": cam.get("latitude"),
            "longitude": cam.get("longitude"),
            "address": address,
            "is_public": is_public,
        }

    async def get_cameras_info(self) -> dict:
        """Fetch the camera list from dom /api/v1/cctv (one round-trip, no pagination)."""
        cctv = await self._ufanet_api.get_cctv_list()
        self.cameras = {}
        for cam in cctv:
            entry = self._build_camera_entry(cam, cam.get("servers") or {})
            if entry:
                self.cameras[entry["id"]] = entry

        return self.cameras

    async def get_public_cameras_info(
        self,
        query: str | None = None,
        radius_km: float | None = None,
        home: tuple[float, float] | None = None,
    ) -> dict:
        """Discover Ufanet's public "city" cameras and cache them.

        These are the cameras the mobile app shows under «Городские камеры».
        They are served by cams_server, not dom: `public_cameras: true` on
        /api/v0/cameras/search/ returns every public camera in every town the
        ISP covers (~2000), each with a live token minted for our account.
        `query` is matched server-side against both title and address; the
        radius is applied locally because the API has no geo filter.

        Live only — the API hands back `token_r`/`token_d` as null for public
        cameras, so there is no archive to expose.
        """
        session = await self._ensure_cams_session()
        raw = await self._search_public_cameras(session, query)
        _LOGGER.debug("Public cameras: %s returned by the API", len(raw))

        self.public_cameras = {}
        for cam in filter_public_cameras(raw, home, radius_km):
            entry = self._build_camera_entry(cam, cam.get("server") or {}, is_public=True)
            if entry:
                self.public_cameras[entry["id"]] = entry

        disambiguate_titles(self.public_cameras)
        _LOGGER.info("Public cameras: %s kept after filtering", len(self.public_cameras))
        return self.public_cameras

    async def _search_public_cameras(self, session: ClientSession, query: str | None) -> list[dict]:
        """Page through /api/v0/cameras/search/ and return the raw rows."""
        results: list[dict] = []
        page = 1
        while True:
            payload = {
                "fields": PUBLIC_CAMERA_FIELDS,
                "public_cameras": True,
                "user_cameras": False,
                "order_by": "addr_asc",
                "token_l_ttl": PUBLIC_TOKEN_L_TTL,
                "page": page,
                "page_size": PUBLIC_PAGE_SIZE,
            }
            if query:
                payload["query"] = query
            data = await self._post_cams(session, "search", payload)
            results.extend(data.get("results") or [])
            pages = (data.get("page") or {}).get("all") or 1
            if page >= pages:
                return results
            page += 1

    async def _refresh_public_tokens(self) -> None:
        """Re-mint live tokens for the cameras we already decided to expose.

        Cheaper and more stable than re-running the search: /this/ takes the
        exact numbers, so a camera never disappears mid-session just because
        the search filters would no longer match it.
        """
        if not self.public_cameras:
            return
        session = await self._ensure_cams_session()
        payload = {
            "fields": PUBLIC_CAMERA_FIELDS,
            "token_l_ttl": PUBLIC_TOKEN_L_TTL,
            "numbers": list(self.public_cameras),
            "page": 1,
            "page_size": len(self.public_cameras),
        }
        data = await self._post_cams(session, "this", payload)
        for cam in data.get("results") or []:
            entry = self._build_camera_entry(cam, cam.get("server") or {}, is_public=True)
            if entry:
                self.public_cameras[entry["id"]] = entry
        disambiguate_titles(self.public_cameras)

    async def _post_cams(self, session: ClientSession, endpoint: str, payload: dict) -> dict:
        """POST to cams_server, re-authenticating once on a 401."""
        url = f"{self.cams_server}/api/v0/cameras/{endpoint}/"
        for attempt in range(2):
            async with session.post(url, params={"lang": "ru"}, json=payload) as resp:
                if resp.status == 401 and attempt == 0:
                    _LOGGER.debug("Cams auth expired on %s, re-authenticating", endpoint)
                    await self._authenticate_cams()
                    continue
                resp.raise_for_status()
                return await resp.json()
        return {}

    def build_device_name(self, device_title) -> str:
        device_name = device_title.lower()
        device_name = f"{self.config_entry_name}.{device_name}"
        device_name = transliterate_ru(device_name)
        return device_name.capitalize()

    def build_display_name(self, camera_info: dict) -> str:
        """The name a user actually reads on the device and its entities.

        Contract cameras keep the transliterated legacy name: their device is
        re-registered under the raw Russian title by `ArchiveLinkSensor`
        anyway (sensor is set up after camera), and their entity names have
        always been the transliterated form — changing that would rename
        entities people already reference. City cameras have no archive
        sensor, so without this they would be the only devices left reading
        as latin. `build_device_name` still supplies the entity_id slug.
        """
        if camera_info.get("is_public"):
            return camera_info["title"]
        return self.build_device_name(camera_info["title"])

    async def get_camera_info(self, camera_id: str) -> dict | None:
        if camera_id in self.public_cameras:
            return self.public_cameras[camera_id]
        if camera_id not in self.cameras:
            await self.get_cameras_info()
        return self.cameras.get(camera_id)

    async def _refresh_camera_source(self, camera_id: str) -> None:
        """Re-fetch whichever list the camera came from."""
        if camera_id in self.public_cameras:
            await self._refresh_public_tokens()
        else:
            await self.get_cameras_info()

    async def get_camera_url(self, camera_id: str, url_type: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)
        if not camera_info:
            _LOGGER.error("Camera %s not found.", camera_id)
            return None

        now = int(time())
        token_exp = self._decode_token_exp(camera_info.get("token_l"))
        if token_exp and (token_exp - now) < TOKEN_REFRESH_BUFFER:
            _LOGGER.warning(
                "Camera token %s is about to expire (%s sec), refreshing cameras list.",
                camera_id,
                token_exp - now,
            )
            await self._refresh_camera_source(camera_id)
            camera_info = await self.get_camera_info(camera_id)

        token_exp = self._decode_token_exp(camera_info.get("token_l"))
        if not token_exp or (token_exp - now) < TOKEN_REFRESH_BUFFER:
            _LOGGER.error("Failed to update token for camera %s.", camera_id)
            return None

        url_key = f"url_{url_type}"
        url = camera_info.get(url_key)
        if not url:
            _LOGGER.error("URL (%s) not found for camera %s.", url_type, camera_id)
        else:
            _LOGGER.debug("URL (%s) for camera %s: %s", url_type, camera_id, url)
        return url

    def _decode_token_exp(self, token: str) -> int | None:
        try:
            decoded = decode_token(token)
            return int(decoded.get("exp", 0))
        except Exception as e:
            _LOGGER.error("Token decoding error: %s", e)
            return None

    async def get_camera_stream_ws_url(self, camera_id: str) -> str | None:
        return await self.get_camera_url(camera_id, WS_VIDEO)

    async def get_camera_stream_url(self, camera_id: str):
        return await self.get_camera_url(camera_id, VIDEO)

    async def get_camera_image(self, camera_id: str) -> bytes | None:
        """Pull the cached screenshot URL via the dom session.

        token_l from /api/v1/cctv works for the screenshot endpoint without
        any cams_server auth (verified empirically).
        """
        url = await self.get_camera_url(camera_id, SCREEN)
        if not url:
            return None
        session = await self._ufanet_api.get_authenticated_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def close(self) -> None:
        if self._cams_session is not None:
            await self._cams_session.close()
            self._cams_session = None

    async def get_camera_archive(self, camera_id: str, start_time: int, delta_time: int):
        """Get archive download URL.

        This is the only path that still needs cams_server: token_d (with
        embedded ds/dd archive window) is only issued by /api/v0/cameras/this/.
        token_r from /api/v1/cctv returns 403 against the archive endpoint.
        """
        if camera_id in self.public_cameras:
            # Public cameras are live-only: the API returns token_r/token_d as
            # null for them, so there is nothing to build an archive URL from.
            _LOGGER.warning("Camera %s is a public city camera — no archive available", camera_id)
            return None

        session = await self._ensure_cams_session()
        camera_info = await self.get_camera_info(camera_id)
        if not camera_info:
            _LOGGER.error("Camera %s not found for archive request", camera_id)
            return None
        domain = camera_info.get("domain")

        response_data = await self._post_cams(
            session,
            "this",
            {
                "fields": ["token_d"],
                "token_d_ttl": 3600,
                "token_d_duration": delta_time,
                "token_d_start": start_time,
                "numbers": [camera_id],
            },
        )

        result = response_data.get("results", [])
        if not result:
            return None
        for item in result:
            if item["number"] == camera_id:
                file_extension = ".mp4" if delta_time <= 3600 else ".ts"
                archive_url = (
                    f"https://{domain}/{item['number']}/"
                    f"archive-{start_time}-{delta_time}{file_extension}"
                    f"?token={item['token_d']}"
                )
                _LOGGER.debug(archive_url)
                return archive_url
