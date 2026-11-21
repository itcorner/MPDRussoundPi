"""Model-level description of the audio hardware, for ``GET /v1/audio``.

Built from the already validated admin configuration with an allowlist: the
card profile, the USB vendor and product ID (a model identifier shared by every
unit of that model), and per sink its id, description and channel mapping.

Never copied: the device ``name`` (it can embed the USB serial number and bus
path), serial numbers, file paths, ports and anything else in the file.
"""

from __future__ import annotations

import re
from typing import Any, cast

from .audioidentity import HardwareSelector
from .outputconfig import Configuration

CHANNEL_PATTERN = re.compile(r"\[\s*([A-Z]{2,3}(?:\s+[A-Z]{2,3})*)\s*\]\Z")
SINK_ID_PATTERN = re.compile(r"[\w-]{1,64}\Z", re.ASCII)
MAX_TEXT = 64
MAX_DEVICES = 8
MAX_SINKS = 16


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if 32 <= ord(character) < 127).strip()
    return cleaned[:MAX_TEXT] or None


def _channels(value: object) -> list[str] | None:
    """``"[ FC LFE ]"`` becomes ``["FC", "LFE"]``; anything unusual becomes None."""
    if not isinstance(value, str):
        return None
    match = CHANNEL_PATTERN.fullmatch(value.strip())
    return match.group(1).split() if match else None


def describe_audio(configuration: Configuration) -> dict[str, Any]:
    """Allowlisted audio hardware facts of one configuration revision."""
    mappers = configuration.raw.get("outputmappers")
    entries = cast(dict[str, Any], mappers).get("audio_devices") if isinstance(mappers, dict) else None
    devices: list[dict[str, Any]] = []
    for device in (entries if isinstance(entries, list) else [])[:MAX_DEVICES]:
        if not isinstance(device, dict):
            continue
        fields = cast(dict[str, Any], device)
        usb: dict[str, str] | None = None
        selector = fields.get("hardware-selector")
        if isinstance(selector, dict):
            try:
                parsed = HardwareSelector.parse(cast(dict[str, object], selector))
                usb = {"vendor": f"{parsed.vendor:04x}", "product": f"{parsed.product:04x}"}
            except (ValueError, TypeError):
                usb = None
        sinks: list[dict[str, Any]] = []
        for sink in (fields.get("sinks") if isinstance(fields.get("sinks"), list) else [])[:MAX_SINKS]:
            if not isinstance(sink, dict):
                continue
            item = cast(dict[str, Any], sink)
            identity = item.get("name")
            if not isinstance(identity, str) or SINK_ID_PATTERN.fullmatch(identity) is None:
                continue
            sinks.append({
                "id": identity,
                "description": _text(item.get("sink_description")),
                "card_channels": _channels(item.get("master_channel_map")),
                "output_channels": _channels(item.get("channel_map", "[ FL FR ]")),
                "remix": item.get("remix") == "yes",
            })
        devices.append({"profile": _text(fields.get("card-profile")), "usb": usb, "sinks": sinks})
    return {
        "devices": devices,
        "outputs": [{"id": output.id, "sink": output.sink} for output in configuration.outputs],
    }
