"""Tests for the utils module."""

import base64
import json

from custom_components.ucams.utils import all_cameras_info, decode_token, short_address

# Header { "typ": "JWT", "alg": "HS256" }, payload { "exp": 1850000000, "u": "x" }
JWT_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJleHAiOjE4NTAwMDAwMDAsInUiOiJ4In0."
    "abcdef-fakesignature_-"
)


def test_decode_token_returns_payload_for_valid_jwt():
    decoded = decode_token(JWT_TOKEN)
    assert decoded["exp"] == 1850000000
    assert decoded["u"] == "x"


def test_decode_token_falls_back_to_raw_base64():
    """Some Ufanet endpoints return tokens whose first segment is plain base64
    JSON without the rest of a JWT structure — make sure that still parses."""
    payload = {"exp": 1700000000}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    not_quite_jwt = f"{encoded}.garbage"

    assert decode_token(not_quite_jwt) == payload


def test_decode_token_returns_empty_dict_on_garbage():
    assert decode_token("not-a-token") == {}
    assert decode_token("") == {}


def test_decode_token_no_longer_raises():
    """Regression: the old bare-except path could re-raise on b64decode of the
    second branch. New code must always return a dict, never raise."""
    # b64-decodable but not JSON
    raw = base64.urlsafe_b64encode(b"plain text not json").rstrip(b"=").decode()
    assert decode_token(f"{raw}.x.y") == {}


def test_short_address_keeps_street_and_house():
    """City prefixes come with and without the dot; both must fall away."""
    assert (
        short_address("г Нижний Новгород, ул Максима Горького, д 262")
        == "ул Максима Горького, д 262"
    )
    assert short_address("г. Уфа, ул Ленина, д 1, п.2") == "ул Ленина, д 1"
    assert short_address("ул Медицинская, д 15 к 3") == "ул Медицинская, д 15 к 3"


def test_short_address_handles_empty_input():
    assert short_address(None) is None
    assert short_address("") is None
    assert short_address(" , ") is None


def test_all_cameras_info_appends_public_cameras():
    """Platforms showing both sources read own cameras first, then city ones."""
    entry_data = {
        "cameras_info": {"a": {"id": "a"}},
        "public_cameras_info": {"b": {"id": "b"}},
    }
    assert [cam["id"] for cam in all_cameras_info(entry_data)] == ["a", "b"]


def test_all_cameras_info_without_public_bag():
    """Entries created before city cameras existed have no public bag at all."""
    assert all_cameras_info({"cameras_info": {"a": {"id": "a"}}}) == [{"id": "a"}]
    assert all_cameras_info({}) == []
