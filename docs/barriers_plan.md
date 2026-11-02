# План доработки: шлагбаумы (barriers)

> Статус на 02.11.2026. Источник знаний — реверс APK «Мой умный дом» v4.3.0
> (appId 250444479, RuStore) + подтверждение живого аккаунта от просителя.

## Что выяснено

Шлагбаумы живут не на dom.ufanet.ru, а на cams_server (ucams.ufanet.ru) —
поэтому их нет в skud/shared. В терминах приложения это «devices».

| действие приложения | вызов |
|---|---|
| список шлагбаумов | `POST /api/v0/devices/` c `{"fields":["id","ident","title","is_active","camera_numbers"],"extra":["camera","module"],"page":N}` |
| открыть | `POST /api/v0/devices/cmd/` c `{"device_id":<int>,"action":"open","payload":{"module_id":<int|null>}}` |
| временный доступ (шаринг) | `/api/v0/devices/access/links/` + `/create`, `/delete` |
| избранное | `/api/v0/devices/fav/`, `/fav/add/`, `/fav/delete/`, `/fav/order/` |

Детали, снятые с байткода (NetworkHelper.openBarrier/loadBarriers, ICamsAPI
Retrofit-аннотации, UcamsManager → ProviderModel.getCams_server()):

- Base URL — cams_server из contract info (`isp_org.cams_server.url`);
  интеграция его уже дискаверит в `_ensure_cams_session()`, авторизация —
  существующий Bearer cams-токен.
- Ответ списка: `{count, page, results:[{id, title, isActive, cameraNumbers[],
  moduleIds[], emergencyVehicleAccess, cameraCount}], extra:{camera:[…],
  module:[…]}}`; пагинация по `page`.
- У устройства 1+ модулей (`module_ids`): если один — payload без `module_id`
  (enum DEVICE), если несколько — конкретный `module_id` (enum MODULE).
- Побочно закрыт вопрос по pcap: QUIC/UDP-443 в капчах нет, новых хостов нет —
  расшифровка не понадобилась.

Временный доступ (payload'ы сняты с байткода):

- Список ссылок → `POST /api/v0/devices/access/links/`:
  ```json
  {"page":1, "page_size":N,
   "order_by":[{"field":"id","direction":"asc"}],
   "fields":["id","link","device_id","title","device_title","end_date"],
   "filters":[{"field":"device_id","lookup":"in","argument":[<device_id>],"exclude":false}],
   "extra":[], "search":""}
  ```
- Создание (сутки) → `POST /api/v0/devices/access/links/create`:
  ```json
  {"module_id":<int|null>, "device_id":<int>, "start_date":"<ISO>", "end_date":"<ISO>", "title":"<title>"}
  ```
- `GET/POST /api/v0/devices/access/links/action/?link=…` — публичный браузерный
  эндпоинт (в APK его нет — гость открывает без авторизации). Ссылка от
  просителя подтвердила хост (ucams.ufanet.ru = cams_server) и семейство
  `/devices/access/links/*`.

## Фаза 1 — открытие шлагбаума (основная)

| # | Задача | Файл | Статус |
|---|---|---|---|
| 1.1 | `get_barriers()`: пагинированный `POST /api/v0/devices/` через `_ensure_cams_session()`, тело 1:1 как у приложения (fields + extra + page), loop по страницам до пустого `results` (safety-cap 50). Маппинг ключей толерантный (camel/snake). Нормализация: `{id, title, is_active, camera_numbers, module_ids, emergency_vehicle_access}` | `ucams.py` | ✅ |
| 1.2 | `open_barrier(device_id, module_id=None)`: `POST /api/v0/devices/cmd/`; `module_id` в payload только если передан | `ucams.py` | ✅ |
| 1.3 | `_post_cams` отрефакторен на общий `_post_cams_json(session, url, payload)` (401-retry сохранён) | `ucams.py` | ✅ |
| 1.4 | `UcamsBarrierSwitchEntity`: momentary-свитч, `async_turn_on` → `open_barrier(...)`, авто-off без настраиваемого таймера (шлагбаум закрывается сам) | `switch.py` | ✅ |
| 1.5 | `module_id`: один модуль → не передаём (DEVICE-режим приложения); несколько → первый + warning в лог (по свитчу на модуль — после подтверждения формата живым аккаунтом) | `switch.py` | ✅ |
| 1.6 | `is_active=False` → сущность создаётся disabled (`entity_registry_enabled_default=False`) | `switch.py` | ✅ |
| 1.7 | Отдельное HA-устройство `(DOMAIN, f"{entry_id}_barrier_{id}")`, имя — русское `title` как есть (конвенция проекта: device name несёт последняя платформа) | `switch.py` | ✅ |
| 1.8 | `_async_load_barriers(...)` после камер: non-fatal (ошибка → warning, `[]`; камерам не мешает; пустой список = нет сущностей). Результат в bag под ключом `barriers_info` | `__init__.py` | ✅ |
| 1.9 | Тесты: пагинация `get_barriers`, точное тело `open_barrier` (с/без module_id), camelCase-маппинг, setup свитчей (количество, momentary, disabled при `is_active=False`, multi-module) | `tests/` | ✅ |
| 1.10 | README (раздел «Шлагбаумы»), bump 0.9.0, ruff + pytest, conventional commits: `feat: add barrier devices discovery`, `feat: barrier open switches`, `test/docs: …` | — | ✅ |

Опция конфига не нужна: у кого шлагбаума нет — пустой список, ничего не создаётся.

## Фаза 2 — временный доступ (после работающего открытия)

- Сервис `ucams.create_barrier_link` (сутки/произвольное окно): тело из раздела
  «Что выяснено» (`module_id` nullable, ISO-даты, title).
- Сервис `ucams.delete_barrier_link` + список активных ссылок как response
  сервиса (по payload списка ссылок).
- Возврат ссылки в `attributes`/response — чтобы отдавать гостям из уведомления.

## Полевая проверка (закрывает фазу 1)

1. Отдать просителю ветку/beta — список шлагбаумов должен появиться автоматически.
2. Его лог (debug для `custom_components.ucams`) закроет вопрос camel/snake
   ключей и кода ответа `/cmd/`.
3. Если 401/403 на `/cmd/` — вернуться к pcap-ветке (уверенность высокая: тот
   же Bearer, что у городских камер).
4. Если живой ответ `/devices/` окажется snake_case — ничего не меняем,
   маппинг уже толерантный; сверить только состав полей.
