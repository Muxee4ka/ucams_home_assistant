# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
pip install -e ".[dev]"        # install runtime + dev tooling (ruff, pytest, HA, etc.)
ruff check .                   # lint
ruff format --check .          # format check (CI runs both; pre-commit auto-fixes)
pytest                         # full test suite
pytest tests/test_ucams.py::test_get_cameras_info_paginates    # single test
docker compose up -d           # boot HA at :8123 with this integration mounted live
```

CI (`.github/workflows/`) runs ruff + pytest on Python 3.12 and 3.13, plus `hassfest` and the HACS validator. Match those locally before pushing — ruff is the source of truth for style (`pyproject.toml [tool.ruff]`, line length 100, py312 target). `homeassistant`/`aiohttp` are dev-only deps; runtime deps are declared in `custom_components/ucams/manifest.json` (HA installs them itself).

## High-level architecture

Single HACS integration under `custom_components/ucams/` that surfaces Ufanet's video surveillance + intercoms in Home Assistant.

### Two-tier API client

The integration talks to two separate Ufanet backends, and the second depends on the first:

- **`ufanet.DomApi`** (`ufanet.py`) — the "dom.ufanet.ru" portal. Authenticates with contract+password, exposes contracts, skud (intercoms), call history. Uses `JWT <token>` auth header.
- **`ucams.UcamsApi`** (`ucams.py`) — the per-region cams server. Its base URL (`cams_server`) is **discovered at runtime** from `DomApi.get_contract_info()` (`isp_org.cams_server.url`). Auth is bootstrapped by copying `DomApi`'s `Authorization` header and POSTing to `api/v0/auth/?ttl=…` to receive its own bearer token. Uses `Bearer <token>` thereafter.

Both clients track expiry via the JWT `exp` claim (`utils.decode_token`, with a manual base64 fallback for non-standard tokens) and refresh when within `TOKEN_REFRESH_BUFFER` (300s) of expiry. `UcamsApi.get_authenticated_session` also re-auths if the upstream `DomApi` token has expired, since the cams token was minted from it.

`UcamsApi.get_cameras_info` paginates `api/v0/cameras/my/` and pre-builds the per-camera RTSP / WebSocket / screenshot URLs (each carries a per-camera `token_l`). `get_camera_url` re-fetches the camera list when `token_l` is near expiry.

### Entry lifecycle (`__init__.py`)

`async_setup_entry` instantiates both API clients, calls `get_cameras_info()` (so platforms can read it synchronously from `hass.data`), forwards setup to all platforms, then runs `_assign_areas_by_address` and `_async_register_services`. **Auth/setup errors are intentionally allowed to propagate** — `DomApi` raises `ConfigEntryAuthFailed` / `ConfigEntryNotReady` itself, and silencing those would hide real failures from the user.

`hass.data[entry_id]` holds the shared bag every platform reads from:

```
{ "cameras_api", "dom_api", "cameras_info",
  "camera_entities" (set by camera.py),
  "archive_link_sensors" (set by sensor.py),
  "call_history_coordinator" (set by sensor.py) }
```

Services (`ucams.snapshot`, `ucams.get_archive`) are registered once globally and only removed when the last `ucams` entry unloads (`_ucams_entries`). The snapshot service falls back to `assets/no_snapshot.png` when the RTSP grab fails so automations always get a file.

### Platforms (six)

`PLATFORMS = [IMAGE, CAMERA, SWITCH, SENSOR, BUTTON, GEO_LOCATION]`. They cluster around shared **device identifiers** of the form `(DOMAIN, f"{entry_id}_{camera_id}")` so the camera, image, switch (intercom), archive button, archive-link sensor, last-call sensor, and geo_location entity all show up under one HA device. Skud entries without a `cctv_number` get their own device id keyed by `skud_id` instead.

- **`camera.Ucams`** — `CameraEntityFeature.STREAM` only, no still endpoint upstream. `use_stream_for_stills=True` and `handle_snapshot_from_rtsp` calls `async_get_image()` (the HA stream component path) — **not** `entity.async_camera_image()`. A periodic `async_track_time_interval` keeps the active stream's source URL in sync as `token_l` rotates.
- **`image.UcamsCameraImageEntity`** — pulls `screenshot_domain` JPEGs on a `CONF_CAMERA_IMAGE_REFRESH_INTERVAL` cadence by bumping `_attr_image_last_updated` (which causes HA to re-call `async_image`).
- **`switch.DomUfanetSwitchEntity`** — momentary intercom-open switch; `async_turn_on` calls `dom_api.open_skud(skud_id)` and schedules an auto-off task at `skud_info["timeout"]`.
- **`sensor.py`** — three classes:
  - `LastCallSensor` (timestamp device class, `CoordinatorEntity`) — backed by a 30s `DataUpdateCoordinator` polling `api/v1/skuds/call-history/`. One sensor per skud that has a `cctv_number`.
  - `ArchiveLinkSensor` — passive sensor; `update_link()` is called from the snapshot/archive flow and from `ArchiveButton`.
  - `ContractDetailSensor` / `ServiceDetailSensor` — billing data from `get_all_contracts` / `get_contract_details`.
- **`button.ArchiveButton`** — three preset durations (5min / 1h / 5h) per camera; presses request an archive URL and write it to the matching `ArchiveLinkSensor`.
- **`geo_location.UcamsLocation`** — only created for cameras that report lat/lon. Distance to home is computed once at init (cameras don't move).

### Conventions worth knowing

- **Entity ids are slugified through `utils.transliterate_ru` + `build_object_id`.** The `_RU_TRANSLIT` mapping in `utils.py` is **vendored verbatim from `transliterate>=1.10`** to preserve every entity_id that existed before the dependency was dropped — don't "improve" it.
- **`parse_house_area`** strips `"г. <city>, "` prefix and `", п.<n>"` porch suffix from Ufanet addresses, yielding a `"Street, House"` HA area name. Areas are force-assigned post-setup in `_assign_areas_by_address` because `suggested_area` only fires on a device's first registration.
- **`api/v0/cameras/my/` rejects unknown `fields`** with HTTP 400. Notably `"house"` is not valid — addresses are parsed locally instead. Add fields to that request only after confirming they're accepted.
- **Hard-to-reverse / setup-time decisions live in `__init__.py`'s docstrings/comments**; trust those over the wider HA docs when they conflict.

### Tests (`tests/`)

`pytest-asyncio` in `auto` mode. `conftest.py` hands out a `MagicMock(spec=HomeAssistant)` because `UcamsApi`/`DomApi` only store `hass` and never call into it — keeping these as pure unit tests, no event loop boot. HTTP is mocked with `aioresponses`. There are no HA-fixture integration tests yet; if you add one, use `pytest-homeassistant-custom-component` (already in dev deps).

### Local HA dev (`docker-compose.yml`)

`docker compose up -d` boots `ghcr.io/home-assistant/home-assistant:stable` with `./ha_config` mounted at `/config` and the integration mounted live at `/config/custom_components/ucams`. Edit-and-reload works: restart the container or use HA's "reload custom integrations". `ha_config/configuration.yaml` already enables debug logging for `custom_components.ucams` and the stream component, which is what you want when diagnosing camera/RTSP issues.
