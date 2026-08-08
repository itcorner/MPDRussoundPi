from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .russound_backend import RussoundBackend


class Zone:
    """Simple container for a zone's state and the actions that affect it."""

    def __init__(
        self,
        name: str,
        power: bool = False,
        source: int | None = None,
        volume: int = 20,
        controller: int = 1,
        zone_number: int = 1,
    ) -> None:
        self.name = name
        self.power = bool(power)
        self.source = source if isinstance(source, int) else None
        self.volume = max(0, min(100, int(volume)))
        self.controller = int(controller)
        self.zone_number = int(zone_number)

    @property
    def zone(self) -> int:
        return self.zone_number

    @property
    def address(self) -> tuple[int, int]:
        return self.controller, self.zone_number

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, default_source: int | None = None) -> "Zone":
        if not isinstance(data, dict):
            raise TypeError("Zone data must be a dictionary")
        return cls(
            name=str(data.get("name", "")),
            power=bool(data.get("power", False)),
            source=data.get("source", default_source),
            volume=int(data.get("volume", 20)),
            controller=int(data.get("controller", 1)),
            zone_number=int(data.get("zone", data.get("zone_number", 1))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "power": self.power,
            "source": self.source,
            "volume": self.volume,
            "controller": self.controller,
            "zone": self.zone_number,
        }

    def update_from_state(self, state: dict[str, Any] | None, default_source: int | None = None) -> "Zone":
        if not isinstance(state, dict):
            return self
        self.power = bool(state.get("power", self.power))
        self.source = state.get("source", self.source if self.source is not None else default_source)
        self.volume = max(0, min(100, int(state.get("volume", self.volume))))
        self.controller = int(state.get("controller", self.controller))
        self.zone_number = int(state.get("zone", self.zone_number))
        return self

    def apply_to_backend(self, backend: "RussoundBackend | None" = None) -> bool:
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        if not isinstance(backend, RussoundBackend):
            return False
        return backend.set_zone_power(self, self.power)

    def set_power(self, power: bool, backend: "RussoundBackend | None" = None) -> bool:
        self.power = bool(power)
        return self.apply_to_backend(backend)

    def set_source(self, source_id: int | None, inputs: list[dict[str, Any]], backend: "RussoundBackend | None" = None) -> bool:
        self.source = source_id if isinstance(source_id, int) else self.source
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_source(self, self.source, inputs)

    def set_volume(self, volume: int, backend: "RussoundBackend | None" = None) -> bool:
        self.volume = max(0, min(100, int(volume)))
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_volume(self, self.volume)
