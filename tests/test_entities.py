"""Entity naming, ids and device names across platforms.

HA 2026.11 builds every displayed name as "<device> <entity>", so entities name
only themselves (`has_entity_name`) and every platform registering a device has
to pass the same device name. Entity ids are pinned so new entities keep the
form existing ones have, instead of HA deriving them from the Russian names.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.ucams as ucams_init
from custom_components.ucams.button import ArchiveButton
from custom_components.ucams.geo_location import UcamsLocation
from custom_components.ucams.sensor import (
    ArchiveLinkSensor,
    ContractDetailSensor,
    LastCallSensor,
    ServiceDetailSensor,
)

CAMERA = {
    "id": "1762776185KFV40",
    "title": "1, Камера 4",
    "address": "г. Нижний Новгород, Медицинская, 15к3",
    "latitude": 56.3,
    "longitude": 43.9,
}
CONTRACT = {"contract_id": 16386824, "billing_id": 1, "title": "52046469"}
DETAIL = {
    "contract_id": 16386824,
    "contract_title": "52046469",
    "balance": {"current": 120.5},
}
SERVICE = {"service_id": 540531, "service_title_name": "Видеонаблюдение", "service_status": "on"}


def _cameras_api():
    from custom_components.ucams.ucams import UcamsApi

    api = MagicMock()
    entry = SimpleNamespace(data={"name": "Ucams"})
    api.build_device_name = lambda title: UcamsApi.build_device_name(
        SimpleNamespace(config_entry_name=entry.data["name"]), title
    )
    api.build_display_name = lambda camera: camera["title"]
    return api


def test_archive_buttons_are_translated_and_keep_their_entity_ids():
    entry = SimpleNamespace(entry_id="E1")
    button = ArchiveButton(
        MagicMock(),
        entry,
        _cameras_api(),
        CAMERA,
        300,
        "archive_5_min",
        "за 5 минут",
        "arkhiv_last_5_min",
    )

    assert button.has_entity_name
    assert button.translation_key == "archive_5_min"
    assert button.unique_id == "E1_archive_button_1762776185KFV40_300"
    # The form buttons already had before names were translated.
    assert button.entity_id == "button.ucams_1_kamera_4_arkhiv_last_5_min"
    assert button.device_info["name"] == "1, Камера 4"


def test_archive_link_sensor_keeps_the_readme_entity_id():
    sensor = ArchiveLinkSensor("E1", CAMERA["id"], CAMERA["title"])

    assert sensor.has_entity_name
    assert sensor.translation_key == "archive_link"
    assert sensor.entity_id == "sensor.archive_link_1_kamera_4"
    assert sensor.unique_id == "E1_archive_link_sensor_1762776185KFV40"
    assert sensor.device_info["name"] == "1, Камера 4"


def test_last_call_sensor_names_itself_only():
    sensor = LastCallSensor(
        MagicMock(data=[]), "E1", CAMERA["id"], "Ucams.1, kamera 4", "1, Камера 4"
    )

    assert sensor.has_entity_name
    assert sensor.translation_key == "last_call"
    assert sensor.entity_id == "sensor.ucams_1_kamera_4_last_call_1762776185kfv40"
    assert sensor.unique_id == "E1_ucams_last_call_1762776185KFV40"
    assert sensor.device_info["name"] == "1, Камера 4"


def test_contract_sensors_agree_on_the_device_name():
    """Balance and services register one device; a different name from each
    used to flip it on every setup."""
    balance = ContractDetailSensor(MagicMock(), "E1", CONTRACT, DETAIL)
    service = ServiceDetailSensor(MagicMock(), "E1", CONTRACT, SERVICE)

    assert balance.device_info == service.device_info
    assert balance.device_info["name"] == "Договор 52046469"
    assert balance.translation_key == "balance"
    assert balance.entity_id == "sensor.dogovor_52046469"
    assert balance.unique_id == "E1_contract_16386824"
    assert service.name == "Видеонаблюдение"
    assert service.entity_id == "sensor.usluga_videonabliudenie"
    assert service.unique_id == "E1_service_16386824_540531"


def test_geo_location_takes_the_device_name():
    hass = SimpleNamespace(config=SimpleNamespace(latitude=56.3, longitude=43.9))
    entity = UcamsLocation(hass, SimpleNamespace(entry_id="E1"), _cameras_api(), CAMERA)

    assert entity.has_entity_name and entity.name is None
    # What the old name-derived id looked like for contract cameras.
    assert entity.entity_id == "geo_location.ucams_1_kamera_4"
    assert entity.unique_id == "E1_geo-1762776185KFV40"


@pytest.mark.asyncio
async def test_migrate_entry_scopes_unique_ids(monkeypatch):
    """1.1 → 1.2 prefixes ids with the entry_id, once."""
    registry = [
        SimpleNamespace(unique_id="archive_link_sensor_CAM"),
        SimpleNamespace(unique_id="E1_camera-CAM"),
    ]
    renamed = {}

    async def fake_migrate(hass, entry_id, callback):
        assert entry_id == "E1"
        for entry in registry:
            if (update := callback(entry)) is not None:
                renamed[entry.unique_id] = update["new_unique_id"]

    monkeypatch.setattr(ucams_init.er, "async_migrate_entries", fake_migrate)
    hass = MagicMock()
    entry = SimpleNamespace(entry_id="E1", version=1, minor_version=1)

    assert await ucams_init.async_migrate_entry(hass, entry) is True
    assert renamed == {"archive_link_sensor_CAM": "E1_archive_link_sensor_CAM"}
    hass.config_entries.async_update_entry.assert_called_once_with(entry, minor_version=2)


@pytest.mark.asyncio
async def test_migrate_entry_skips_current_and_refuses_newer(monkeypatch):
    migrate = AsyncMock()
    monkeypatch.setattr(ucams_init.er, "async_migrate_entries", migrate)
    hass = MagicMock()

    current = SimpleNamespace(entry_id="E1", version=1, minor_version=2)
    assert await ucams_init.async_migrate_entry(hass, current) is True
    migrate.assert_not_called()

    newer = SimpleNamespace(entry_id="E1", version=2, minor_version=1)
    assert await ucams_init.async_migrate_entry(hass, newer) is False
