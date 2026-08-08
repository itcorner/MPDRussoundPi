from __future__ import annotations

import logging
import time
from typing import Any

try:
    from russound.russound import Russound
except Exception as exc:  # pragma: no cover - defensive import fallback
    Russound = None
    _RUSSOUND_IMPORT_ERROR = exc
else:
    _RUSSOUND_IMPORT_ERROR = None

from .zone import Zone


class RussoundBackend:
    _next_connect_attempt_at = 0.0
    _connect_backoff_seconds = 2.0

    def __init__(self, host: str = "127.0.0.1", port: int = 6666, controller: int = 1) -> None:
        """Create a backend wrapper for a Russound controller connection.

        Args:
            host: TCP host name or IP address of the Russound gateway.
            port: TCP port exposed by the gateway.
            controller: Default controller id used when a zone mapping is missing.
        """
        self.host = host
        self.port = int(port)
        self.controller = int(controller)

    def _connect(self):
        """Open a new Russound client connection when the backend library is available."""
        if Russound is None:
            return None
        now = time.monotonic()
        if now < self._next_connect_attempt_at:
            return None
        try:
            client = Russound(self.host, self.port)
            logging.getLogger("russound.russound").setLevel(logging.CRITICAL)
            connected = client.connect()
            if not connected or not client.is_connected():
                self._next_connect_attempt_at = time.monotonic() + self._connect_backoff_seconds
                return None
            self._next_connect_attempt_at = 0.0
            return client
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            self._next_connect_attempt_at = time.monotonic() + self._connect_backoff_seconds
            logging.debug("Unable to connect to Russound backend: %s", exc)
            return None

    def _source_index(self, source_id: int | None, inputs: list[dict[str, Any]]) -> int | None:
        """Resolve a configured input id to the zero-based index expected by the Russound API.

        Args:
            source_id: Explicit input id to resolve.
            inputs: List of configured input definitions used for lookup.
        """
        if not isinstance(source_id, int):
            return None
        for index, input_item in enumerate(inputs):
            if input_item.get("id") == source_id:
                return index
        return None

    def _resolve_zone_address(self, zone: dict[str, Any] | Zone, config: dict[str, Any] | None = None) -> tuple[int, int]:
        """Translate a logical room mapping to a concrete controller and zone number.

        Args:
            zone: Zone definition containing controller and zone values.
            config: Full configuration containing controller zone-count limits.
        """
        if isinstance(zone, Zone):
            controller = zone.controller
            zone_number = zone.zone_number
        else:
            controller = int(zone.get("controller", 1))
            zone_number = int(zone.get("zone", 1))
        if isinstance(config, dict):
            controller_limits = {}
            for controller_config in config.get("controllers", []):
                if not isinstance(controller_config, dict):
                    continue
                controller_id = int(controller_config.get("id", 1))
                if 1 <= controller_id <= 4:
                    controller_limits[controller_id] = int(controller_config.get("zone_count", 8))
            if controller_limits:
                controller = controller if controller in controller_limits else 1
                zone_limit = controller_limits.get(controller, 8)
                zone_number = max(1, min(zone_number, zone_limit))
        return controller, zone_number

    def read_zone(self, zone: dict[str, Any] | Zone, inputs: list[dict[str, Any]], config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Read the current power, source, volume, and mute state for one room.

        Args:
            zone: Zone definition with controller and zone mapping.
            inputs: Available input definitions for source-name resolution.
            config: Configuration used to validate controller and zone bounds.
        """
        client = self._connect()
        if client is None:
            return None
        controller, zone_number = self._resolve_zone_address(zone, config)
        try:
            power = client.get_power(controller, zone_number)
            source_index = client.get_source(controller, zone_number)
            volume = client.get_volume(controller, zone_number)
            source_id = None
            if source_index is not None and 0 <= source_index < len(inputs):
                source_id = inputs[source_index].get("id")
            return {
                "power": bool(power),
                "source": source_id or (inputs[0].get("id") if inputs else ""),
                "volume": int(volume or 0),
                "muted": False,
            }
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to read Russound zone %s: %s", zone_number, exc)
            return None

    def set_zone_power(self, zone: dict[str, Any] | Zone, power: bool) -> bool:
        """Switch the given room on or off in the Russound system.

        Args:
            zone: Zone definition with its mapped controller and zone number.
            power: True to switch the room on, False to switch it off.
        """
        client = self._connect()
        if client is None:
            return False
        controller, zone_number = self._resolve_zone_address(zone)
        try:
            client.set_power(controller, zone_number, 1 if power else 0)
            return True
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to set Russound power for zone %s: %s", zone_number, exc)
            return False

    def set_zone_source(self, zone: dict[str, Any] | Zone, source_id: int | None, inputs: list[dict[str, Any]]) -> bool:
        """Set the input source for a single room.

        Args:
            zone: Zone definition containing the target controller and zone.
            source_id: Explicit input id to apply.
            inputs: Configured input list used to resolve the source.
        """
        client = self._connect()
        if client is None:
            return False
        controller, zone_number = self._resolve_zone_address(zone)
        source_index = self._source_index(source_id, inputs)
        if source_index is None:
            return False
        try:
            client.set_source(controller, zone_number, source_index)
            return True
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to set Russound source for zone %s: %s", zone_number, exc)
            return False

    def set_zone_volume(self, zone: dict[str, Any] | Zone, volume: int) -> bool:
        """Set the volume of one room to a value between 0 and 100.

        Args:
            zone: Zone definition with the mapped Russound controller/zone.
            volume: Desired volume level, clamped to the supported range.
        """
        client = self._connect()
        if client is None:
            return False
        controller, zone_number = self._resolve_zone_address(zone)
        try:
            client.set_volume(controller, zone_number, max(0, min(100, volume)))
            return True
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to set Russound volume for zone %s: %s", zone_number, exc)
            return False

    def set_zone_mute(
        self,
        zone: dict[str, Any] | Zone,
        muted: bool,
        current_muted: bool | None = None,
    ) -> bool:
        """Set the mute state for a mapped room.

        Args:
            zone: Zone definition that resolves to the target controller and zone.
            muted: Desired mute state.
            current_muted: Optional prior mute state used to avoid unnecessary toggles.
        """
        client = self._connect()
        if client is None:
            return False
        controller, zone_number = self._resolve_zone_address(zone)
        desired_muted = bool(muted)
        if current_muted is not None and bool(current_muted) == desired_muted:
            return True
        try:
            client.toggle_mute(controller, zone_number)
            return True
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to set Russound mute for zone %s: %s", zone_number, exc)
            return False

    def set_all_power(self, power: bool, zone_count: int) -> bool:
        """Turn every zone on or off for the default controller.

        Args:
            power: True to enable all zones, False to disable them.
            zone_count: Number of zones to update on the controller.
        """
        client = self._connect()
        if client is None:
            return False
        try:
            for zone in range(1, zone_count + 1):
                client.set_power(self.controller, zone, 1 if power else 0)
            return True
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            logging.debug("Unable to set Russound system power: %s", exc)
            return False
