import time
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.ucams.utils import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = hass.data[config_entry.entry_id]["cameras_info"]
    if not cameras_api:
        _LOGGER.error("cameras_api не найден")
        return
    if not cameras_info:
        _LOGGER.error("cameras_info не найден")
        return

    buttons = []
    for camera in cameras_info.values():
        device_name = camera["title"]
        camera_id = camera["id"]
        buttons.append(ArchiveButton(hass, config_entry, cameras_api, camera_id, device_name, "Last 5 min", 300))
        buttons.append(ArchiveButton(hass, config_entry, cameras_api, camera_id, device_name, "Last hour", 3600))
        buttons.append(ArchiveButton(hass, config_entry, cameras_api, camera_id, device_name, "Last 5 hours", 5 * 3600))
    async_add_entities(buttons)


class ArchiveButton(ButtonEntity):
    def __init__(self, hass: HomeAssistant, config_entry, cameras_api, camera_id: str, device_name: str, label: str,
                 duration: int):
        """
        :param hass: HomeAssistant
        :param config_entry: ConfigEntry
        :param cameras_api: UcamsApi
        :param camera_id: str
        :param device_name: str
        :param label: str
        :param duration: int
        """
        self.hass = hass
        self._config_entry = config_entry
        self._cameras_api = cameras_api
        self._camera_id = camera_id
        self._label = label
        self._duration = duration
        self._device_name = self._cameras_api.build_device_name(device_name)

        self._attr_unique_id = f"archive_button_{camera_id}_{duration}"
        self._attr_name = f"Архив {label}"

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
            self._label, self._camera_id, start_time, self._duration
        )
        archive_url = await self._cameras_api.get_camera_archive(self._camera_id, start_time, self._duration)
        if archive_url:
            _LOGGER.info("Получена ссылка на архив для '%s': %s", self._label, archive_url)
            sensor = self.hass.data[self._config_entry.entry_id]["archive_link_sensors"].get(self._camera_id)
            if sensor:
                sensor.update_link(archive_url, comment=self._label)
        else:
            _LOGGER.error("Не удалось получить архив для камеры %s, период '%s'", self._camera_id, self._label)
