import logging

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util.location import distance

from .ucams import UcamsApi
from .utils import DOMAIN, PUBLIC_CAMERA_MODEL, all_cameras_info, build_object_id

_LOGGER = logging.getLogger(__name__)

SOURCE = "ucams"


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    data = hass.data[config_entry.entry_id]
    cameras_api: UcamsApi = data["cameras_api"]
    entities = []
    for camera_info in all_cameras_info(data):
        lat = camera_info.get("latitude")
        lon = camera_info.get("longitude")
        if lat is None or lon is None:
            continue
        entities.append(UcamsLocation(hass, config_entry, cameras_api, camera_info))
    async_add_entities(entities)


class UcamsLocation(GeolocationEvent):
    _attr_should_poll = False
    _attr_source = SOURCE
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:cctv"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: UcamsApi,
        camera_info: dict,
    ) -> None:
        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.camera_id = camera_info["id"]
        self.display_name = cameras_api.build_display_name(camera_info)
        self._address = camera_info.get("address")
        self._is_public = bool(camera_info.get("is_public"))

        self._attr_unique_id = f"geo-{self.camera_id}"
        self._attr_name = self.display_name
        if self._is_public:
            # City-camera names are Russian, and this platform otherwise derives
            # the entity_id from the name. Pin it to the same transliterated slug
            # camera/image use so the three entities of one camera match — and so
            # a rename upstream can't move the entity. Contract cameras keep the
            # auto-derived id they have always had.
            self.entity_id = "geo_location.{}".format(
                build_object_id(cameras_api.build_device_name(camera_info["title"]), self.camera_id)
            )
        self._attr_latitude = float(camera_info["latitude"])
        self._attr_longitude = float(camera_info["longitude"])

        # Cameras don't move — compute distance from home once at init.
        self._attr_distance = distance(
            hass.config.latitude,
            hass.config.longitude,
            self._attr_latitude,
            self._attr_longitude,
        )
        if self._attr_distance is not None:
            self._attr_distance = self._attr_distance / 1000  # meters → km

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {}
        if self._address:
            attrs["address"] = self._address
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.display_name,
            "manufacturer": "Ufanet",
        }
        if self._is_public:
            info["model"] = PUBLIC_CAMERA_MODEL
        return info
