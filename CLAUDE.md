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

### API clients (hot path: dom only; cams_server lazy for archive)

Two backends, but the integration only contacts one of them on the hot path:

- **`ufanet.DomApi`** (`ufanet.py`) — `dom.ufanet.ru`. Authenticates with contract+password (`/api/v1/auth/auth_by_contract/`). Exposes contracts, skud (intercoms), call history, and the **flat camera list via `/api/v1/cctv`** (`get_cctv_list`). Uses `JWT <token>` auth header.
- **`ucams.UcamsApi`** (`ucams.py`) — wraps `DomApi` for the camera read path; only authenticates separately against the regional `cams_server` when **archive** or **public city cameras** are requested. Its `cams_server` URL is **discovered lazily** from `DomApi.get_contract_info()` (`isp_org.cams_server.url`) on the first archive call.

`utils.decode_token` decodes JWT payloads (with a manual base64 fallback for non-standard tokens) and `TOKEN_REFRESH_BUFFER` (300s) is the slack used everywhere expiry is checked.

`UcamsApi.get_cameras_info` calls `DomApi.get_cctv_list` (single round-trip, no pagination) and pre-builds per-camera RTSP / WebSocket / screenshot URLs from each item's `servers.{domain, screenshot_domain}` and `token_l` (live JWT, `t:"L"`). `get_camera_url` re-fetches when `token_l` is near expiry. `get_camera_image` goes through the dom session — no cams_server bootstrap needed for screenshots.

**Public «city» cameras (opt-in)** are the second cams_server path.
`UcamsApi.get_public_cameras_info` POSTs `/api/v0/cameras/search/` with
`public_cameras: true, user_cameras: false` — that returns every public camera
the ISP runs (~2000, all towns), each with a `token_l` minted for the account.
`query` filters server-side on title *and* address; the radius filter is local
(`filter_public_cameras`) because the API has no geo filter. Token refresh goes
through `/api/v0/cameras/this/` with the stored `numbers`, never a re-search, so
a camera can't vanish mid-session. **They are live-only** — `token_r`/`token_d`
come back `null`, so `get_camera_archive` refuses them before making a request.

`UcamsApi.get_camera_archive` is the only path that still hits cams_server: only `/api/v0/cameras/this/` issues `token_d` (archive download JWT, `t:"D"`, with embedded `ds`/`dd` start+duration). `_ensure_cams_session` lazily auths there using the dom JWT as bootstrap. `token_r` from `/api/v1/cctv` is **not** a substitute — empirically returns 403 against the archive endpoint.

### Entry lifecycle (`__init__.py`)

`async_setup_entry` instantiates both API clients, calls `get_cameras_info()` (so platforms can read it synchronously from `hass.data`), forwards setup to all platforms, then runs `_assign_areas_by_address` and `_async_register_services`. **Auth/setup errors are intentionally allowed to propagate** — `DomApi` raises `ConfigEntryAuthFailed` / `ConfigEntryNotReady` itself, and silencing those would hide real failures from the user.

`hass.data[entry_id]` holds the shared bag every platform reads from:

```
{ "cameras_api", "dom_api", "cameras_info", "public_cameras_info",
  "camera_entities" (set by camera.py),
  "archive_link_sensors" (set by sensor.py),
  "call_history_coordinator" (set by sensor.py) }
```

`public_cameras_info` is deliberately a **separate bag** from `cameras_info`:
only `camera`/`image`/`geo_location` merge the two (via `utils.all_cameras_info`).
Archive buttons, archive-link sensors and `_assign_areas_by_address` keep reading
`cameras_info` alone — city cameras have no archive, and pinning them to areas
would spawn one area per street. Their discovery failure is non-fatal by design
(`_async_load_public_cameras` logs and returns `{}`); the user's own cameras must
still come up.

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
- **Device names come from the *last* platform to register the device.** All
  platforms pass a `name` in `DeviceInfo` for the same identifiers, and
  `sensor.ArchiveLinkSensor` passes the **raw Russian** `camera["title"]` while
  camera/image pass the transliterated one — so contract-camera devices read
  Russian in the UI. City cameras have no archive sensor, so they need
  `UcamsApi.build_display_name` to get the same treatment. `build_device_name`
  stays the source of the **entity_id** slug for everyone; only the displayed
  name differs. `geo_location` pins `entity_id` explicitly for public cameras
  because it otherwise derives it from the (now Russian) name.
- **`short_address` vs `parse_house_area`.** `parse_house_area` (area names) only
  strips a dotted `"г. <city>, "` prefix; `/api/v1/cctv` and the public list both
  return the undotted `"г <city>, "` form, which it leaves alone — changing that
  would rename every existing area, so it stays as is. `short_address` (display
  names only) just keeps the trailing `"street, house"` pair and is prefix-format
  agnostic. City-camera titles get that suffix plus, on collision, a
  `#<last 4 of the camera number>` tag (`disambiguate_titles`), because Ufanet
  names most of them "Камера 1".
- **`parse_house_area`** strips `"г. <city>, "` prefix and `", п.<n>"` porch suffix from Ufanet addresses, yielding a `"Street, House"` HA area name. Areas are force-assigned post-setup in `_assign_areas_by_address` because `suggested_area` only fires on a device's first registration.
- **The legacy `cams_server/api/v0/cameras/my/` endpoint** (no longer used on the read path) strictly validates its `fields` array — `"house"` returned HTTP 400, which is why addresses are parsed locally. If you ever re-add that endpoint, every new field needs verifying in isolation before shipping.
- **Hard-to-reverse / setup-time decisions live in `__init__.py`'s docstrings/comments**; trust those over the wider HA docs when they conflict.

### Tests (`tests/`)

`pytest-asyncio` in `auto` mode. `conftest.py` hands out a `MagicMock(spec=HomeAssistant)` because `UcamsApi`/`DomApi` only store `hass` and never call into it — keeping these as pure unit tests, no event loop boot. HTTP is mocked with `aioresponses`. There are no HA-fixture integration tests yet; if you add one, use `pytest-homeassistant-custom-component` (already in dev deps).

### Local HA dev (`docker-compose.yml`)

`docker compose up -d` boots `ghcr.io/home-assistant/home-assistant:stable` with `./ha_config` mounted at `/config` and the integration mounted live at `/config/custom_components/ucams`. Edit-and-reload works: restart the container or use HA's "reload custom integrations". `ha_config/configuration.yaml` already enables debug logging for `custom_components.ucams` and the stream component, which is what you want when diagnosing camera/RTSP issues.
