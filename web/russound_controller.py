import json
import tempfile
from pathlib import Path
from typing import Any, cast

from .russound_backend import RussoundBackend
from .zone import Zone


def _resolve_config_path(path: str | Path | None) -> Path | None:
    """Resolve an optional config path to an absolute filesystem path."""
    if path is None:
        return None
    return Path(path).expanduser().resolve()


def _resolve_state_path(path: str | Path | None) -> Path:
    """Resolve a state path to an absolute filesystem path."""
    if path is None:
        return Path(__file__).resolve().parent / "russound_state.json"
    return Path(path).expanduser().resolve()


def load_config(config_path: str | Path | None = None) -> dict[str, Any] | None:
    """Load the web controller configuration from disk.

    Args:
        config_path: Optional path to a JSON config file.
    """
    resolved = _resolve_config_path(config_path)
    if resolved is None or not resolved.exists():
        return None
    with resolved.open("r", encoding="utf-8") as handle:
        data: Any = json.load(handle)
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
    return None


def _sync_system_power(state: dict[str, Any]) -> dict[str, Any]:
    """Derive the system-wide power flag from the current room states.

    Args:
        state: Mutable state object containing all room power values.
    """
    state["system_power"] = any(zone.get("power", False) for zone in state.get("zones", []))
    return state


def _sync_state_from_backend(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Refresh the in-memory room state from the Russound hardware backend.

    Args:
        state: Current controller state to update.
        config: Room and input configuration used to resolve backend mappings.
    """
    backend = RussoundBackend()
    inputs = [
        {"id": input_item["id"], "name": input_item["name"]}
        for input_item in config.get("inputs", [])
    ]
    for zone_data in state.get("zones", []):
        zone = Zone.from_dict(zone_data, default_source=inputs[0].get("id") if inputs else None)
        zone_state = backend.read_zone(zone, inputs, config)
        if zone_state is None:
            continue
        zone.update_from_state(
            {
                "power": zone_state.get("power", zone.power),
                "source": zone_state.get("source", zone.source),
                "volume": zone_state.get("volume", zone.volume),
                "muted": zone_state.get("muted", zone.muted),
                "controller": zone.controller,
                "zone": zone.zone_number,
            },
            default_source=inputs[0].get("id") if inputs else None,
        )
        zone_data.update(zone.to_dict())
    return _sync_system_power(state)


def load_state(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    refresh_backend: bool = True,
) -> dict[str, Any]:
    """Load the current controller state from disk and refresh it from the Russound backend.

    Args:
        config_path: Optional path to the JSON configuration file.
        state_path: Optional path to the persisted state JSON file.
        refresh_backend: When True, enrich state from Russound hardware.
    """
    config = load_config(config_path)
    if config is None:
        return {
            "system_power": False,
            "zones": [],
            "inputs": [],
        }

    resolved_state_path = _resolve_state_path(state_path)
    if resolved_state_path.exists():
        try:
            with resolved_state_path.open("r", encoding="utf-8") as handle:
                data: Any = json.load(handle)
            if isinstance(data, dict):
                state_data = cast(dict[str, Any], data)
                state = ensure_state_matches_config(state_data, config)
                if refresh_backend:
                    return _sync_state_from_backend(state, config)
                return _sync_system_power(state)
        except (json.JSONDecodeError, OSError):
            # If a read races with a write or state is malformed, rebuild from config defaults.
            pass

    inputs: list[dict[str, Any]] = [
        {"id": input_item["id"], "name": input_item["name"]}
        for input_item in config.get("inputs", [])
    ]
    zones: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "system_power": False,
        "zones": zones,
        "inputs": inputs,
    }
    for zone in config.get("zones", []):
        zone_id = zone["id"]
        default_source = inputs[0]["id"] if inputs else None
        state["zones"].append(
            Zone(
                id=zone_id,
                name=zone.get("name", zone_id),
                power=False,
                source=default_source,
                volume=20,
                muted=False,
                controller=int(zone.get("controller", 1)),
                zone_number=int(zone.get("zone", 1)),
            ).to_dict()
        )
    if refresh_backend:
        return _sync_state_from_backend(state, config)
    return _sync_system_power(state)


def ensure_state_matches_config(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Merge persisted state with the latest config and keep room mappings in sync.

    Args:
        state: Persisted state data to normalize.
        config: Current room and input configuration.
    """
    state.setdefault("system_power", False)
    state.setdefault("zones", [])
    state.setdefault("inputs", [])

    state["inputs"] = [
        {"id": input_item["id"], "name": input_item["name"]}
        for input_item in config.get("inputs", [])
    ]

    zone_lookup = {zone["id"]: zone for zone in config.get("zones", [])}
    existing_zone_lookup = {zone["id"]: zone for zone in state.get("zones", [])}

    merged_zones: list[dict[str, Any]] = []
    for zone_id, zone_config in zone_lookup.items():
        existing_zone = existing_zone_lookup.get(zone_id, {})
        default_source = state["inputs"][0]["id"] if state["inputs"] else None
        merged_zone = Zone(
            id=zone_id,
            name=zone_config.get("name", zone_id),
            power=bool(existing_zone.get("power", False)),
            source=existing_zone.get("source", default_source),
            volume=int(existing_zone.get("volume", 20)),
            muted=bool(existing_zone.get("muted", False)),
            controller=int(existing_zone.get("controller", zone_config.get("controller", 1))),
            zone_number=int(existing_zone.get("zone", zone_config.get("zone", 1))),
        )
        if merged_zone.source not in {input_item["id"] for input_item in state["inputs"]}:
            merged_zone.source = default_source
        merged_zones.append(merged_zone.to_dict())

    state["zones"] = merged_zones
    return _sync_system_power(state)


def persist_state(state_path: str | Path | None, state: dict[str, Any]) -> Path:
    """Persist the current controller state to disk as JSON.

    Args:
        state_path: Optional destination path for the state file.
        state: State payload to write.
    """
    resolved_state_path = _resolve_state_path(state_path)
    resolved_state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=resolved_state_path.parent,
        prefix=f".{resolved_state_path.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        tmp_path = Path(handle.name)
    tmp_path.replace(resolved_state_path)
    return resolved_state_path


def update_system_power(state: dict[str, Any], power: bool) -> dict[str, Any]:
    """Turn the full system on or off by forwarding the request to each mapped room.

    Args:
        state: State object containing the configured rooms.
        power: True for system on, False for system off.
    """
    backend = RussoundBackend()
    if not power:
        state["system_power"] = False
        for zone_data in state.get("zones", []):
            zone = Zone.from_dict(zone_data)
            zone.power = False
            zone.set_power(False, backend=backend)
            zone_data.update(zone.to_dict())
    else:
        for zone_data in state.get("zones", []):
            zone = Zone.from_dict(zone_data)
            zone.power = True
            zone.set_power(True, backend=backend)
            zone_data.update(zone.to_dict())
        state["system_power"] = True
    return _sync_system_power(state)


def set_shared_source(state: dict[str, Any], source: int | None) -> dict[str, Any]:
    """Apply one selected input to every room in the current state.

    Args:
        state: Current controller state with room definitions and available inputs.
        source: Input id to apply to each room.
    """
    valid_sources = {input_item["id"] for input_item in state.get("inputs", [])}
    if isinstance(source, int) and source in valid_sources:
        backend = RussoundBackend()
        for zone_data in state.get("zones", []):
            zone = Zone.from_dict(zone_data)
            zone.source = source
            zone.set_source(source, state.get("inputs", []), backend=backend)
            zone_data.update(zone.to_dict())
    return _sync_system_power(state)


def update_zone_setting(state: dict[str, Any], zone_id: str, setting: str, value: Any) -> dict[str, Any]:
    """Update one room property such as power, source, volume, or mute.

    Args:
        state: Controller state containing the room list.
        zone_id: Logical room identifier to update.
        setting: Name of the property to change.
        value: New value for that property.
    """
    backend = RussoundBackend()
    for zone_data in state.get("zones", []):
        if zone_data.get("id") != zone_id:
            continue
        zone = Zone.from_dict(zone_data)
        if setting == "power":
            zone.set_power(bool(value), backend=backend)
            zone_data.update(zone.to_dict())
        elif setting == "source":
            valid_sources = {input_item["id"] for input_item in state.get("inputs", [])}
            if value in valid_sources:
                zone.set_source(value, state.get("inputs", []), backend=backend)
                zone_data.update(zone.to_dict())
            return _sync_system_power(state)
        elif setting == "volume":
            zone.set_volume(max(0, min(100, int(value))), backend=backend)
            zone_data.update(zone.to_dict())
        elif setting == "mute":
            desired_muted = bool(value)
            zone.set_mute(desired_muted, backend=backend)
            zone_data.update(zone.to_dict())
        else:
            raise ValueError(f"Unsupported setting: {setting}")
        break
    return _sync_system_power(state)


def apply_shortcut(state: dict[str, Any], shortcut: dict[str, Any]) -> dict[str, Any]:
    """Apply a named shortcut preset to selected rooms and a chosen input.

    Args:
        state: Current controller state.
        shortcut: Shortcut definition with zone_ids and optional source.
    """
    source = shortcut.get("source")
    backend = RussoundBackend()

    for zone_id in shortcut.get("zone_ids", []):
        for zone_data in state.get("zones", []):
            if zone_data.get("id") == zone_id:
                zone = Zone.from_dict(zone_data)
                zone.power = True
                zone.set_power(True, backend=backend)
                if isinstance(source, int):
                    zone.source = source
                    zone.set_source(source, state.get("inputs", []), backend=backend)
                zone_data.update(zone.to_dict())
                break

    return _sync_system_power(state)


def build_view_payload(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    refresh_backend: bool = True,
) -> dict[str, Any]:
    """Create the JSON payload returned by the web API for the current config and state.

    Args:
        config_path: Optional path to the JSON config file.
        state_path: Optional path to the persisted state JSON file.
        refresh_backend: When True, refresh zone state from hardware.
    """
    config = load_config(config_path)
    state = load_state(config_path, state_path, refresh_backend=refresh_backend)
    if config is None:
        return {
            "config": None,
            "config_required": True,
            "message": "A Russound config file is required. Copy web/config_example.json and start the server with --config.",
            "state": state,
        }
    return {"config": config, "config_required": False, "state": state}
