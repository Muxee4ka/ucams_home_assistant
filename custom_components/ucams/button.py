import logging
import time

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .utils import DOMAIN, build_object_id, scoped_unique_id

_LOGGER = logging.getLogger(__name__)

# (duration in seconds, translation key, log label, entity_id suffix). The
# suffix matches the ids buttons got before names were translated, so a new
# camera's buttons line up with the ones users already have.
ARCHIVE_PRESETS = (
    (300, "archive_5_min", "за 5 минут", "arkhiv_last_5_min"),
    (3600, "archive_1_hour", "за 1 час", "arkhiv_last_hour"),
    (5 * 3600, "archive_5_hours", "за 5 часов", "arkhiv_last_5_hours"),
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = hass.data[config_entry.entry_id]["cameras_info"]
    if not cameras_api:
        _LOGGER.error("cameras_api не найден")
        return
    if not cameras_info:
        _LOGGER.error("cameras_info не найден")
        return

    buttons = [
        ArchiveButton(hass, config_entry, cameras_api, camera, *preset)
        for camera in cameras_info.values()
        for preset in ARCHIVE_PRESETS
    ]
    async_add_entities(buttons)


class ArchiveButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:video-vintage"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry,
        cameras_api,
        camera: dict,
        duration: int,
        translation_key: str,
        label: str,
        object_id_suffix: str,
    ):
        self.hass = hass
        self._config_entry = config_entry
        self._cameras_api = cameras_api
        self._camera_id = camera["id"]
        self._label = label
        self._duration = duration
        self._device_name = cameras_api.build_display_name(camera)

        self._attr_translation_key = translation_key
        self._attr_unique_id = scoped_unique_id(
            config_entry.entry_id, f"archive_button_{self._camera_id}_{duration}"
        )
        self.entity_id = "button.{}".format(
            build_object_id(cameras_api.build_device_name(camera["title"]), object_id_suffix)
        )

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self._config_entry.entry_id}_{self._camera_id}")},
            "name": self._device_name,
            "manufacturer": "Ufanet",
        }

    async def async_press(self):
        """Press the button."""
        # Вычисляем время начала архива: текущий момент минус длительность
        start_time = int(time.time()) - self._duration
        _LOGGER.debug(
            "Нажата кнопка '%s' для камеры %s: start_time=%d, duration=%d",
            self._label,
            self._camera_id,
            start_time,
            self._duration,
        )
        archive_url = await self._cameras_api.get_camera_archive(
            self._camera_id, start_time, self._duration
        )
        if archive_url:
            _LOGGER.info("Получена ссылка на архив для '%s': %s", self._label, archive_url)
            sensor = self.hass.data[self._config_entry.entry_id]["archive_link_sensors"].get(
                self._camera_id
            )
            if sensor:
                sensor.update_link(archive_url, comment=self._label)
        else:
            _LOGGER.error(
                "Не удалось получить архив для камеры %s, период '%s'", self._camera_id, self._label
            )
