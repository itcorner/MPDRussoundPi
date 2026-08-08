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


def _default_zone_name(controller_id: int, zone_number: int) -> str:
    return f"Controller {controller_id} Zone {zone_number}"


def _default_source_name(source_id: int) -> str:
    return f"Source {source_id}"


def _zone_address(zone: dict[str, Any]) -> tuple[int, int]:
    return int(zone.get("controller", 1)), int(zone.get("zone", 1))


def shortcut_zone_addresses(shortcut: dict[str, Any], config: dict[str, Any]) -> list[tuple[int, int]]:
    raw_addresses = shortcut.get("zone_addresses")
    if isinstance(raw_addresses, list):
        normalized_addresses: list[tuple[int, int]] = []
        for raw_address in raw_addresses:
            if not isinstance(raw_address, dict):
                continue
            controller_id = raw_address.get("controller")
            zone_number = raw_address.get("zone")
            if isinstance(controller_id, int) and isinstance(zone_number, int):
                normalized_addresses.append((controller_id, zone_number))
        return normalized_addresses

    return []


def _visible_zone_addresses(config: dict[str, Any]) -> set[tuple[int, int]]:
    return {
        _zone_address(zone)
        for zone in config.get("zones", [])
        if isinstance(zone, dict) and bool(zone.get("visible", True))
    }


def _filter_config_for_overview(config: dict[str, Any]) -> dict[str, Any]:
    visible_zone_addresses = _visible_zone_addresses(config)
    filtered_config = dict(config)
    filtered_config["zones"] = [
        zone for zone in config.get("zones", [])
        if isinstance(zone, dict) and _zone_address(zone) in visible_zone_addresses
    ]
    return filtered_config


def _filter_state_for_overview(state: dict[str, Any], visible_zone_addresses: set[tuple[int, int]]) -> dict[str, Any]:
    filtered_state = dict(state)
    filtered_state["zones"] = [
        zone for zone in state.get("zones", [])
        if isinstance(zone, dict) and _zone_address(zone) in visible_zone_addresses
    ]
    return filtered_state


def _persist_json_file(destination: Path, payload: dict[str, Any]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        tmp_path = Path(handle.name)
    tmp_path.replace(destination)
    return destination


def persist_config(config_path: str | Path | None, config: dict[str, Any]) -> Path:
    resolved_config_path = _resolve_config_path(config_path)
    if resolved_config_path is None:
        raise ValueError("A config path is required to persist configuration")
    return _persist_json_file(resolved_config_path, config)


def build_config_editor_payload(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    if config is None:
        return {
            "config": None,
            "config_required": True,
            "message": "A Russound config file is required. Copy web/config_example.json and start the server with --config.",
            "zone_slots": [],
            "source_slots": [],
        }

    zone_lookup = {
        (int(zone.get("controller", 1)), int(zone.get("zone", 1))): zone
        for zone in config.get("zones", [])
        if isinstance(zone, dict)
    }
    zone_slots: list[dict[str, Any]] = []
    for controller in sorted(config.get("controllers", []), key=lambda item: int(item.get("id", 1))):
        if not isinstance(controller, dict):
            continue
        controller_id = int(controller.get("id", 1))
        zone_count = int(controller.get("zone_count", 0))
        for zone_number in range(1, zone_count + 1):
            existing_zone = zone_lookup.get((controller_id, zone_number))
            zone_slots.append(
                {
                    "controller": controller_id,
                    "zone": zone_number,
                    "enabled": existing_zone is not None,
                    "visible": bool(existing_zone.get("visible", True)) if existing_zone else True,
                    "name": existing_zone.get("name", _default_zone_name(controller_id, zone_number)) if existing_zone else _default_zone_name(controller_id, zone_number),
                }
            )

    source_slots: list[dict[str, Any]] = []
    for input_item in sorted(config.get("inputs", []), key=lambda item: int(item.get("id", 0))):
        if not isinstance(input_item, dict):
            continue
        source_id = int(input_item.get("id", 0))
        source_slots.append(
            {
                "id": source_id,
                "name": input_item.get("name", _default_source_name(source_id)),
            }
        )

    return {
        "config": config,
        "config_required": False,
        "zone_slots": zone_slots,
        "source_slots": source_slots,
    }


def update_config_zones(
    config_path: str | Path | None,
    state_path: str | Path | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    config = load_config(config_path)
    if config is None:
        raise ValueError("A config file is required before configuration can be updated")

    zone_slots = payload.get("zone_slots")
    if not isinstance(zone_slots, list):
        raise ValueError("Invalid request body: 'zone_slots' must be a list")

    source_slots = payload.get("source_slots")
    if source_slots is None:
        source_slots = []
    if not isinstance(source_slots, list):
        raise ValueError("Invalid request body: 'source_slots' must be a list")

    controller_limits: dict[int, int] = {}
    for controller in config.get("controllers", []):
        if not isinstance(controller, dict):
            continue
        controller_limits[int(controller.get("id", 1))] = int(controller.get("zone_count", 0))

    existing_zone_lookup = {
        (int(zone.get("controller", 1)), int(zone.get("zone", 1))): zone
        for zone in config.get("zones", [])
        if isinstance(zone, dict)
    }
    seen_addresses: set[tuple[int, int]] = set()
    new_zones: list[dict[str, Any]] = []

    for raw_slot in zone_slots:
        if not isinstance(raw_slot, dict):
            raise ValueError("Invalid request body: each zone slot must be an object")
        controller_id = raw_slot.get("controller")
        zone_number = raw_slot.get("zone")
        enabled = raw_slot.get("enabled")
        visible = raw_slot.get("visible")
        name = raw_slot.get("name")

        if not isinstance(controller_id, int) or isinstance(controller_id, bool):
            raise ValueError("Invalid request body: slot controller must be an integer")
        if not isinstance(zone_number, int) or isinstance(zone_number, bool):
            raise ValueError("Invalid request body: slot zone must be an integer")
        if not isinstance(enabled, bool):
            raise ValueError("Invalid request body: slot enabled must be a boolean")
        if not isinstance(visible, bool):
            raise ValueError("Invalid request body: slot visible must be a boolean")
        if not isinstance(name, str):
            raise ValueError("Invalid request body: slot name must be a string")
        if controller_id not in controller_limits:
            raise ValueError(f"Invalid request body: unknown controller {controller_id}")
        if zone_number < 1 or zone_number > controller_limits[controller_id]:
            raise ValueError(
                f"Invalid request body: controller {controller_id} only supports zones 1..{controller_limits[controller_id]}"
            )

        address = (controller_id, zone_number)
        if address in seen_addresses:
            raise ValueError(f"Invalid request body: duplicate controller/zone slot {controller_id}/{zone_number}")
        seen_addresses.add(address)

        if not enabled:
            continue

        existing_zone = existing_zone_lookup.get(address)
        normalized_zone = dict(existing_zone) if existing_zone else {}
        normalized_zone.update(
            {
                "name": name.strip() or _default_zone_name(controller_id, zone_number),
                "controller": controller_id,
                "zone": zone_number,
                "visible": visible,
            }
        )
        new_zones.append(normalized_zone)

    new_zones.sort(key=lambda zone: (int(zone.get("controller", 1)), int(zone.get("zone", 1))))
    valid_zone_addresses = {_zone_address(zone) for zone in new_zones}

    updated_shortcuts: list[dict[str, Any]] = []
    for shortcut in config.get("shortcuts", []):
        if not isinstance(shortcut, dict):
            continue
        updated_shortcut = dict(shortcut)
        updated_shortcut["zone_addresses"] = [
            {"controller": controller_id, "zone": zone_number}
            for controller_id, zone_number in shortcut_zone_addresses(shortcut, config)
            if (controller_id, zone_number) in valid_zone_addresses
        ]
        updated_shortcuts.append(updated_shortcut)

    updated_config = dict(config)
    updated_config["zones"] = new_zones
    updated_config["shortcuts"] = updated_shortcuts

    existing_inputs: list[dict[str, Any]] = []
    input_lookup: dict[int, dict[str, Any]] = {}
    for input_item in config.get("inputs", []):
        if not isinstance(input_item, dict):
            continue
        source_id = int(input_item.get("id", 0))
        normalized_input = dict(input_item)
        normalized_input.setdefault("name", _default_source_name(source_id))
        existing_inputs.append(normalized_input)
        input_lookup[source_id] = normalized_input

    if source_slots:
        seen_source_ids: set[int] = set()
        for raw_source in source_slots:
            if not isinstance(raw_source, dict):
                raise ValueError("Invalid request body: each source slot must be an object")
            source_id = raw_source.get("id")
            source_name = raw_source.get("name")

            if not isinstance(source_id, int) or isinstance(source_id, bool):
                raise ValueError("Invalid request body: source id must be an integer")
            if not isinstance(source_name, str):
                raise ValueError("Invalid request body: source name must be a string")
            if source_id not in input_lookup:
                raise ValueError(f"Invalid request body: unknown source {source_id}")
            if source_id in seen_source_ids:
                raise ValueError(f"Invalid request body: duplicate source slot {source_id}")
            seen_source_ids.add(source_id)

            input_lookup[source_id]["name"] = source_name.strip() or _default_source_name(source_id)

    updated_config["inputs"] = existing_inputs
    persist_config(config_path, updated_config)

    updated_state = load_state(config_path, state_path, refresh_backend=False)
    persist_state(state_path, updated_state)
    return build_config_editor_payload(config_path, state_path)


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
        if not isinstance(zone, dict):
            continue
        controller_id, zone_number = _zone_address(zone)
        default_source = inputs[0]["id"] if inputs else None
        state["zones"].append(
            Zone(
                name=zone.get("name", _default_zone_name(controller_id, zone_number)),
                power=False,
                source=default_source,
                volume=20,
                controller=controller_id,
                zone_number=zone_number,
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

    zone_lookup = {
        _zone_address(zone): zone
        for zone in config.get("zones", [])
        if isinstance(zone, dict)
    }
    existing_zone_lookup = {
        _zone_address(zone): zone
        for zone in state.get("zones", [])
        if isinstance(zone, dict)
    }

    merged_zones: list[dict[str, Any]] = []
    for zone_address, zone_config in zone_lookup.items():
        existing_zone = existing_zone_lookup.get(zone_address, {})
        default_source = state["inputs"][0]["id"] if state["inputs"] else None
        controller_id, zone_number = zone_address
        merged_zone = Zone(
            name=zone_config.get("name", _default_zone_name(controller_id, zone_number)),
            power=bool(existing_zone.get("power", False)),
            source=existing_zone.get("source", default_source),
            volume=int(existing_zone.get("volume", 20)),
            controller=int(existing_zone.get("controller", controller_id)),
            zone_number=int(existing_zone.get("zone", zone_number)),
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
    return _persist_json_file(resolved_state_path, state)


def update_system_power(state: dict[str, Any], power: bool) -> dict[str, Any]:
    """Turn the full system on or off by forwarding the request to each mapped room.

    Args:
        state: State object containing the configured rooms.
        power: True for system on, False for system off.
    """
    backend = RussoundBackend()
    if not power:
        backend.set_all_power(False, len(state.get("zones", [])))
        state["system_power"] = False
        for zone_data in state.get("zones", []):
            zone = Zone.from_dict(zone_data)
            zone.power = False
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


def update_zone_setting(
    state: dict[str, Any],
    controller_id: int,
    zone_number: int,
    setting: str,
    value: Any,
) -> dict[str, Any]:
    """Update one room property such as power, source, or volume.

    Args:
        state: Controller state containing the room list.
        controller_id: Russound controller address for the target zone.
        zone_number: Russound zone number on that controller.
        setting: Name of the property to change.
        value: New value for that property.
    """
    backend = RussoundBackend()
    for zone_data in state.get("zones", []):
        if int(zone_data.get("controller", 0)) != controller_id or int(zone_data.get("zone", 0)) != zone_number:
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
        else:
            raise ValueError(f"Unsupported setting: {setting}")
        break
    return _sync_system_power(state)


def apply_shortcut(state: dict[str, Any], shortcut: dict[str, Any]) -> dict[str, Any]:
    """Apply a named shortcut preset to selected rooms and a chosen input.

    Args:
        state: Current controller state.
        shortcut: Shortcut definition with zone addresses and optional source.
    """
    source = shortcut.get("source")
    backend = RussoundBackend()

    for raw_address in shortcut.get("zone_addresses", []):
        if not isinstance(raw_address, dict):
            continue
        controller_id = raw_address.get("controller")
        zone_number = raw_address.get("zone")
        if not isinstance(controller_id, int) or not isinstance(zone_number, int):
            continue
        for zone_data in state.get("zones", []):
            if _zone_address(zone_data) == (controller_id, zone_number):
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
    visible_zone_addresses = _visible_zone_addresses(config)
    return {
        "config": _filter_config_for_overview(config),
        "config_required": False,
        "state": _filter_state_for_overview(state, visible_zone_addresses),
    }
