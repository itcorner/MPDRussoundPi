"""Validation of the agent's audio hardware description (``GET /v1/audio``).

The Status page has no login, so the response is rebuilt from an allowlist with
exact keys, strict types and bounds. Only model-level facts are accepted: card
profile, USB vendor and product ID, sink ids, descriptions and channel names.
"""

from __future__ import annotations

import re
from typing import Any, cast

HEX_ID = re.compile(r"[0-9a-f]{4}\Z")
IDENTIFIER = re.compile(r"[\w-]{1,64}\Z", re.ASCII)
OUTPUT_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
CHANNEL = re.compile(r"[A-Z]{2,3}\Z")
CONFIG_REVISION = re.compile(r"[a-f0-9]{64}\Z")
MAX_TEXT = 64
MAX_DEVICES = 8
MAX_SINKS = 16
MAX_OUTPUTS = 32
MAX_CHANNELS = 8


class Invalid(ValueError):
    """The response is outside the contract."""


def _keys(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(cast(dict[str, Any], value)) != keys:
        raise Invalid("object")
    return cast(dict[str, Any], value)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or not all(32 <= ord(c) < 127 for c in value):
        raise Invalid("text")
    return value


def _channels(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or len(cast(list[Any], value)) > MAX_CHANNELS:
        raise Invalid("channels")
    result: list[str] = []
    for item in cast(list[Any], value):
        if not isinstance(item, str) or CHANNEL.fullmatch(item) is None:
            raise Invalid("channel")
        result.append(item)
    return result


def _usb(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    fields = _keys(value, {"vendor", "product"})
    if not all(isinstance(fields[key], str) and HEX_ID.fullmatch(fields[key]) for key in fields):
        raise Invalid("usb")
    return {"vendor": fields["vendor"], "product": fields["product"]}


def _sink(value: object) -> dict[str, Any]:
    fields = _keys(value, {"id", "description", "card_channels", "output_channels", "remix"})
    if not isinstance(fields["id"], str) or IDENTIFIER.fullmatch(fields["id"]) is None or not isinstance(fields["remix"], bool):
        raise Invalid("sink")
    return {
        "id": fields["id"], "description": _text(fields["description"]),
        "card_channels": _channels(fields["card_channels"]),
        "output_channels": _channels(fields["output_channels"]), "remix": fields["remix"],
    }


def _device(value: object) -> dict[str, Any]:
    fields = _keys(value, {"profile", "usb", "sinks"})
    sinks = fields["sinks"]
    if not isinstance(sinks, list) or len(cast(list[Any], sinks)) > MAX_SINKS:
        raise Invalid("sinks")
    return {"profile": _text(fields["profile"]), "usb": _usb(fields["usb"]), "sinks": [_sink(item) for item in cast(list[Any], sinks)]}


def _output(value: object) -> dict[str, str]:
    fields = _keys(value, {"id", "sink"})
    if (not isinstance(fields["id"], str) or OUTPUT_ID.fullmatch(fields["id"]) is None
            or not isinstance(fields["sink"], str) or IDENTIFIER.fullmatch(fields["sink"]) is None):
        raise Invalid("output")
    return {"id": fields["id"], "sink": fields["sink"]}


def validate_audio_response(response: dict[str, Any]) -> dict[str, Any]:
    """Sanitized audio description; raises ``Invalid`` for anything outside the contract."""
    revision = response.get("config_revision")
    devices, outputs = response.get("devices"), response.get("outputs")
    if (not isinstance(revision, str) or CONFIG_REVISION.fullmatch(revision) is None
            or not isinstance(devices, list) or len(cast(list[Any], devices)) > MAX_DEVICES
            or not isinstance(outputs, list) or len(cast(list[Any], outputs)) > MAX_OUTPUTS):
        raise Invalid("document")
    return {
        "config_revision": revision,
        "devices": [_device(item) for item in cast(list[Any], devices)],
        "outputs": [_output(item) for item in cast(list[Any], outputs)],
    }
