from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, cast

from .russound_backend import RussoundBackend
from .russound_state import RussoundState
from .zone import Zone


class RussoundController:
    """Singleton-style controller that owns config, state, and persistence behavior."""

    def __init__(self, config_path: str | Path | None = None, state_path: str | Path | None = None) -> None:
        self.config_path = _resolve_config_path(config_path)
        self.state_path = _resolve_state_path(state_path)

    def load_config(self) -> dict[str, Any] | None:
        resolved = _resolve_config_path(self.config_path)
        if resolved is None or not resolved.exists():
            return None
        with resolved.open("r", encoding="utf-8") as handle:
            data: Any = json.load(handle)
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
        return None

    def build_config_editor_payload(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        config = self.load_config()
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
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        if payload is None:
            raise ValueError("Invalid request body")
        config = self.load_config()
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
        persist_config(self.config_path, updated_config)

        updated_state = self.load_state(refresh_backend=False)
        self.persist_state(updated_state)
        return self.build_config_editor_payload()

    def _sync_system_power(self, state: RussoundState) -> RussoundState:
        return state.sync_system_power()

    def _sync_state_from_backend(self, state: RussoundState, config: dict[str, Any]) -> RussoundState:
        backend = RussoundBackend(config=config)
        inputs = [
            {"id": input_item["id"], "name": input_item["name"]}
            for input_item in config.get("inputs", [])
        ]
        zone_items = list(state.zones)
        for zone_index, zone_item in enumerate(zone_items):
            zone = _coerce_zone(zone_item, default_source=inputs[0].get("id") if inputs else None)
            zone_state = backend.read_zone(zone, inputs)
            if zone_state is None:
                continue
            zone.update_from_state(
                {
                    "power": zone_state.get("power", zone.power),
                    "source": zone_state.get("source", zone.source),
                    "volume": zone_state.get("volume", zone.volume),
                    "bass": zone_state.get("bass", zone.bass),
                    "treble": zone_state.get("treble", zone.treble),
                    "loudness": zone_state.get("loudness", zone.loudness),
                    "balance": zone_state.get("balance", zone.balance),
                    "controller": zone.controller,
                    "zone": zone.zone_number,
                },
                default_source=inputs[0].get("id") if inputs else None,
            )
            zone_items[zone_index] = zone
        state.zones = zone_items
        return self._sync_system_power(state)

    def load_state(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        refresh_backend: bool = True,
    ) -> RussoundState:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        config = self.load_config()
        if config is None:
            return RussoundState()

        resolved_state_path = _resolve_state_path(self.state_path)
        if resolved_state_path.exists():
            try:
                with resolved_state_path.open("r", encoding="utf-8") as handle:
                    data: Any = json.load(handle)
                if isinstance(data, dict):
                    state_data = cast(dict[str, Any], data)
                    state = self.ensure_state_matches_config(state_data, config)
                    if refresh_backend:
                        return self._sync_state_from_backend(state, config)
                    return self._sync_system_power(state)
            except (json.JSONDecodeError, OSError):
                pass

        inputs: list[dict[str, Any]] = [
            {"id": input_item["id"], "name": input_item["name"]}
            for input_item in config.get("inputs", [])
        ]
        state = RussoundState(system_power=False, zones=[], inputs=inputs)
        for zone_config in config.get("zones", []):
            if not isinstance(zone_config, dict):
                continue
            controller_id, zone_number = _zone_address(zone_config)
            default_source = inputs[0]["id"] if inputs else None
            state.zones.append(
                Zone(
                    name=zone_config.get("name", _default_zone_name(controller_id, zone_number)),
                    power=False,
                    source=default_source,
                    volume=20,
                    bass=0,
                    treble=0,
                    loudness=False,
                    balance=0,
                    controller=controller_id,
                    zone_number=zone_number,
                    enabled=bool(zone_config.get("enabled", True)),
                    visible=bool(zone_config.get("visible", True)),
                )
            )
        if refresh_backend:
            return self._sync_state_from_backend(state, config)
        return self._sync_system_power(state)

    def ensure_state_matches_config(
        self,
        state: dict[str, Any] | RussoundState,
        config: dict[str, Any],
    ) -> RussoundState:
        if not isinstance(state, RussoundState):
            state = RussoundState.from_payload(state)
        state.inputs = [
            {"id": input_item["id"], "name": input_item["name"]}
            for input_item in config.get("inputs", [])
        ]

        zone_lookup = {
            _zone_address(zone): zone
            for zone in config.get("zones", [])
            if isinstance(zone, dict)
        }
        existing_zone_lookup = {
            _zone_address(zone): _coerce_zone(zone, default_source=state.inputs[0]["id"] if state.inputs else None)
            for zone in state.zones
        }

        merged_zones: list[Zone] = []
        for zone_address, zone_config in zone_lookup.items():
            existing_zone = existing_zone_lookup.get(zone_address)
            default_source = state.inputs[0]["id"] if state.inputs else None
            controller_id, zone_number = zone_address
            merged_zone = Zone(
                name=zone_config.get("name", _default_zone_name(controller_id, zone_number)),
                power=bool(existing_zone.power if isinstance(existing_zone, Zone) else False),
                source=(existing_zone.source if isinstance(existing_zone, Zone) else default_source),
                volume=int(existing_zone.volume if isinstance(existing_zone, Zone) else 20),
                bass=int(existing_zone.bass if isinstance(existing_zone, Zone) else 0),
                treble=int(existing_zone.treble if isinstance(existing_zone, Zone) else 0),
                loudness=bool(existing_zone.loudness if isinstance(existing_zone, Zone) else False),
                balance=int(existing_zone.balance if isinstance(existing_zone, Zone) else 0),
                controller=int(existing_zone.controller if isinstance(existing_zone, Zone) else controller_id),
                zone_number=int(existing_zone.zone_number if isinstance(existing_zone, Zone) else zone_number),
                enabled=bool(zone_config.get("enabled", getattr(existing_zone, "enabled", True) if isinstance(existing_zone, Zone) else True)),
                visible=bool(zone_config.get("visible", getattr(existing_zone, "visible", True) if isinstance(existing_zone, Zone) else True)),
            )
            if merged_zone.source not in {input_item["id"] for input_item in state.inputs}:
                merged_zone.source = default_source
            merged_zones.append(merged_zone)

        state.zones = merged_zones
        return self._sync_system_power(state)

    def persist_state(self, state: RussoundState | dict[str, Any] | None = None) -> Path:
        resolved_state_path = _resolve_state_path(self.state_path)
        payload = state if state is not None else RussoundState()
        prepared_payload = _prepare_state_payload_for_persistence(payload)
        return _persist_json_file(resolved_state_path, prepared_payload)

    def _backend_for_config(self, config: dict[str, Any] | None = None) -> RussoundBackend | None:
        resolved_config = config if isinstance(config, dict) else self.load_config()
        backend = RussoundBackend(config=resolved_config)
        try:
            if backend._connect() is None:
                return None
        except Exception:
            return None
        return backend

    def update_system_power(self, power: bool, state: RussoundState | None = None) -> RussoundState:
        current_state = state if state is not None else self.load_state(refresh_backend=False)
        config = self.load_config()
        return current_state.set_system_power(power, backend=self._backend_for_config(config))

    def set_shared_source(self, source: int | None, state: RussoundState | None = None) -> RussoundState:
        current_state = state if state is not None else self.load_state(refresh_backend=False)
        config = self.load_config()
        return current_state.set_shared_source(source, backend=self._backend_for_config(config))

    def update_zone_setting(
        self,
        controller_id: int,
        zone_number: int,
        setting: str,
        value: Any,
        state: RussoundState | None = None,
    ) -> RussoundState:
        current_state = state if state is not None else self.load_state(refresh_backend=False)
        config = self.load_config()
        return current_state.update_zone_setting(controller_id, zone_number, setting, value, backend=self._backend_for_config(config))

    def apply_shortcut(self, shortcut: dict[str, Any], state: RussoundState | None = None) -> RussoundState:
        current_state = state if state is not None else self.load_state(refresh_backend=False)
        config = self.load_config()
        return current_state.apply_shortcut(shortcut, backend=self._backend_for_config(config))

    def handle_system_power_change(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        power: bool | None = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        if power is None:
            raise ValueError("Power value is required")
        state = self.load_state(refresh_backend=False)
        self.update_system_power(power, state)
        self.persist_state(state)
        return self.build_view_payload(refresh_backend=False)

    def handle_source_change(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        source: int | None = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        if source is None:
            raise ValueError("Source value is required")
        state = self.load_state(refresh_backend=False)
        if not state.has_input(source):
            raise LookupError("Source not found")
        self.set_shared_source(source, state)
        self.persist_state(state)
        return self.build_view_payload(refresh_backend=False)

    def handle_shortcut_activation(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        shortcut_id: str | None = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        if shortcut_id is None:
            raise ValueError("Shortcut id is required")
        state = self.load_state(refresh_backend=False)
        config = self.load_config()
        if config is None:
            raise RuntimeError("A config file is required before shortcuts can be activated")
        shortcut = next((item for item in config.get("shortcuts", []) if item.get("id") == shortcut_id), None)
        if shortcut is None:
            raise LookupError("Shortcut not found")
        resolved_zone_addresses = shortcut_zone_addresses(shortcut, config)
        if not state.has_zone_addresses(resolved_zone_addresses):
            raise RuntimeError("Shortcut configuration error: shortcut references unknown zone")
        shortcut_source = shortcut.get("source")
        if shortcut_source is not None and not state.has_input(shortcut_source):
            raise RuntimeError("Shortcut configuration error: shortcut references unknown source")
        normalized_shortcut = dict(shortcut)
        normalized_shortcut["zone_addresses"] = [
            {"controller": controller_id, "zone": zone_number}
            for controller_id, zone_number in resolved_zone_addresses
        ]
        self.apply_shortcut(normalized_shortcut, state)
        self.persist_state(state)
        return self.build_view_payload(refresh_backend=False)

    def handle_zone_setting_change(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        controller_id: int | None = None,
        zone_number: int | None = None,
        setting: str | None = None,
        value: Any = None,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        if controller_id is None or zone_number is None or setting is None:
            raise ValueError("Zone target and setting are required")
        state = self.load_state(refresh_backend=False)
        if not state.has_zone_address(controller_id, zone_number):
            raise LookupError("Zone not found")
        if setting == "source" and not state.has_input(value):
            raise LookupError("Source not found")
        try:
            self.update_zone_setting(controller_id, zone_number, setting, value, state)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc
        self.persist_state(state)
        return self.build_view_payload(refresh_backend=False)

    def build_view_payload(
        self,
        config_path: str | Path | None = None,
        state_path: str | Path | None = None,
        refresh_backend: bool = True,
    ) -> dict[str, Any]:
        if config_path is not None:
            self.config_path = _resolve_config_path(config_path)
        if state_path is not None:
            self.state_path = _resolve_state_path(state_path)
        config = self.load_config()
        state = self.load_state(refresh_backend=refresh_backend)
        if config is None:
            return {
                "config": None,
                "config_required": True,
                "message": "A Russound config file is required. Copy web/config_example.json and start the server with --config.",
                "state": state.to_payload(),
            }
        visible_zone_addresses = _visible_zone_addresses(config)
        return {
            "config": _filter_config_for_overview(config),
            "config_required": False,
            "state": _filter_state_for_overview(state, visible_zone_addresses),
        }


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser().resolve()


def _resolve_state_path(path: str | Path | None) -> Path:
    if path is None:
        return Path(__file__).resolve().parent / "russound_state.json"
    return Path(path).expanduser().resolve()


def _default_zone_name(controller_id: int, zone_number: int) -> str:
    return f"Controller {controller_id} Zone {zone_number}"


def _default_source_name(source_id: int) -> str:
    return f"Source {source_id}"


def _zone_address(zone: Zone | dict[str, Any]) -> tuple[int, int]:
    if isinstance(zone, Zone):
        return zone.controller, zone.zone_number
    return int(zone.get("controller", 1)), int(zone.get("zone", 1))


def _coerce_zone(zone_data: Zone | dict[str, Any] | None, default_source: int | None = None) -> Zone:
    if isinstance(zone_data, Zone):
        return zone_data
    if isinstance(zone_data, dict):
        return Zone.from_dict(zone_data, default_source=default_source)
    return Zone(name="", source=default_source)


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


def _filter_state_for_overview(state: RussoundState | dict[str, Any], visible_zone_addresses: set[tuple[int, int]]) -> dict[str, Any]:
    if isinstance(state, RussoundState):
        filtered_state = state.to_payload()
        filtered_state["zones"] = [
            zone.to_frontend_payload()
            for zone in state.zones
            if _zone_address(zone) in visible_zone_addresses
        ]
        return filtered_state
    normalized_state = RussoundState.from_payload(state)
    filtered_state = normalized_state.to_payload()
    filtered_state["zones"] = [
        zone.to_frontend_payload() for zone in normalized_state.zones if _zone_address(zone) in visible_zone_addresses
    ]
    return filtered_state


def _prepare_state_payload_for_persistence(payload: RussoundState | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(payload, RussoundState):
        prepared_payload = payload.to_payload()
    elif isinstance(payload, dict):
        prepared_payload = dict(payload)
    else:
        prepared_payload = {}

    if "zones" in prepared_payload:
        prepared_payload["zones"] = [
            zone.to_state_payload() if isinstance(zone, Zone) else zone
            for zone in prepared_payload.get("zones", [])
        ]
    if "system_power" in prepared_payload and "inputs" in prepared_payload:
        prepared_payload.pop("inputs", None)
    return prepared_payload


def _prepare_config_payload_for_persistence(config: dict[str, Any]) -> dict[str, Any]:
    prepared_config = dict(config)
    if "zones" in prepared_config:
        prepared_config["zones"] = [
            zone.to_config_payload() if isinstance(zone, Zone) else zone
            for zone in prepared_config.get("zones", [])
        ]
    return prepared_config


def _persist_json_file(destination: Path, payload: Any) -> Path:
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
    prepared_config = _prepare_config_payload_for_persistence(config)
    return _persist_json_file(resolved_config_path, prepared_config)


def load_config(config_path: str | Path | None = None) -> dict[str, Any] | None:
    return get_controller(config_path=config_path).load_config()


def load_state(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    refresh_backend: bool = True,
) -> RussoundState:
    return get_controller(config_path=config_path, state_path=state_path).load_state(refresh_backend=refresh_backend)


def ensure_state_matches_config(state: dict[str, Any] | RussoundState, config: dict[str, Any]) -> RussoundState:
    return get_controller().ensure_state_matches_config(state, config)


def persist_state(state_path: str | Path | None, state: RussoundState | dict[str, Any]) -> Path:
    return get_controller(state_path=state_path).persist_state(state)


def update_system_power(state: RussoundState | dict[str, Any], power: bool) -> RussoundState:
    return get_controller().update_system_power(power, state if isinstance(state, RussoundState) else None)


def set_shared_source(state: RussoundState | dict[str, Any], source: int | None) -> RussoundState:
    return get_controller().set_shared_source(source, state if isinstance(state, RussoundState) else None)


def update_zone_setting(
    state: RussoundState | dict[str, Any],
    controller_id: int,
    zone_number: int,
    setting: str,
    value: Any,
) -> RussoundState:
    return get_controller().update_zone_setting(controller_id, zone_number, setting, value, state if isinstance(state, RussoundState) else None)


def apply_shortcut(state: RussoundState | dict[str, Any], shortcut: dict[str, Any]) -> RussoundState:
    return get_controller().apply_shortcut(shortcut, state if isinstance(state, RussoundState) else None)


def handle_system_power_change(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    power: bool | None = None,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).handle_system_power_change(
        config_path=config_path,
        state_path=state_path,
        power=power,
    )


def handle_source_change(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    source: int | None = None,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).handle_source_change(
        config_path=config_path,
        state_path=state_path,
        source=source,
    )


def handle_shortcut_activation(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    shortcut_id: str | None = None,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).handle_shortcut_activation(
        config_path=config_path,
        state_path=state_path,
        shortcut_id=shortcut_id,
    )


def handle_zone_setting_change(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    controller_id: int | None = None,
    zone_number: int | None = None,
    setting: str | None = None,
    value: Any = None,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).handle_zone_setting_change(
        config_path=config_path,
        state_path=state_path,
        controller_id=controller_id,
        zone_number=zone_number,
        setting=setting,
        value=value,
    )


def build_view_payload(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
    refresh_backend: bool = True,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).build_view_payload(
        config_path=config_path,
        state_path=state_path,
        refresh_backend=refresh_backend,
    )


def build_config_editor_payload(
    config_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).build_config_editor_payload(
        config_path=config_path,
        state_path=state_path,
    )


def update_config_zones(
    config_path: str | Path | None,
    state_path: str | Path | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return get_controller(config_path=config_path, state_path=state_path).update_config_zones(
        config_path=config_path,
        state_path=state_path,
        payload=payload,
    )


def get_controller(config_path: str | Path | None = None, state_path: str | Path | None = None) -> RussoundController:
    global _controller
    if _controller is None:
        _controller = RussoundController(config_path=config_path, state_path=state_path)
        return _controller
    if config_path is not None:
        _controller.config_path = _resolve_config_path(config_path)
    if state_path is not None:
        _controller.state_path = _resolve_state_path(state_path)
    return _controller


_controller: RussoundController | None = None
