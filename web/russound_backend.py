from __future__ import annotations

from contextlib import nullcontext
import logging
import time
from typing import Any, Callable, Protocol, cast

try:
    from russound.russound import Russound as _RussoundRuntime  # pyright: ignore[reportMissingTypeStubs]
except Exception as exc:  # pragma: no cover - defensive import fallback
    _RussoundRuntime = None
    _russound_import_error = exc
else:
    _russound_import_error = None

Russound = _RussoundRuntime

from .config_types import RussoundConfig, coerce_russound_config, resolve_backend_endpoint, resolve_controller_zone_limits
from .zone import Zone


_ZONE_INFO_REQUEST_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 01 04 02 00 @zz 07 00 00"
_ZONE_INFO_RESPONSE_SIGNATURE = "04 02 00 @zz 07"
_ZONE_USER_PARAMETER_REQUEST_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 01 05 02 00 @zz 00 @pp 00 00"
_ZONE_USER_PARAMETER_RESPONSE_SIGNATURE = "05 02 00 @zz 00 @pp"
_ZONE_USER_PARAMETER_SET_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 00 05 02 00 @zz 00 @pp 00 00 00 01 00 01 00 @pr"
_ZONE_USER_PARAMETER_PATHS = {
    "bass": 0x00,
    "treble": 0x01,
    "loudness": 0x02,
    "balance": 0x03,
    "turn_on_volume": 0x04,
}


class RussoundClientProtocol(Protocol):
    lock: Any
    sock: Any | None

    def connect(self) -> bool: ...
    def is_connected(self) -> bool: ...
    def get_power(self, controller: int, zone_number: int) -> object: ...
    def get_source(self, controller: int, zone_number: int) -> object: ...
    def get_volume(self, controller: int, zone_number: int) -> object: ...
    def set_power(self, controller: int, zone_number: int, power: int) -> None: ...
    def set_source(self, controller: int, zone_number: int, source_index: int) -> None: ...
    def set_volume(self, controller: int, zone_number: int, volume: int) -> None: ...
    def all_on_off(self, power: int) -> None: ...
    def _Russound__create_response_signature(self, template: str, zone_number: int) -> Any: ...
    def _Russound__create_send_message(self, template: str, controller: int, zone_number: int | None = None, parameter: int | None = None) -> Any: ...
    def _Russound__send_data(self, send_msg: Any) -> None: ...
    def _Russound__get_response_message(self, signature: Any = ...) -> Any: ...


class RussoundBackend:
    _next_connect_attempt_at = 0.0
    _connect_backoff_seconds = 2.0
    _default_host = "127.0.0.1"
    _default_port = 6666

    def __init__(self, config: object | None = None) -> None:
        """Create a backend wrapper for a Russound controller connection.

        Args:
            config: Optional controller and backend endpoint configuration.
        """
        self.config: RussoundConfig | None = coerce_russound_config(config)
        endpoint = resolve_backend_endpoint(self.config)
        self.host = endpoint.host
        self.port = endpoint.port
        self.client: RussoundClientProtocol | None = None
        self._connectivity_state = "idle"
        self._last_connectivity_detail: str | None = None

        if endpoint.loaded_from_config:
            logging.debug("Loaded Russound backend endpoint from config: %s:%d", self.host, self.port)

    def _log_connectivity_state(self, state: str, message: str, *args: object, detail: str | None = None) -> None:
        if self._connectivity_state == state and self._last_connectivity_detail == detail:
            return
        logging.debug(message, *args)
        self._connectivity_state = state
        self._last_connectivity_detail = detail

    def _connect(self) -> RussoundClientProtocol | None:
        """Open a new Russound client connection when the backend library is available."""
        if self.client is not None:
            try:
                if self.client.is_connected():
                    self._connectivity_state = "connected"
                    self._last_connectivity_detail = None
                    return self.client
            except Exception:
                pass
            self._close_client(self.client)

        if Russound is None:
            error_detail = str(_russound_import_error)
            self._log_connectivity_state(
                "library-missing",
                "Russound backend library is not available: %s",
                error_detail,
                detail=error_detail,
            )
            self.client = None
            return None

        now = time.monotonic()
        if now < self._next_connect_attempt_at:
            self.client = None
            return None

        try:
            client = cast(RussoundClientProtocol, Russound(self.host, self.port))
            logging.getLogger("russound.russound").setLevel(logging.CRITICAL)
            connected = client.connect()
            if not connected or not client.is_connected():
                self._next_connect_attempt_at = time.monotonic() + self._connect_backoff_seconds
                self._close_client(client)
                self._log_connectivity_state(
                    "connect-failed",
                    "Russound backend unavailable at %s:%d; retrying in %.1fs",
                    self.host,
                    self.port,
                    self._connect_backoff_seconds,
                    detail="connect-failed",
                )
                self.client = None
                return None
            self._next_connect_attempt_at = 0.0
            self.client = client
            self._log_connectivity_state(
                "connected",
                "Connected to Russound backend at %s:%d",
                self.host,
                self.port,
            )
            return client
        except Exception as exc:  # pragma: no cover - runtime dependency may be absent
            self._next_connect_attempt_at = time.monotonic() + self._connect_backoff_seconds
            self.client = None
            error_detail = str(exc)
            self._log_connectivity_state(
                "connect-exception",
                "Russound backend unavailable at %s:%d; retrying in %.1fs: %s",
                self.host,
                self.port,
                self._connect_backoff_seconds,
                error_detail,
                detail=error_detail,
            )
            return None

    def _source_index(self, source_id: int, inputs: list[dict[str, Any]]) -> int:
        """Resolve a configured input id to the zero-based index expected by the Russound API.

        Args:
            source_id: Explicit input id to resolve.
            inputs: List of configured input definitions used for lookup.
        """
        for _, input_item in enumerate(inputs):
            if input_item.get("id") == source_id:
                return source_id - 1
        raise ValueError(f"Source {source_id} is not configured")

    def _client_lock_context(self, client: RussoundClientProtocol):
        lock = getattr(client, "lock", None)
        return lock if lock is not None else nullcontext()

    def is_connected(self) -> bool:
        return self.client is not None

    def connect(self) -> bool:
        return self._connect() is not None

    def close(self) -> None:
        self._close_client()

    def _close_client(self, client: RussoundClientProtocol | None = None) -> None:
        client_to_close = self.client if client is None else client
        if client_to_close is None:
            self.client = None
            return

        if self.client is client_to_close:
            self.client = None

        try:
            disconnect = getattr(client_to_close, "disconnect", None)
            if callable(disconnect):
                disconnect()
                return

            close = getattr(client_to_close, "close", None)
            if callable(close):
                close()
                return

            sock = getattr(client_to_close, "sock", None)
            if sock is not None:
                sock_close = getattr(sock, "close", None)
                if callable(sock_close):
                    sock_close()
        except Exception as exc:  # pragma: no cover - defensive cleanup fallback
            logging.debug("Unable to disconnect Russound backend client cleanly: %s", exc)

    def _disconnect(self) -> None:
        self._close_client()


    def _request_zone_info_message(self, client: Any, controller: int, zone_number: int) -> Any | None:
        create_signature = getattr(client, "_Russound__create_response_signature", None)
        create_message = getattr(client, "_Russound__create_send_message", None)
        send_data = getattr(client, "_Russound__send_data", None)
        get_response = getattr(client, "_Russound__get_response_message", None)
        if not all(callable(method) for method in (create_signature, create_message, send_data, get_response)):
            return None

        create_signature = cast(Callable[[str, int], Any], create_signature)
        create_message = cast(Callable[[str, int, int], Any], create_message)
        send_data = cast(Callable[[Any], None], send_data)
        get_response = cast(Callable[[Any], Any], get_response)

        response_signature = create_signature(_ZONE_INFO_RESPONSE_SIGNATURE, zone_number)
        send_msg = create_message(_ZONE_INFO_REQUEST_TEMPLATE, controller, zone_number)
        with self._client_lock_context(client):
            send_data(send_msg)
            return get_response(response_signature)

    def _request_zone_user_parameter_message(self, client: Any, controller: int, zone_number: int, parameter: str) -> Any | None:
        parameter_path = _ZONE_USER_PARAMETER_PATHS.get(parameter)
        if parameter_path is None:
            raise ValueError(f"Unsupported zone parameter: {parameter}")

        create_signature = getattr(client, "_Russound__create_response_signature", None)
        create_message = getattr(client, "_Russound__create_send_message", None)
        send_data = getattr(client, "_Russound__send_data", None)
        get_response = getattr(client, "_Russound__get_response_message", None)
        if not all(callable(method) for method in (create_signature, create_message, send_data, get_response)):
            return None

        create_signature = cast(Callable[[str, int], Any], create_signature)
        create_message = cast(Callable[[str, int, int], Any], create_message)
        send_data = cast(Callable[[Any], None], send_data)
        get_response = cast(Callable[[Any], Any], get_response)

        parameter_hex = f"{parameter_path:02X}"
        response_signature = create_signature(_ZONE_USER_PARAMETER_RESPONSE_SIGNATURE.replace("@pp", parameter_hex), zone_number)
        send_msg = create_message(_ZONE_USER_PARAMETER_REQUEST_TEMPLATE.replace("@pp", parameter_hex), controller, zone_number)
        with self._client_lock_context(client):
            send_data(send_msg)
            return get_response(response_signature)

    def _parse_zone_info_message(self, message: Any) -> dict[str, Any] | None:
        if message is None or len(message) < 22:
            return None
        return {
            "power": bool(message[11]),
            "source_index": int(message[12]),
            "volume": int(message[13]) * 2,
            "bass": int(message[14]) - 10,
            "treble": int(message[15]) - 10,
            "loudness": bool(message[16]),
            "balance": int(message[17]) - 10,
            "system_power": bool(message[18]),
            "shared_source": bool(message[19]),
        }

    def _parse_zone_user_parameter_value(self, parameter: str, message: Any) -> Any | None:
        if message is None or len(message) < 13:
            return None
        raw_value = int(message[12])
        if parameter in {"bass", "treble", "balance"}:
            return raw_value - 10
        if parameter == "turn_on_volume":
            return raw_value * 2
        if parameter == "loudness":
            return bool(raw_value)
        return raw_value

    def _normalize_zone_user_parameter_value(self, parameter: str, value: Any) -> int:
        if parameter in {"bass", "treble", "balance"}:
            return max(-10, min(10, int(value))) + 10
        if parameter == "turn_on_volume":
            return max(0, min(100, int(value))) // 2
        if parameter == "loudness":
            return 1 if bool(value) else 0
        raise ValueError(f"Unsupported zone parameter: {parameter}")

    def _controller_zone_limits(self) -> dict[int, int]:
        return resolve_controller_zone_limits(self.config)

    def _validate_zone_address(self, controller: int, zone_number: int, controller_zone_limits: dict[int, int]) -> tuple[int, int]:
        controller_id = int(controller)
        zone_number_value = int(zone_number)
        if not 1 <= controller_id <= 6:
            raise ValueError(f"Unsupported controller id: {controller_id}")
        if not controller_zone_limits:
            if not 1 <= zone_number_value <= 6:
                raise ValueError(f"Unsupported zone number: {zone_number_value}")
            return controller_id, zone_number_value
        if controller_id not in controller_zone_limits:
            raise ValueError(f"Controller {controller_id} is not configured in the backend scope")
        zone_limit = controller_zone_limits[controller_id]
        if not 1 <= zone_number_value <= zone_limit:
            raise ValueError(f"Zone number {zone_number_value} is out of scope for controller {controller_id}")
        return controller_id, zone_number_value

    def is_address_in_scope(self, address: tuple[int, int] | None = None) -> bool:
        """Return True when a controller/zone tuple is within the configured scope."""
        if not isinstance(address, tuple) or len(address) != 2:
            return False
        controller, zone_number = address
        try:
            self._validate_zone_address(controller, zone_number, self._controller_zone_limits())
        except ValueError:
            return False
        return True

    def _resolve_zone_address(self, zone: Zone) -> tuple[int, int]:
        """Translate a logical room mapping to a concrete controller and zone number."""
        controller, zone_number = zone.address
        return self._validate_zone_address(controller, zone_number, self._controller_zone_limits())

    def read_zone(self, zone: Zone, inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Read the current power, source, and volume state for one room.

        Args:
            zone: Zone definition with controller and zone mapping.
            inputs: Available input definitions for source-name resolution.
            config: Configuration used to validate controller and zone bounds.
        """
        client = self._connect()
        if client is None:
            return None
        try:
            controller, zone_number = self._resolve_zone_address(zone)
            zone_info = self._parse_zone_info_message(self._request_zone_info_message(client, controller, zone_number))
            if zone_info is not None:
                source_index = zone_info.get("source_index")
                source_id = None
                if isinstance(source_index, int) and 0 <= source_index < len(inputs):
                    source_id = inputs[source_index].get("id")
                return {
                    "power": bool(zone_info.get("power", False)),
                    "source": source_id or (inputs[0].get("id") if inputs else ""),
                    "volume": int(zone_info.get("volume", 0)),
                    "bass": int(zone_info.get("bass", 0)),
                    "treble": int(zone_info.get("treble", 0)),
                    "loudness": bool(zone_info.get("loudness", False)),
                    "balance": int(zone_info.get("balance", 0)),
                }
            try:
                power = client.get_power(controller, zone_number)
                source_index = client.get_source(controller, zone_number)
                volume = client.get_volume(controller, zone_number)
                source_id = None
                volume_value = int(volume) if isinstance(volume, int) else 0
                if isinstance(source_index, int) and 0 <= source_index < len(inputs):
                    source_id = inputs[source_index].get("id")
                return {
                    "power": bool(power),
                    "source": source_id or (inputs[0].get("id") if inputs else ""),
                    "volume": volume_value,
                    "bass": 0,
                    "treble": 0,
                    "loudness": False,
                    "balance": 0,
                }
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to read Russound zone %s: %s", zone_number, exc)
                return None
        finally:
            if self.client is not client:
                self._close_client(client)

    def read_zone_parameters(self, zone: Zone) -> dict[str, Any] | None:
        """Read extended CAA66 zone parameters such as bass, treble, and balance.

        Returns normalized values where applicable:
        - `power`, `volume`: the current zone power and volume state
        - `bass`, `treble`, `balance`: `-10..10`
        - `turn_on_volume`: `0..100`
        - `loudness`: booleans
        """
        client = self._connect()
        if client is None:
            return None
        try:
            controller, zone_number = self._resolve_zone_address(zone)

            zone_info = self._parse_zone_info_message(self._request_zone_info_message(client, controller, zone_number))
            if zone_info is None:
                return None

            zone_info["power"] = bool(zone_info.get("power", False))
            zone_info["volume"] = max(0, min(100, int(zone_info.get("volume", 0))))

            discrete_parameters = {
                "turn_on_volume",
            }
            for parameter in discrete_parameters:
                value = self._parse_zone_user_parameter_value(
                    parameter,
                    self._request_zone_user_parameter_message(client, controller, zone_number, parameter),
                )
                if value is None:
                    return None
                zone_info[parameter] = value

            return zone_info
        finally:
            if self.client is not None:
                self._disconnect()

    def read_zone_user_parameter(self, zone: Zone, parameter: str) -> Any | None:
        """Read one extended CAA66 zone parameter by name."""
        if parameter not in _ZONE_USER_PARAMETER_PATHS and parameter not in {"power", "volume", "bass", "treble", "loudness", "balance", "system_power", "shared_source"}:
            return None

        controller, zone_number = self._resolve_zone_address(zone)

        if parameter in {"power", "volume", "bass", "treble", "loudness", "balance", "system_power", "shared_source"}:
            zone_info = self.read_zone_parameters(zone)
            return None if zone_info is None else zone_info.get(parameter)

        client = self._connect()
        if client is None:
            return None
        try:
            return self._parse_zone_user_parameter_value(
                parameter,
                self._request_zone_user_parameter_message(client, controller, zone_number, parameter),
            )
        finally:
            if self.client is not client:
                self._close_client(client)

    def read_zone_bass(self, zone: Zone) -> int | None:
        return self.read_zone_user_parameter(zone, "bass")

    def read_zone_treble(self, zone: Zone) -> int | None:
        return self.read_zone_user_parameter(zone, "treble")

    def read_zone_balance(self, zone: Zone) -> int | None:
        return self.read_zone_user_parameter(zone, "balance")

    def read_zone_loudness(self, zone: Zone) -> bool | None:
        return self.read_zone_user_parameter(zone, "loudness")

    def read_zone_turn_on_volume(self, zone: Zone) -> int | None:
        return self.read_zone_user_parameter(zone, "turn_on_volume")

    def set_zone_power(self, zone: Zone, power: bool) -> bool:
        """Switch the given room on or off in the Russound system.

        Args:
            zone: Zone definition with its mapped controller and zone number.
            power: True to switch the room on, False to switch it off.
        """
        client = self._connect()
        if client is None:
            return False
        try:
            controller, zone_number = self._resolve_zone_address(zone)
            try:
                client.set_power(controller, zone_number, 1 if power else 0)
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to set power for Russound controller %d - zone %d: %s", controller, zone_number, exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)

    def set_zone_source(self, zone: Zone, source_id: int, inputs: list[dict[str, Any]]) -> bool:
        """Set the input source for a single room.

        Args:
            zone: Zone definition containing the target controller and zone.
            source_id: Explicit input id to apply.
            inputs: Configured input list used to resolve the source.
        """
        client = self._connect()
        if client is None:
            return False
        try:
            controller, zone_number = self._resolve_zone_address(zone)
            try:
                source_index = self._source_index(source_id, inputs)
            except ValueError:
                return False
            try:
                client.set_source(controller, zone_number, source_index)
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to set source for Russound controller %d - zone %d: %s", controller, zone_number, exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)

    def set_zone_volume(self, zone: Zone, volume: int) -> bool:
        """Set the volume of one room to a value between 0 and 100.

        Args:
            zone: Zone definition with the mapped Russound controller/zone.
            volume: Desired volume level, clamped to the supported range.
        """
        client = self._connect()
        if client is None:
            return False
        try:
            controller, zone_number = self._resolve_zone_address(zone)
            try:
                client.set_volume(controller, zone_number, max(0, min(100, volume)))
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to set volume for Russound controller %d - zone %d: %s", controller, zone_number, exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)

    def set_zone_user_parameter(self, zone: Zone, parameter: str, value: Any) -> bool:
        """Set one CAA66 zone user parameter using the direct data-message form."""
        client = self._connect()
        if client is None:
            return False
        try:
            controller, zone_number = self._resolve_zone_address(zone)
            parameter_path = _ZONE_USER_PARAMETER_PATHS.get(parameter)
            if parameter_path is None:
                return False
            create_message = getattr(client, "_Russound__create_send_message", None)
            send_data = getattr(client, "_Russound__send_data", None)
            get_response = getattr(client, "_Russound__get_response_message", None)
            if not all(callable(method) for method in (create_message, send_data, get_response)):
                return False

            create_message = cast(Callable[[str, int, int, int], Any], create_message)
            send_data = cast(Callable[[Any], None], send_data)
            get_response = cast(Callable[[], Any], get_response)
            normalized_value = self._normalize_zone_user_parameter_value(parameter, value)
            template = _ZONE_USER_PARAMETER_SET_TEMPLATE.replace("@pp", f"{parameter_path:02X}")
            send_msg = create_message(template, controller, zone_number, normalized_value)
            try:
                with self._client_lock_context(client):
                    send_data(send_msg)
                    get_response()
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to set %s for Russound controller %d - zone %d: %s", parameter, controller, zone_number, exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)

    def set_zone_treble(self, zone: Zone, treble: int) -> bool:
        return self.set_zone_user_parameter(zone, "treble", treble)

    def set_zone_bass(self, zone: Zone, bass: int) -> bool:
        return self.set_zone_user_parameter(zone, "bass", bass)

    def set_zone_loudness(self, zone: Zone, loudness: bool) -> bool:
        return self.set_zone_user_parameter(zone, "loudness", loudness)

    def set_zone_balance(self, zone: Zone, balance: int) -> bool:
        return self.set_zone_user_parameter(zone, "balance", balance)

    def turn_all_zones_off(self) -> bool:
        """Turn every zone off for the default controller."""
        client = self._connect()
        if client is None:
            return False
        try:
            try:
                client.all_on_off(0)
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to turn Russound zones off: %s", exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)

    def turn_all_zones_on(self, zones: list[Zone] | None = None) -> bool:
        """Turn on only the provided enabled zones for the default controller."""
        client = self._connect()
        if client is None:
            return False
        try:
            try:
                if not zones:
                    return True
                for zone_data in zones:
                    client.set_power(zone_data.controller, zone_data.zone_number, 1)
                return True
            except Exception as exc:  # pragma: no cover - runtime dependency may be absent
                logging.debug("Unable to turn Russound zones on: %s", exc)
                return False
        finally:
            if self.client is not client:
                self._close_client(client)
