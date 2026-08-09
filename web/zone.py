from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .russound_backend import RussoundBackend


class Zone:
    """Simple container for a zone's state and the actions that affect it."""

    @staticmethod
    def _normalize_source(source: Any) -> int:
        return source if type(source) is int else 0

    def __init__(
        self,
        name: str,
        power: bool = False,
        source: int | None = None,
        volume: int = 20,
        bass: int = 0,
        treble: int = 0,
        loudness: bool = False,
        balance: int = 0,
        controller: int = 1,
        zone_number: int = 1,
        enabled: bool = True,
        visible: bool = True,
    ) -> None:
        self.name = name
        self.power = bool(power)
        self.source = self._normalize_source(source)
        self.volume = max(0, min(100, int(volume)))
        self.bass = max(-10, min(10, int(bass)))
        self.treble = max(-10, min(10, int(treble)))
        self.loudness = bool(loudness)
        self.balance = max(-10, min(10, int(balance)))
        self.controller = int(controller)
        self.zone_number = int(zone_number)
        self.enabled = bool(enabled)
        self.visible = bool(visible)

    @property
    def address(self) -> tuple[int, int]:
        return self.controller, self.zone_number

    @classmethod
    def from_config_payload(cls, data: dict[str, Any] | None) -> "Zone":
        if not isinstance(data, dict):
            raise TypeError("Zone config payload must be a dictionary")
        return cls(
            name=str(data.get("name", "")),
            power=False,
            source=None,
            volume=20,
            bass=0,
            treble=0,
            loudness=False,
            balance=0,
            controller=int(data.get("controller", 1)),
            zone_number=int(data.get("zone", data.get("zone_number", 1))),
            enabled=bool(data.get("enabled", True)),
            visible=bool(data.get("visible", True)),
        )

    @classmethod
    def from_state_payload(cls, data: dict[str, Any] | None, default_source: int | None = None) -> "Zone":
        if not isinstance(data, dict):
            raise TypeError("Zone state payload must be a dictionary")
        return cls(
            name=str(data.get("name", "")),
            power=bool(data.get("power", False)),
            source=data.get("source", default_source),
            volume=int(data.get("volume", 20)),
            bass=int(data.get("bass", 0)),
            treble=int(data.get("treble", 0)),
            loudness=bool(data.get("loudness", False)),
            balance=int(data.get("balance", 0)),
            controller=int(data.get("controller", 1)),
            zone_number=int(data.get("zone", data.get("zone_number", 1))),
            enabled=bool(data.get("enabled", True)),
            visible=bool(data.get("visible", True)),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, default_source: int | None = None) -> "Zone":
        if not isinstance(data, dict):
            raise TypeError("Zone data must be a dictionary")
        if "power" in data or "source" in data or "volume" in data or "bass" in data or "treble" in data or "loudness" in data or "balance" in data:
            return cls.from_state_payload(data, default_source=default_source)
        return cls.from_config_payload(data)

    def to_config_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "controller": self.controller,
            "zone": self.zone_number,
            "enabled": self.enabled,
            "visible": self.visible,
        }

    def to_state_payload(self) -> dict[str, Any]:
        return {
            "power": self.power,
            "source": self.source,
            "volume": self.volume,
            "bass": self.bass,
            "treble": self.treble,
            "loudness": self.loudness,
            "balance": self.balance,
            "controller": self.controller,
            "zone": self.zone_number,
        }

    def to_frontend_payload(self) -> dict[str, Any]:
        payload = self.to_state_payload()
        payload["name"] = self.name
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.to_state_payload()

    def update_from_state(self, state: dict[str, Any] | None) -> "Zone":
        if not isinstance(state, dict):
            return self
        self.power = bool(state.get("power", self.power))
        self.source = self._normalize_source(state.get("source", self.source))
        self.volume = max(0, min(100, int(state.get("volume", self.volume))))
        self.bass = max(-10, min(10, int(state.get("bass", self.bass))))
        self.treble = max(-10, min(10, int(state.get("treble", self.treble))))
        self.loudness = bool(state.get("loudness", self.loudness))
        self.balance = max(-10, min(10, int(state.get("balance", self.balance))))
        self.controller = int(state.get("controller", self.controller))
        self.zone_number = int(state.get("zone", self.zone_number))
        return self

    def apply_to_backend(self, backend: "RussoundBackend | None" = None, inputs: list[dict[str, Any]] | None = None) -> bool:
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        self.source = self._normalize_source(self.source)

        if not self.set_power(self.power, backend=backend):
            return False
        if not self.set_source(self.source, inputs or [], backend=backend):
            return False
        if not self.set_volume(self.volume, backend=backend):
            return False
        if not self.set_bass(self.bass, backend=backend):
            return False
        if not self.set_treble(self.treble, backend=backend):
            return False
        if not self.set_loudness(self.loudness, backend=backend):
            return False
        return self.set_balance(self.balance, backend=backend)

    def set_power(self, power: bool, backend: "RussoundBackend | None" = None) -> bool:
        self.power = bool(power)
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_power(self, self.power)

    def set_source(self, source_id: int, inputs: list[dict[str, Any]], backend: "RussoundBackend | None" = None) -> bool:
        self.source = self._normalize_source(source_id)
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_source(self, self.source, inputs)

    def set_volume(self, volume: int, backend: "RussoundBackend | None" = None) -> bool:
        self.volume = max(0, min(100, int(volume)))
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_volume(self, self.volume)

    def set_bass(self, bass: int, backend: "RussoundBackend | None" = None) -> bool:
        self.bass = max(-10, min(10, int(bass)))
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_bass(self, self.bass)

    def set_treble(self, treble: int, backend: "RussoundBackend | None" = None) -> bool:
        self.treble = max(-10, min(10, int(treble)))
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_treble(self, self.treble)

    def set_loudness(self, loudness: bool, backend: "RussoundBackend | None" = None) -> bool:
        self.loudness = bool(loudness)
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_loudness(self, self.loudness)

    def set_balance(self, balance: int, backend: "RussoundBackend | None" = None) -> bool:
        self.balance = max(-10, min(10, int(balance)))
        from .russound_backend import RussoundBackend

        backend = backend or RussoundBackend()
        return backend.set_zone_balance(self, self.balance)
