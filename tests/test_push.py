"""Tests for the opt-in FCM push path and the call event it speeds up."""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant.util import dt as dt_util

from custom_components.ucams import coordinator as coordinator_mod
from custom_components.ucams import push as push_mod
from custom_components.ucams.coordinator import async_refresh_for_call
from custom_components.ucams.event import IntercomCallEvent

FCM_URL = "https://dom.example.com/api/v0/fcm/"
FRESH_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjE4NTAwMDAwMDB9.fake-sig"


async def test_register_push_device_posts_app_payload(dom_api):
    dom_api.token = FRESH_JWT
    dom_api.token_expiration = 1850000000
    with aioresponses() as m:
        m.post(FCM_URL, payload={"status": "ok"})
        await dom_api.register_push_device("fcm-token", "Home Assistant_x", "Home Assistant")
        (req,) = m.requests[("POST", next(k[1] for k in m.requests))]
    assert req.kwargs["json"] == {
        "token": "fcm-token",
        "device_id": "Home Assistant_x",
        "title": "Home Assistant",
        "application": "ru.ufanet.smarthome",
        "os": 0,
        "token_type": 0,
    }


async def test_unregister_push_device_deletes_by_device_id(dom_api):
    dom_api.token = FRESH_JWT
    dom_api.token_expiration = 1850000000
    with aioresponses() as m:
        m.delete(FCM_URL, payload={"status": "ok"})
        await dom_api.unregister_push_device("Home Assistant_x")
        (req,) = next(iter(m.requests.values()))
    assert req.kwargs["json"] == {"device_id": "Home Assistant_x"}


def _listener():
    hass = MagicMock()
    on_call = AsyncMock()
    with patch.object(push_mod, "_store", return_value=MagicMock()):
        listener = push_mod.PushListener(hass, "entry", MagicMock(), on_call)
    listener._state = {"device_id": "Home Assistant_x", "persistent_ids": []}
    return listener, hass, on_call


def test_sip_push_triggers_on_call_with_push_time():
    listener, hass, on_call = _listener()
    push = {"data": {"reason": "sip", "time": "2026-09-21T21:38:28+05:00", "password": "x"}}
    listener._handle_push(push, "pid-1", None)

    hass.async_create_task.assert_called_once()
    # The mocked hass never runs it; close so it isn't reported as un-awaited.
    hass.async_create_task.call_args.args[0].close()
    on_call.assert_called_once()
    (called_at,) = on_call.call_args.args
    assert called_at.isoformat() == "2026-09-21T21:38:28+05:00"
    assert listener._state["persistent_ids"] == ["pid-1"]


def test_non_sip_push_is_ignored_but_acknowledged():
    listener, hass, on_call = _listener()
    listener._handle_push({"data": {"reason": "key_add"}}, "pid-1", None)
    hass.async_create_task.assert_not_called()
    on_call.assert_not_called()
    # Still remembered so the library won't hand it to us again.
    assert listener._state["persistent_ids"] == ["pid-1"]


def test_persistent_ids_are_capped():
    listener, _, _ = _listener()
    for i in range(push_mod.MAX_PERSISTENT_IDS + 5):
        listener._handle_push({"data": {}}, f"pid-{i}", None)
    ids = listener._state["persistent_ids"]
    assert len(ids) == push_mod.MAX_PERSISTENT_IDS
    assert ids[-1] == f"pid-{push_mod.MAX_PERSISTENT_IDS + 4}"


async def test_remove_registration_unregisters_and_forgets():
    store = MagicMock(
        async_load=AsyncMock(return_value={"registered": True, "device_id": "Home Assistant_x"}),
        async_remove=AsyncMock(),
    )
    dom_api = MagicMock(unregister_push_device=AsyncMock())
    with patch.object(push_mod, "_store", return_value=store):
        await push_mod.async_remove_registration(MagicMock(), "entry", dom_api)
    dom_api.unregister_push_device.assert_awaited_once_with("Home Assistant_x")
    store.async_remove.assert_awaited_once()


async def test_remove_registration_without_state_is_a_noop():
    store = MagicMock(async_load=AsyncMock(return_value=None), async_remove=AsyncMock())
    dom_api = MagicMock(unregister_push_device=AsyncMock())
    with patch.object(push_mod, "_store", return_value=store):
        await push_mod.async_remove_registration(MagicMock(), "entry", dom_api)
    dom_api.unregister_push_device.assert_not_called()
    store.async_remove.assert_not_called()


def _fake_coordinator(pages):
    """Coordinator whose data advances to the next page on each refresh."""
    coord = SimpleNamespace(data=[], refreshes=0)

    async def _refresh():
        coord.data = pages[min(coord.refreshes, len(pages) - 1)]
        coord.refreshes += 1

    coord.async_refresh = _refresh
    return coord


async def test_refresh_for_call_stops_once_history_has_it():
    called_at = dt_util.parse_datetime("2026-09-21T21:38:28+05:00")
    coord = _fake_coordinator([[], [{"called_at": "2026-09-21T21:38:28+05:00"}]])
    with patch.object(coordinator_mod.asyncio, "sleep", AsyncMock()):
        await async_refresh_for_call(coord, called_at)
    assert coord.refreshes == 2


async def test_refresh_for_call_gives_up_after_all_retries():
    called_at = dt_util.parse_datetime("2026-09-21T21:38:28+05:00")
    old = [{"called_at": "2026-09-21T13:05:32+05:00"}]
    coord = _fake_coordinator([old])
    with patch.object(coordinator_mod.asyncio, "sleep", AsyncMock()):
        await async_refresh_for_call(coord, called_at)
    assert coord.refreshes == len(coordinator_mod.PUSH_REFRESH_DELAYS)


def _call(uuid, camera="cam1", age=timedelta(seconds=5)):
    return {
        "uuid": uuid,
        "camera_number": camera,
        "called_at": (dt_util.utcnow() - age).isoformat(),
        "flat": "12",
    }


def _event(history):
    coord = MagicMock(data=history)
    entity = IntercomCallEvent(coord, "entry", "cam1", "Dom 1")
    entity._trigger_event = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity, coord


def test_event_does_not_replay_startup_history():
    entity, _ = _event([_call("a")])
    entity._handle_coordinator_update()
    entity._trigger_event.assert_not_called()


def test_event_fires_for_new_calls_oldest_first():
    entity, coord = _event([_call("a")])
    coord.data = [
        _call("c", age=timedelta(seconds=1)),
        _call("b", age=timedelta(seconds=20)),
        _call("x", camera="other"),
        _call("a"),
    ]
    entity._handle_coordinator_update()
    fired = [c.args[1]["uuid"] for c in entity._trigger_event.call_args_list]
    assert fired == ["b", "c"]
    assert entity._trigger_event.call_args.args[0] == "ring"

    # Same data again (e.g. the next poll) must not fire twice.
    entity._trigger_event.reset_mock()
    entity._handle_coordinator_update()
    entity._trigger_event.assert_not_called()


def test_event_skips_stale_catch_up_calls():
    entity, coord = _event([])
    coord.data = [_call("old", age=timedelta(hours=1))]
    entity._handle_coordinator_update()
    entity._trigger_event.assert_not_called()


@pytest.mark.parametrize(("auth_method", "unregisters"), [("password", True), ("phone", False)])
async def test_push_off_only_unregisters_password_accounts(auth_method, unregisters):
    from custom_components import ucams

    entry = SimpleNamespace(options={}, entry_id="entry")
    dom_api = SimpleNamespace(auth_method=auth_method)
    with patch.object(ucams, "async_remove_registration", AsyncMock()) as remove:
        await ucams._async_setup_push(MagicMock(), entry, dom_api, MagicMock())
    assert remove.await_count == (1 if unregisters else 0)


def _read_error_record(exc):
    try:
        raise exc
    except BaseException:
        import sys

        return logging.LogRecord(
            "firebase_messaging.fcmpushclient",
            logging.ERROR,
            __file__,
            1,
            "Unexpected exception during read\n",
            None,
            sys.exc_info(),
        )


@pytest.mark.parametrize(
    ("exc", "kept"),
    [
        (asyncio.IncompleteReadError(b"", 1), False),
        (ConnectionResetError(104, "reset"), False),
        (ValueError("bad frame"), True),
    ],
)
def test_routine_mcs_disconnects_are_muted(exc, kept):
    assert push_mod._DropRoutineDisconnects().filter(_read_error_record(exc)) is kept
