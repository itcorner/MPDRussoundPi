from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import TypedDict, cast

BACKEND_HOST_ENV_VAR = "RUSSOUND_BACKEND_HOST"
BACKEND_PORT_ENV_VAR = "RUSSOUND_BACKEND_PORT"

LOGGER = logging.getLogger(__name__)


def validate_russound_config(config: object) -> None:
    if not isinstance(config, dict) or not config:
        raise ValueError("Configuration must be a non-empty JSON object")
    backend = config.get("backend")
    if backend is not None:
        if not isinstance(backend, dict):
            raise ValueError("backend must be an object")
        if "host" in backend and (not isinstance(backend["host"], str) or not backend["host"].strip()):
            raise ValueError("Backend host must be a non-empty hostname or IP address")
        for field, minimum, maximum in (("port", 1, 65535), ("baud", 1, 4000000)):
            if field in backend:
                value = backend[field]
                if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                    raise ValueError(f"Backend {field} must be an integer from {minimum} to {maximum}")
        for field in ("device", "protocol_audit_log_file"):
            if field in backend and (not isinstance(backend[field], str) or not backend[field].strip()):
                raise ValueError(f"Backend {field} must be a non-empty string")
        if "poll_interval_seconds" in backend:
            interval = backend["poll_interval_seconds"]
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 1:
                raise ValueError("Backend poll interval must be a finite number of at least 1 second")
    for collection in ("controllers", "zones", "inputs", "shortcuts"):
        items = config.get(collection, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"{collection} must be a list of objects")
        seen = set()
        for item in items:
            fields = {
                "controllers": (("id", 1, 6), ("zone_count", 1, 6)),
                "zones": (("controller", 1, 6), ("zone", 1, 6), ("keypad_id", 1, 6)),
                "inputs": (("id", 1, 6),),
                "shortcuts": (("source", 1, 6),),
            }[collection]
            for field, minimum, maximum in fields:
                if field in item:
                    value = item[field]
                    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                        raise ValueError(f"{collection}.{field} must be an integer from {minimum} to {maximum}")
            if "name" in item and not isinstance(item["name"], str):
                raise ValueError(f"{collection}.name must be a string")
            if collection == "inputs" and "playback_output" in item:
                raw_binding = cast(object, item["playback_output"])
                binding = cast(dict[str, object], raw_binding) if isinstance(raw_binding, dict) else {}
                if (
                    set(binding) != {"agent_id", "output_id"}
                    or any(
                        not isinstance(value, str)
                        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None
                        for value in binding.values()
                    )
                ):
                    raise ValueError("inputs.playback_output must contain valid agent_id and output_id")
            for field in ("enabled", "visible"):
                if field in item and not isinstance(item[field], bool):
                    raise ValueError(f"{collection}.{field} must be a boolean")
            if collection in ("controllers", "inputs"):
                if "id" not in item or (collection == "controllers" and "zone_count" not in item):
                    raise ValueError(f"{collection} entries need id" + (" and zone_count" if collection == "controllers" else ""))
                if item["id"] in seen:
                    raise ValueError(f"Duplicate {collection} id {item['id']}")
                seen.add(item["id"])
            if collection == "shortcuts":
                addresses = item.get("zone_addresses", [])
                if not isinstance(addresses, list) or any(
                    not isinstance(address, dict) or any(
                        isinstance(address.get(field), bool) or not isinstance(address.get(field), int) or not 1 <= address[field] <= 6
                        for field in ("controller", "zone")
                    ) for address in addresses
                ):
                    raise ValueError("Shortcut zone_addresses must contain controller/zone addresses from 1 to 6")


class BackendConfig(TypedDict, total=False):
    host: str
    port: int
    device: str
    baud: int
    poll_interval_seconds: float
    protocol_audit_log_file: str


class ControllerConfig(TypedDict, total=False):
    id: int
    zone_count: int


class ZoneConfig(TypedDict, total=False):
    name: str
    controller: int
    zone: int
    keypad_id: int
    enabled: bool
    visible: bool


class PlaybackOutputBinding(TypedDict):
    agent_id: str
    output_id: str


class InputConfig(TypedDict, total=False):
    id: int
    name: str
    enabled: bool
    playback_output: PlaybackOutputBinding


class ShortcutZoneAddressConfig(TypedDict, total=False):
    controller: int
    zone: int


class ShortcutConfig(TypedDict, total=False):
    id: str
    name: str
    zone_addresses: list[ShortcutZoneAddressConfig]
    source: int


class RussoundConfig(TypedDict, total=False):
    backend: BackendConfig
    controllers: list[ControllerConfig]
    zones: list[ZoneConfig]
    inputs: list[InputConfig]
    shortcuts: list[ShortcutConfig]


@dataclass(frozen=True)
class BackendEndpoint:
    host: str = "127.0.0.1"
    port: int = 6666
    device: str | None = None
    baud: int = 19200
    loaded_from_config: bool = False


def coerce_russound_config(raw_config: object) -> RussoundConfig | None:
    if not isinstance(raw_config, dict):
        return None
    return cast(RussoundConfig, raw_config)


def env_str(name: str) -> str | None:
    """Return a non-empty, stripped environment value, or None when unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    stripped_value = value.strip()
    return stripped_value or None


def env_port(name: str) -> int | None:
    """Return a valid TCP port from the environment, ignoring malformed values."""
    raw_value = env_str(name)
    if raw_value is None:
        return None
    try:
        port = int(raw_value)
    except ValueError:
        LOGGER.warning("Ignoring %s=%r because it is not an integer", name, raw_value)
        return None
    if not 1 <= port <= 65535:
        LOGGER.warning("Ignoring %s=%d because it is outside 1-65535", name, port)
        return None
    return port


def _backend_endpoint_from_config(config: RussoundConfig | None) -> BackendEndpoint:
    if config is None:
        return BackendEndpoint()

    backend_config = config.get("backend")
    if backend_config is None:
        return BackendEndpoint()

    host = backend_config.get("host")
    port = backend_config.get("port")
    device = backend_config.get("device")
    baud = backend_config.get("baud", 19200)
    if isinstance(host, str) and host.strip() and isinstance(port, int) and not isinstance(port, bool):
        return BackendEndpoint(
            host=host.strip(),
            port=port,
            device=device.strip() if isinstance(device, str) and device.strip() else None,
            baud=baud if isinstance(baud, int) and not isinstance(baud, bool) else 19200,
            loaded_from_config=True,
        )
    if isinstance(device, str) and device.strip():
        return BackendEndpoint(
            device=device.strip(),
            baud=baud if isinstance(baud, int) and not isinstance(baud, bool) else 19200,
            loaded_from_config=True,
        )
    return BackendEndpoint()


def resolve_backend_endpoint(config: RussoundConfig | None) -> BackendEndpoint:
    """Resolve the hardware endpoint, letting environment variables override the config file."""
    endpoint = _backend_endpoint_from_config(config)

    host_override = env_str(BACKEND_HOST_ENV_VAR)
    port_override = env_port(BACKEND_PORT_ENV_VAR)
    if host_override is None and port_override is None:
        return endpoint

    return BackendEndpoint(
        host=host_override or endpoint.host,
        port=port_override or endpoint.port,
        loaded_from_config=True,
    )


def resolve_controller_zone_limits(config: RussoundConfig | None) -> dict[int, int]:
    if config is None:
        return {}

    controller_zone_limits: dict[int, int] = {}
    for controller_config in config.get("controllers", []):
        controller_id = controller_config.get("id", 1)
        zone_count = controller_config.get("zone_count", 6)
        if not isinstance(controller_id, int) or isinstance(controller_id, bool):
            continue
        if not isinstance(zone_count, int) or isinstance(zone_count, bool):
            continue
        if 1 <= controller_id <= 6:
            controller_zone_limits[controller_id] = min(6, max(1, zone_count))
    return controller_zone_limits


def resolve_backend_poll_interval_seconds(config: RussoundConfig | None, default_seconds: float) -> float:
    if config is None:
        return default_seconds

    backend_config = config.get("backend")
    if backend_config is None:
        return default_seconds

    poll_interval_seconds = backend_config.get("poll_interval_seconds")
    if isinstance(poll_interval_seconds, (int, float)) and not isinstance(poll_interval_seconds, bool):
        return float(poll_interval_seconds)
    return default_seconds


def resolve_backend_protocol_audit_log_file(config: RussoundConfig | None) -> str | None:
    if config is None:
        return None

    backend_config = config.get("backend")
    if backend_config is None:
        return None

    log_file = backend_config.get("protocol_audit_log_file")
    if isinstance(log_file, str) and log_file.strip():
        return log_file.strip()
    return None