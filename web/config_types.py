from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, cast


class BackendConfig(TypedDict, total=False):
    host: str
    port: int
    poll_interval_seconds: float


class ControllerConfig(TypedDict, total=False):
    id: int
    zone_count: int


class ZoneConfig(TypedDict, total=False):
    name: str
    controller: int
    zone: int
    enabled: bool
    visible: bool


class InputConfig(TypedDict, total=False):
    id: int
    name: str


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
    loaded_from_config: bool = False


def coerce_russound_config(raw_config: object) -> RussoundConfig | None:
    if not isinstance(raw_config, dict):
        return None
    return cast(RussoundConfig, raw_config)


def resolve_backend_endpoint(config: RussoundConfig | None) -> BackendEndpoint:
    if config is None:
        return BackendEndpoint()

    backend_config = config.get("backend")
    if backend_config is None:
        return BackendEndpoint()

    host = backend_config.get("host")
    port = backend_config.get("port")
    if isinstance(host, str) and host.strip() and isinstance(port, int) and not isinstance(port, bool):
        return BackendEndpoint(host=host.strip(), port=port, loaded_from_config=True)
    return BackendEndpoint()


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