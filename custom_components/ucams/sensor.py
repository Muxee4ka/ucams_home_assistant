import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .utils import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    dom_api = hass.data[config_entry.entry_id]["dom_api"]
    contracts = await dom_api.get_all_contracts()

    sensors = []
    if contracts["status"] != "ok":
        return
    for contract in contracts["detail"]["contracts"]:
        contract_id = contract["contract_id"]
        billing_id = contract["billing_id"]
        details = await dom_api.get_contract_details(contract_id, billing_id)

        for detail in details["detail"]:
            sensors.append(ContractDetailSensor(hass, detail))
            for service in detail["services"]:
                sensors.append(ServiceDetailSensor(hass, contract, service))

    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = hass.data[config_entry.entry_id]["cameras_info"]
    if not cameras_api:
        _LOGGER.error("cameras_api не найден")
        return
    if not cameras_info:
        _LOGGER.error("cameras_info не найден")
        return
    for camera in cameras_info.values():
        camera_id = camera["id"]
        device_name = camera["title"]
        archive_sensor = ArchiveLinkSensor(config_entry.entry_id, camera_id, device_name)
        sensors.append(archive_sensor)

        hass.data[config_entry.entry_id].setdefault("archive_link_sensors", {})[camera_id] = archive_sensor

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
                    contract_address.get("flat")
                ]
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
            "Дата окончания": datetime.fromtimestamp(balance["expiry_date"] or 0).strftime("%d.%m.%Y %H:%M:%S"),
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
            "Дата платежа": datetime.fromtimestamp(self.service["period_end"] or 0).strftime("%d.%m.%Y %H:%M:%S"),
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

    def update_link(self, link: str, comment: str = None):
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