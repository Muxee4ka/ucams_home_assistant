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

# Live TTL we ask cams_server for. 86400 is the server-side maximum — larger
# values are rejected outright, the default is 3600.
TOKEN_L_TTL = 86400
PUBLIC_PAGE_SIZE = 200
CAMERA_FIELDS = [
    "number",
    "title",
    "address",
    "latitude",
    "longitude",
    "server",
    "token_l",
]
# How long to stop re-asking upstream after a camera failed to produce a
# usable live token. Without it every image refresh and every stream retry
# would re-pull the whole camera list for a camera that is simply broken.
TOKEN_RETRY_COOLDOWN = 300


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
        # camera_id -> unix time before which we won't chase a live token again
        self._token_retry_after: dict[str, float] = {}

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
        # `.get`, not `[...]`: /api/v1/cctv sometimes hands back a camera row
        # with `token_l: null`. The entry is still built so the entity exists
        # and `get_camera_url` can mint a token for it later — dropping it here
        # would make the camera silently vanish until the next HA restart.
        token_l = cam.get("token_l")
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

        tokenless = [cam_id for cam_id, cam in self.cameras.items() if not cam.get("token_l")]
        if tokenless:
            _LOGGER.warning(
                "dom /api/v1/cctv returned no live token for %s of %s cameras (%s); "
                "they will be minted from cams_server on demand",
                len(tokenless),
                len(self.cameras),
                ", ".join(tokenless),
            )
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
                "fields": CAMERA_FIELDS,
                "public_cameras": True,
                "user_cameras": False,
                "order_by": "addr_asc",
                "token_l_ttl": TOKEN_L_TTL,
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
            "fields": CAMERA_FIELDS,
            "token_l_ttl": TOKEN_L_TTL,
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

    async def _mint_contract_tokens(self, camera_ids: list[str]) -> None:
        """Mint live tokens for contract cameras straight from cams_server.

        Fallback for when dom `/api/v1/cctv` hands a camera back without a
        usable `token_l` — `/api/v0/cameras/this/` issues one for any camera
        the account can see, and also reports the streaming host to use, which
        is not always the one `/api/v1/cctv` named.

        The dom-side title and address are kept: they are what the entity_id
        and the area assignment were built from, and a rename here would move
        entities out from under the user's automations.
        """
        session = await self._ensure_cams_session()
        payload = {
            "fields": CAMERA_FIELDS,
            "token_l_ttl": TOKEN_L_TTL,
            "numbers": camera_ids,
            "page": 1,
            "page_size": len(camera_ids),
        }
        data = await self._post_cams(session, "this", payload)
        for cam in data.get("results") or []:
            existing = self.cameras.get(cam.get("number")) or {}
            merged = {**cam}
            for key in ("title", "address"):
                if existing.get(key):
                    merged[key] = existing[key]
            entry = self._build_camera_entry(merged, cam.get("server") or {})
            if entry:
                self.cameras[entry["id"]] = entry

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

    def _token_is_fresh(self, token: str | None) -> bool:
        """True when `token` still has more than the refresh buffer left on it."""
        exp = self._decode_token_exp(token)
        return bool(exp) and (exp - int(time())) >= TOKEN_REFRESH_BUFFER

    async def _renew_live_token(self, camera_id: str) -> dict | None:
        """Chase a usable `token_l` for one camera, or give up with a reason.

        Called whenever the cached token is missing, unparseable or about to
        expire — **including when it is missing**. That last case used to fall
        through without re-fetching anything, so a camera that came back from
        `/api/v1/cctv` with `token_l: null` once stayed dead for the life of
        the config entry and only a reload (e.g. editing the dom URL and
        changing it back) brought it round.
        """
        now = time()
        retry_after = self._token_retry_after.get(camera_id, 0)
        if now < retry_after:
            _LOGGER.debug(
                "Camera %s has no usable live token; not retrying for another %s sec",
                camera_id,
                int(retry_after - now),
            )
            return None

        await self._refresh_camera_source(camera_id)
        camera_info = await self.get_camera_info(camera_id)
        if camera_info and self._token_is_fresh(camera_info.get("token_l")):
            self._token_retry_after.pop(camera_id, None)
            return camera_info

        # dom had nothing usable. cams_server mints live tokens by camera
        # number for anything the account can see, contract cameras included.
        if camera_id not in self.public_cameras:
            try:
                await self._mint_contract_tokens([camera_id])
            except Exception as err:  # never break a stream request over this
                _LOGGER.debug("cams_server token mint failed for %s: %r", camera_id, err)
            camera_info = await self.get_camera_info(camera_id)
            if camera_info and self._token_is_fresh(camera_info.get("token_l")):
                _LOGGER.info("Live token for camera %s recovered via cams_server", camera_id)
                self._token_retry_after.pop(camera_id, None)
                return camera_info

        self._token_retry_after[camera_id] = now + TOKEN_RETRY_COOLDOWN
        if camera_info is None:
            _LOGGER.error("Camera %s disappeared from the account.", camera_id)
        else:
            token_exp = self._decode_token_exp(camera_info.get("token_l"))
            if not camera_info.get("token_l"):
                reason = "upstream returned no live token"
            elif not token_exp:
                reason = "live token could not be decoded"
            else:
                reason = f"live token expires in {token_exp - int(now)} sec"
            _LOGGER.error(
                "No usable live token for camera %s (%s). Streams and screenshots for "
                "it will stay unavailable; retrying in %s sec.",
                camera_id,
                reason,
                TOKEN_RETRY_COOLDOWN,
            )
        return None

    async def get_camera_url(self, camera_id: str, url_type: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)
        if not camera_info:
            _LOGGER.error("Camera %s not found.", camera_id)
            return None

        if not self._token_is_fresh(camera_info.get("token_l")):
            camera_info = await self._renew_live_token(camera_id)
            if camera_info is None:
                return None

        url_key = f"url_{url_type}"
        url = camera_info.get(url_key)
        if not url:
            _LOGGER.error("URL (%s) not found for camera %s.", url_type, camera_id)
        else:
            _LOGGER.debug("URL (%s) for camera %s: %s", url_type, camera_id, url)
        return url

    def _decode_token_exp(self, token: str | None) -> int | None:
        """Expiry of a live token, or None when there isn't one to read.

        `decode_token` already returns `{}` for a missing or unparseable
        token, so a None here means "no expiry known", never an exception.
        """
        try:
            return int(decode_token(token).get("exp", 0)) or None
        except (TypeError, ValueError) as err:
            _LOGGER.debug("Token decoding error: %s", err)
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
