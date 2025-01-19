from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from .utils import DOMAIN


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
            filter(None, [contract_address.get("city"), contract_address.get("street"), contract_address.get("house"), contract_address.get("flat")])
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
