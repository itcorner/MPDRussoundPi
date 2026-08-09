from __future__ import annotations

from typing import Any

from .zone import Zone


class RussoundState:
    """Central in-memory state container for the Russound controller."""

    def __init__(
        self,
        system_power: bool = False,
        zones: list[Zone] | None = None,
        inputs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.system_power = bool(system_power)
        self.zones: list[Zone] = list(zones or [])
        self.inputs: list[dict[str, Any]] = [dict(input_item) for input_item in (inputs or [])]

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, default_source: int | None = None) -> "RussoundState":
        if not isinstance(payload, dict):
            payload = {}
        zones_payload = payload.get("zones", [])
        zones: list[Zone] = []
        if isinstance(zones_payload, list):
            for zone_data in zones_payload:
                if isinstance(zone_data, Zone):
                    zones.append(zone_data)
                elif isinstance(zone_data, dict):
                    zones.append(Zone.from_state_payload(zone_data, default_source=default_source))
        inputs_payload = payload.get("inputs", [])
        inputs: list[dict[str, Any]] = []
        if isinstance(inputs_payload, list):
            for input_item in inputs_payload:
                if isinstance(input_item, dict):
                    inputs.append(dict(input_item))
        return cls(
            system_power=bool(payload.get("system_power", False)),
            zones=zones,
            inputs=inputs,
        )

    def __getitem__(self, key: str) -> Any:
        if key == "system_power":
            return self.system_power
        if key == "zones":
            return self.zones
        if key == "inputs":
            return self.inputs
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "system_power":
            self.system_power = bool(value)
            return
        if key == "zones":
            self.zones = [zone if isinstance(zone, Zone) else Zone.from_state_payload(zone) for zone in value]
            return
        if key == "inputs":
            self.inputs = [dict(input_item) for input_item in value]
            return
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_payload(self) -> dict[str, Any]:
        return {
            "system_power": self.system_power,
            "zones": [zone.to_state_payload() for zone in self.zones],
        }

    def to_state_dict(self) -> dict[str, Any]:
        return self.to_payload()

    def has_zone_address(self, controller_id: int, zone_number: int) -> bool:
        return any(
            zone.controller == controller_id and zone.zone_number == zone_number
            for zone in self.zones
        )

    def has_zone_addresses(self, zone_addresses: list[tuple[int, int]]) -> bool:
        known_zone_addresses = {(zone.controller, zone.zone_number) for zone in self.zones}
        return all(zone_address in known_zone_addresses for zone_address in zone_addresses)

    def has_input(self, source_id: Any) -> bool:
        return any(input_item.get("id") == source_id for input_item in self.inputs)

    def sync_system_power(self) -> "RussoundState":
        self.system_power = any(zone.power for zone in self.zones)
        return self

    def set_system_power(self, power: bool, backend: Any | None = None) -> "RussoundState":
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        if not power:
            backend.turn_all_zones_off()
            self.system_power = False
            for zone in self.zones:
                zone.power = False
        else:
            enabled_zones = [zone for zone in self.zones if getattr(zone, "enabled", True)]
            backend.turn_all_zones_on(enabled_zones)
            for zone in self.zones:
                zone.power = bool(getattr(zone, "enabled", True))
            self.system_power = True
        return self.sync_system_power()

    def set_shared_source(self, source: int | None, backend: Any | None = None) -> "RussoundState":
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        if isinstance(source, int) and self.has_input(source):
            for zone in self.zones:
                if not getattr(zone, "enabled", True):
                    continue
                zone.source = source
                zone.set_source(source, self.inputs, backend=backend)
        return self.sync_system_power()

    def update_zone_setting(self, controller_id: int, zone_number: int, setting: str, value: Any, backend: Any | None = None) -> "RussoundState":
        from .russound_backend import RussoundBackend

        backend_provided: bool = backend is not None
        #backend_instance = backend if backend is not None else RussoundBackend()
        backend_instance = backend or RussoundBackend()
        for zone in self.zones:
            if zone.controller != controller_id or zone.zone_number != zone_number:
                continue
            if setting == "power":
                success = zone.set_power(bool(value), backend=backend_instance)
                if backend_provided and not success:
                    raise RuntimeError("Unable to update Russound hardware")
            elif setting == "source":
                if value in {input_item["id"] for input_item in self.inputs if isinstance(input_item, dict)}:
                    success = zone.set_source(value, self.inputs, backend=backend_instance)
                    if backend_provided and not success:
                        raise RuntimeError("Unable to update Russound hardware")
                else:
                    raise LookupError("Source not found")
            elif setting == "volume":
                success = zone.set_volume(max(0, min(100, int(value))), backend=backend_instance)
                if backend_provided and not success:
                    raise RuntimeError("Unable to update Russound hardware")
            elif setting == "bass":
                success = zone.set_bass(max(-10, min(10, int(value))), backend=backend_instance)
                if not success:
                    raise RuntimeError("Unable to update Russound hardware")
            elif setting == "treble":
                success = zone.set_treble(max(-10, min(10, int(value))), backend=backend_instance)
                if not success:
                    raise RuntimeError("Unable to update Russound hardware")
            elif setting == "loudness":
                success = zone.set_loudness(bool(value), backend=backend_instance)
                if not success:
                    raise RuntimeError("Unable to update Russound hardware")
            elif setting == "balance":
                success = zone.set_balance(max(-10, min(10, int(value))), backend=backend_instance)
                if not success:
                    raise RuntimeError("Unable to update Russound hardware")
            else:
                raise ValueError(f"Unsupported setting: {setting}")
            break
        return self.sync_system_power()

    def apply_shortcut(self, shortcut: dict[str, Any], backend: Any | None = None) -> "RussoundState":
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        source = shortcut.get("source")
        for raw_address in shortcut.get("zone_addresses", []):
            if not isinstance(raw_address, dict):
                continue
            controller_id = raw_address.get("controller")
            zone_number = raw_address.get("zone")
            if not isinstance(controller_id, int) or not isinstance(zone_number, int):
                continue
            for zone in self.zones:
                if zone.controller != controller_id or zone.zone_number != zone_number:
                    continue
                zone.power = True
                zone.set_power(True, backend=backend)
                if isinstance(source, int):
                    zone.source = source
                    zone.set_source(source, self.inputs, backend=backend)
                break
        return self.sync_system_power()
