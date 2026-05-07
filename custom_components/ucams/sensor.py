import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .utils import DOMAIN, build_object_id

_LOGGER = logging.getLogger(__name__)

CALL_HISTORY_PAGE_SIZE = 20
CALL_HISTORY_SCAN_INTERVAL = timedelta(seconds=30)


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


def _parse_called_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # fromisoformat handles "2026-05-07T12:34:56+05:00" and Z-suffixed values on 3.11+
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_call_history_coordinator(hass, dom_api) -> DataUpdateCoordinator:
    async def _update():
        data = await dom_api.get_call_history(page_size=CALL_HISTORY_PAGE_SIZE)
        if isinstance(data, dict):
            return data.get("results") or []
        if isinstance(data, list):
            return data
        return []

    return DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="ucams_call_history",
        update_method=_update,
        update_interval=CALL_HISTORY_SCAN_INTERVAL,
    )


async def async_setup_entry(hass, config_entry, async_add_entities):
    dom_api = hass.data[config_entry.entry_id]["dom_api"]
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]

    sensors: list = []

    coordinator = _build_call_history_coordinator(hass, dom_api)
    await coordinator.async_config_entry_first_refresh()
    hass.data[config_entry.entry_id]["call_history_coordinator"] = coordinator

    try:
        skud_list = await dom_api.get_shared_skud()
    except Exception as err:
        _LOGGER.warning("Failed to fetch skud list for call history: %s", err)
        skud_list = []
    for skud_info in skud_list:
        camera_id = skud_info.get("cctv_number")
        if not camera_id:
            # No CCTV number → can't tie history rows to this skud, skip.
            continue
        camera_info = await cameras_api.get_camera_info(camera_id)
        device_name = (
            cameras_api.build_device_name(camera_info["title"])
            if camera_info
            else cameras_api.build_device_name(
                f"{skud_info.get('string_view', 'skud')}_{skud_info['id']}"
            )
        )
        sensors.append(
            LastCallSensor(
                coordinator,
                config_entry.entry_id,
                camera_id,
                device_name,
            )
        )

    try:
        contracts = await dom_api.get_all_contracts()
    except Exception as err:
        _LOGGER.warning("Failed to fetch contracts: %s", err)
        contracts = {"status": "error"}

    if contracts.get("status") == "ok":
        for contract in contracts["detail"]["contracts"]:
            contract_id = contract["contract_id"]
            billing_id = contract["billing_id"]
            try:
                details = await dom_api.get_contract_details(contract_id, billing_id)
            except Exception as err:
                _LOGGER.warning("Failed to fetch contract %s details: %s", contract_id, err)
                continue
            for detail in details["detail"]:
                sensors.append(ContractDetailSensor(hass, detail))
                for service in detail["services"]:
                    sensors.append(ServiceDetailSensor(hass, contract, service))

    cameras_info = hass.data[config_entry.entry_id].get("cameras_info") or {}
    for camera in cameras_info.values():
        camera_id = camera["id"]
        device_name = camera["title"]
        archive_sensor = ArchiveLinkSensor(config_entry.entry_id, camera_id, device_name)
        sensors.append(archive_sensor)

        hass.data[config_entry.entry_id].setdefault("archive_link_sensors", {})[camera_id] = (
            archive_sensor
        )

    async_add_entities(sensors)


class ContractDetailSensor(SensorEntity):
    def __init__(self, hass, detail):
        self.hass = hass
        self.detail = detail
        self._attr_name = f"Договор {detail['contract_title']}"
        self._attr_unique_id = f"contract_{detail['contract_id']}"
        self._attr_native_value = detail["balance"]["current"]

    @property
    def extra_state_attributes(self):
        detail = self.detail
        balance = detail["balance"]
        contract_address = detail["contract_address"]

        address = ", ".join(
            filter(
                None,
                [
                    contract_address.get("city"),
                    contract_address.get("street"),
                    contract_address.get("house"),
                    contract_address.get("flat"),
                ],
            )
        )
        return {
            "Адрес": address,
            "Входное сальдо": balance["input_saldo"],
            "Начисления": balance["charge"],
            "Платеж": balance["payment"],
            "Текущий баланс": balance["current"],
            "Выходное сальдо": balance["output_saldo"],
            "Рекомендуемая оплата": balance["recommended"],
            "Лимит": balance["limit"],
            "Дата окончания": _fmt_ts(balance.get("expiry_date")),
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"contract_{self.detail['contract_id']}")},
            "name": f"Договор {self.detail['contract_title']}",
            "manufacturer": "Ufanet",
        }


class ServiceDetailSensor(SensorEntity):
    def __init__(self, hass, contract, service):
        self.hass = hass
        self.contract = contract
        self.service = service
        self._attr_name = f"Услуга {service['service_title_name']}"
        self._attr_unique_id = f"service_{contract['contract_id']}_{service['service_id']}"
        self._attr_native_value = service["service_status"]

    @property
    def extra_state_attributes(self):
        tariff = self.service.get("tariff", {})
        return {
            "Название": self.service["service_title_name"],
            "Статус": self.service["service_status"],
            "Дата платежа": _fmt_ts(self.service.get("period_end")),
            "Стоимость": self.service["cost"],
            "Тариф": tariff.get("title", ""),
            "Скорость": tariff.get("speed", ""),
            "Активация": self.service["date_from"],
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"contract_{self.contract['contract_id']}")},
            "name": f"Договор {self.contract['title']}",
            "manufacturer": "Ufanet",
        }


class ArchiveLinkSensor(SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, config_entry_id: str, camera_id: str, device_name: str):
        self._config_entry_id = config_entry_id
        self._camera_id = camera_id
        self._device_name = device_name
        self._attr_name = f"Archive Link {device_name}"
        self._attr_unique_id = f"archive_link_sensor_{camera_id}"
        self._state = None
        self._attrs = {}

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attrs

    def update_link(self, link: str, comment: str | None = None):
        self._state = "available"
        self._attrs["archive_url"] = link
        self._attrs["updated"] = datetime.now().isoformat()
        if comment:
            self._attrs["comment"] = comment
        else:
            self._attrs["comment"] = "Archive generated"
        self.async_write_ha_state()

    async def async_update(self):
        pass

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self._config_entry_id}_{self._camera_id}")},
            "name": self._device_name,
            "manufacturer": "Ufanet",
        }


class LastCallSensor(CoordinatorEntity, SensorEntity):
    """Per-intercom sensor: state is the camera's most recent call timestamp."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        camera_id: str,
        device_name: str,
    ):
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self._camera_id = camera_id
        self._device_name = device_name
        self._attr_name = f"Последний звонок {device_name}"
        self._attr_unique_id = f"ucams_last_call_{camera_id}"
        self.entity_id = f"sensor.{build_object_id(device_name, f'last_call_{camera_id}')}"

    @property
    def _matching_calls(self) -> list[dict]:
        items = self.coordinator.data or []
        return [item for item in items if str(item.get("camera_number")) == str(self._camera_id)]

    @property
    def native_value(self) -> datetime | None:
        calls = self._matching_calls
        if not calls:
            return None
        return _parse_called_at(calls[0].get("called_at"))

    @property
    def extra_state_attributes(self):
        calls = self._matching_calls
        return {
            "calls": [
                {
                    "uuid": item.get("uuid"),
                    "called_at": item.get("called_at"),
                    "address": item.get("address"),
                    "porch": item.get("porch"),
                    "flat": item.get("flat"),
                    "camera_number": item.get("camera_number"),
                    "skud_mac": item.get("skud_mac"),
                    "timezone": item.get("timezone"),
                }
                for item in calls
            ],
            "count": len(calls),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self._config_entry_id}_{self._camera_id}")},
            "name": self._device_name,
            "manufacturer": "Ufanet",
        }
