from __future__ import annotations

import argparse
import curses
from collections import deque
import json
import logging
import socket
import threading
import socketserver
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, cast


LOG = logging.getLogger("tool.dummy_backend")
DEFAULT_STATE_PATH = Path(__file__).with_name("example_state.json")
DEFAULT_KEYPAD_ID = 1
ZONE_INFO_RESPONSE_SIGNATURE = ["04", "02", "00"]
USER_PARAMETER_RESPONSE_SIGNATURE = ["05", "02", "00"]
ZONE_FIELD_DEFS: tuple[tuple[str, str, str, int | None, int | None], ...] = (
    ("power", "Power", "bool", None, None),
    ("source", "Source", "int", 1, 8),
    ("volume", "Volume", "int", 0, 100),
    ("bass", "Bass", "int", -10, 10),
    ("treble", "Treble", "int", -10, 10),
    ("loudness", "Loudness", "bool", None, None),
    ("balance", "Balance", "int", -10, 10),
    ("turn_on_volume", "Turn On Volume", "int", 0, 100),
    ("background_color", "Background Color", "int", 0, 15),
    ("do_not_disturb", "Do Not Disturb", "bool", None, None),
    ("party_mode", "Party Mode", "int", 0, 2),
    ("shared_source", "Shared Source", "bool", None, None),
)


def _zero_based(value: int) -> int:
    return max(0, int(value) - 1)


def _to_hex_bytes(payload: bytes | bytearray) -> list[str]:
    return [f"{byte:02X}" for byte in payload]


def _from_hex_bytes(values: list[str]) -> bytes:
    return bytes(int(value, 16) for value in values)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _clamp_even(value: int, minimum: int, maximum: int) -> int:
    clamped_value = _clamp(value, minimum, maximum)
    if clamped_value % 2 != 0:
        if clamped_value >= maximum:
            clamped_value -= 1
        else:
            clamped_value += 1
    return _clamp(clamped_value, minimum, maximum)


class TuiLogBuffer:
    def __init__(self, max_lines: int = 500) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def append(self, message: str) -> None:
        with self._lock:
            self._lines.append(message)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class TuiLogHandler(logging.Handler):
    def __init__(self, log_buffer: TuiLogBuffer) -> None:
        super().__init__()
        self._log_buffer = log_buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_buffer.append(self.format(record))
        except Exception:
            self.handleError(record)


@dataclass
class ZoneState:
    power: bool = False
    source: int = 1
    volume: int = 20
    bass: int = 0
    treble: int = 0
    loudness: bool = False
    balance: int = 0
    turn_on_volume: int = 20
    background_color: int = 0
    do_not_disturb: bool = False
    party_mode: int = 0
    shared_source: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ZoneState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            power=bool(data.get("power", False)),
            source=int(data.get("source", 1) or 1),
            volume=_clamp_even(int(data.get("volume", 20) or 20), 0, 100),
            bass=int(data.get("bass", 0) or 0),
            treble=int(data.get("treble", 0) or 0),
            loudness=bool(data.get("loudness", False)),
            balance=int(data.get("balance", 0) or 0),
            turn_on_volume=_clamp_even(int(data.get("turn_on_volume", 20) or 20), 0, 100),
            background_color=int(data.get("background_color", 0) or 0),
            do_not_disturb=bool(data.get("do_not_disturb", False)),
            party_mode=int(data.get("party_mode", 0) or 0),
            shared_source=bool(data.get("shared_source", False)),
        )

    def to_display_payload(self) -> dict[str, int | bool]:
        return {
            "power": self.power,
            "source_index": max(0, int(self.source) - 1),
            "volume": max(0, min(100, int(self.volume))),
            "bass": max(-10, min(10, int(self.bass))),
            "treble": max(-10, min(10, int(self.treble))),
            "loudness": self.loudness,
            "balance": max(-10, min(10, int(self.balance))),
            "system_power": self.power,
            "shared_source": self.shared_source,
            "turn_on_volume": max(0, min(100, int(self.turn_on_volume))),
        }

    def zone_info_fields(self) -> dict[str, int]:
        return {
            "power": 1 if self.power else 0,
            "source_index": max(0, int(self.source) - 1),
            "volume": _clamp(int(self.volume) // 2, 0, 50),
            "bass": _clamp(int(self.bass) + 10, 0, 20),
            "treble": _clamp(int(self.treble) + 10, 0, 20),
            "loudness": 1 if self.loudness else 0,
            "balance": _clamp(int(self.balance) + 10, 0, 20),
            "system_power": 1 if self.power else 0,
            "shared_source": 1 if self.shared_source else 0,
        }

    def parameter_raw_value(self, parameter_id: int) -> int:
        if parameter_id == 0:
            return _clamp(int(self.bass) + 10, 0, 20)
        if parameter_id == 1:
            return _clamp(int(self.treble) + 10, 0, 20)
        if parameter_id == 2:
            return 1 if self.loudness else 0
        if parameter_id == 3:
            return _clamp(int(self.balance) + 10, 0, 20)
        if parameter_id == 4:
            return _clamp(int(self.turn_on_volume) // 2, 0, 50)
        return 0

    def apply_parameter_value(self, parameter_id: int, value: int) -> None:
        if parameter_id == 0:
            self.bass = value - 10
        elif parameter_id == 1:
            self.treble = value - 10
        elif parameter_id == 2:
            self.loudness = bool(value)
        elif parameter_id == 3:
            self.balance = value - 10
        elif parameter_id == 4:
            self.turn_on_volume = _clamp(value * 2, 0, 100)
        elif parameter_id == 5:
            self.background_color = value
        elif parameter_id == 6:
            self.do_not_disturb = bool(value)
        elif parameter_id == 7:
            self.party_mode = value

    def apply_power_value(self, power_value: int) -> None:
        self.power = bool(power_value)

    def apply_source_value(self, source_value: int) -> None:
        self.source = max(1, int(source_value) + 1)
        self.power = True

    def apply_volume_value(self, volume_value: int) -> None:
        self.volume = _clamp_even(int(volume_value) * 2, 0, 100)

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "power": self.power,
            "source": self.source,
            "volume": self.volume,
            "bass": self.bass,
            "treble": self.treble,
            "loudness": self.loudness,
            "balance": self.balance,
            "turn_on_volume": self.turn_on_volume,
            "background_color": self.background_color,
            "do_not_disturb": self.do_not_disturb,
            "party_mode": self.party_mode,
            "shared_source": self.shared_source,
        }

    def adjust_field(self, field_name: str, delta: int) -> None:
        if field_name == "power":
            if delta:
                self.power = not self.power
            return
        if field_name == "source":
            self.source = _clamp(self.source + delta, 1, 8)
            return
        if field_name == "volume":
            self.volume = _clamp_even(self.volume + (delta * 2), 0, 100)
            return
        if field_name == "bass":
            self.bass = _clamp(self.bass + delta, -10, 10)
            return
        if field_name == "treble":
            self.treble = _clamp(self.treble + delta, -10, 10)
            return
        if field_name == "loudness":
            if delta:
                self.loudness = not self.loudness
            return
        if field_name == "balance":
            self.balance = _clamp(self.balance + delta, -10, 10)
            return
        if field_name == "turn_on_volume":
            self.turn_on_volume = _clamp_even(self.turn_on_volume + (delta * 2), 0, 100)
            return
        if field_name == "background_color":
            self.background_color = _clamp(self.background_color + delta, 0, 15)
            return
        if field_name == "do_not_disturb":
            if delta:
                self.do_not_disturb = not self.do_not_disturb
            return
        if field_name == "party_mode":
            self.party_mode = _clamp(self.party_mode + delta, 0, 2)
            return
        if field_name == "shared_source":
            if delta:
                self.shared_source = not self.shared_source


@dataclass
class KeypadDisplayState:
    message: str = ""
    alignment: int = 0
    flash_time: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "KeypadDisplayState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            message=str(data.get("message", "")),
            alignment=_clamp(int(data.get("alignment", 0) or 0), 0, 1),
            flash_time=_clamp(int(data.get("flash_time", 0) or 0), 0, 0xFFFF),
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            "message": self.message,
            "alignment": self.alignment,
            "flash_time": self.flash_time,
        }


@dataclass
class DummyRussoundState:
    controllers: dict[int, dict[int, ZoneState]] = field(default_factory=lambda: {})
    keypad_overrides: dict[int, dict[int, dict[int, KeypadDisplayState]]] = field(default_factory=lambda: {})
    zone_update_callback: Callable[[int, int], None] | None = field(default=None, repr=False, compare=False)

    def set_zone_update_callback(self, callback: Callable[[int, int], None] | None) -> None:
        self.zone_update_callback = callback

    def notify_zone_update(self, controller_id: int, zone_number: int) -> None:
        if self.zone_update_callback is not None:
            self.zone_update_callback(controller_id, zone_number)

    @classmethod
    def from_file(cls, state_path: Path | None) -> "DummyRussoundState":
        if state_path is None or not state_path.exists():
            return cls()
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        controllers: dict[int, dict[int, ZoneState]] = {}
        raw_controllers = cast(dict[str, Any], data.get("controllers") or {})
        for controller_key, zone_map in raw_controllers.items():
            if not isinstance(zone_map, dict):
                continue
            try:
                controller_id = int(controller_key)
            except (TypeError, ValueError):
                continue
            controllers[controller_id] = {}
            raw_zone_map = cast(dict[str, Any], zone_map)
            for zone_key, zone_data in raw_zone_map.items():
                try:
                    zone_number = int(zone_key)
                except (TypeError, ValueError):
                    continue
                controllers[controller_id][zone_number] = ZoneState.from_dict(zone_data)

        keypad_overrides: dict[int, dict[int, dict[int, KeypadDisplayState]]] = {}
        raw_keypad_overrides = cast(dict[str, Any], data.get("keypad_overrides") or {})
        for controller_key, zone_map in raw_keypad_overrides.items():
            if not isinstance(zone_map, dict):
                continue
            try:
                controller_id = int(controller_key)
            except (TypeError, ValueError):
                continue
            keypad_overrides[controller_id] = {}
            raw_zone_map = cast(dict[str, Any], zone_map)
            for zone_key, keypad_map in raw_zone_map.items():
                if not isinstance(keypad_map, dict):
                    continue
                try:
                    zone_number = int(zone_key)
                except (TypeError, ValueError):
                    continue
                keypad_overrides[controller_id][zone_number] = {}
                raw_keypad_map = cast(dict[str, Any], keypad_map)
                display_state = raw_keypad_map.get(str(DEFAULT_KEYPAD_ID))
                if isinstance(display_state, dict):
                    keypad_overrides[controller_id][zone_number][DEFAULT_KEYPAD_ID] = KeypadDisplayState.from_dict(
                        cast(dict[str, Any], display_state)
                    )

        return cls(
            controllers=controllers,
            keypad_overrides=keypad_overrides,
        )

    def zone(self, controller_id: int, zone_number: int) -> ZoneState:
        controller = self.controllers.setdefault(controller_id, {})
        if zone_number not in controller:
            controller[zone_number] = ZoneState()
        return controller[zone_number]

    def zone_addresses(self) -> list[tuple[int, int]]:
        addresses: list[tuple[int, int]] = []
        for controller_id in sorted(self.controllers):
            for zone_number in sorted(self.controllers[controller_id]):
                addresses.append((controller_id, zone_number))
        return addresses

    def to_dict(self) -> dict[str, Any]:
        return {
            "controllers": {
                str(controller_id): {
                    str(zone_number): zone_state.as_dict()
                    for zone_number, zone_state in sorted(zone_map.items())
                }
                for controller_id, zone_map in sorted(self.controllers.items())
            },
            "keypad_overrides": {
                str(controller_id): {
                    str(zone_number): {
                        str(keypad_number): display_state.as_dict()
                        for keypad_number, display_state in sorted(keypad_map.items())
                    }
                    for zone_number, keypad_map in sorted(zone_map.items())
                }
                for controller_id, zone_map in sorted(self.keypad_overrides.items())
            },
        }

    def keypad_display(self, controller_id: int, zone_number: int, keypad_number: int) -> KeypadDisplayState:
        if keypad_number != DEFAULT_KEYPAD_ID:
            raise ValueError(f"Dummy backend only simulates keypad {DEFAULT_KEYPAD_ID}")
        controller = self.keypad_overrides.setdefault(controller_id, {})
        zone_map = controller.setdefault(zone_number, {})
        if keypad_number not in zone_map:
            zone_map[keypad_number] = KeypadDisplayState()
        return zone_map[keypad_number]

    def save_to_file(self, state_path: Path) -> None:
        state_path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    def _build_zone_info_response(self, controller_id: int, zone_number: int) -> list[str]:
        zone = self.zone(controller_id, zone_number)
        fields = zone.zone_info_fields()
        response = [
            "04",
            "02",
            "00",
            f"{_zero_based(zone_number):02X}",
            "07",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            f"{fields['power']:02X}",
            f"{fields['source_index']:02X}",
            f"{fields['volume']:02X}",
            f"{fields['bass']:02X}",
            f"{fields['treble']:02X}",
            f"{fields['loudness']:02X}",
            f"{fields['balance']:02X}",
            f"{fields['system_power']:02X}",
            f"{fields['shared_source']:02X}",
            "00",
            "00",
            "00",
            "F7",
        ]
        return response

    def build_zone_info_response(self, controller_id: int, zone_number: int) -> list[str]:
        return self._build_zone_info_response(controller_id, zone_number)

    def _build_discrete_get_response(self, controller_id: int, zone_number: int, parameter_id: int) -> list[str]:
        zone = self.zone(controller_id, zone_number)
        raw_value = zone.parameter_raw_value(parameter_id)
        response = [
            "05",
            "02",
            "00",
            f"{_zero_based(zone_number):02X}",
            "00",
            f"{parameter_id:02X}",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            f"{raw_value:02X}",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            "F7",
        ]
        return response

    def handle_frame(self, payload: list[str]) -> list[str] | None:
        if len(payload) < 2:
            return None

        if self._is_all_keypads_display_frame(payload):
            self._apply_all_keypads_display_frame(payload)
            return None

        if self._is_specific_keypad_display_frame(payload):
            self._apply_specific_keypad_display_frame(payload)
            return None

        if self._is_zone_info_request(payload):
            controller_id, zone_number = self._extract_controller_zone(payload, zone_index=11)
            if controller_id is None or zone_number is None:
                return None
            return self._build_zone_info_response(controller_id, zone_number)

        if self._is_user_parameter_get_request(payload):
            controller_id, zone_number = self._extract_controller_zone(payload, zone_index=11)
            if controller_id is None or zone_number is None:
                return None
            parameter_id = self._extract_hex(payload, 13)
            if parameter_id is None:
                return None
            return self._build_discrete_get_response(controller_id, zone_number, parameter_id)

        self._apply_set_frame(payload)
        if self._is_all_power_set_frame(payload):
            power_value = self._extract_hex(payload, 15)
            if power_value is not None:
                for controller_id, zone_number in self.zone_addresses():
                    self.notify_zone_update(controller_id, zone_number)
        elif self._is_power_set_frame(payload) or self._is_volume_set_frame(payload):
            controller_id, zone_number = self.request_target(payload)
            if controller_id is not None and zone_number is not None:
                self.notify_zone_update(controller_id, zone_number)
        return None

    def request_label(self, payload: list[str]) -> str | None:
        if self._is_all_keypads_display_frame(payload):
            return "display on all keypads"

        if self._is_specific_keypad_display_frame(payload):
            return "display on keypad"

        if self._is_zone_info_request(payload):
            return "get zone info"

        if self._is_user_parameter_get_request(payload):
            parameter_id = self._extract_hex(payload, 13)
            return self._parameter_name(parameter_id, "get")

        if self._is_all_power_set_frame(payload):
            return "set system power"

        if self._is_power_set_frame(payload):
            return "set power"

        if self._is_source_set_frame(payload):
            return "set source"

        if self._is_volume_set_frame(payload):
            return "set volume"

        if self._is_user_parameter_set_frame(payload):
            parameter_id = self._extract_hex(payload, 13)
            return self._parameter_name(parameter_id, "set")

        return None

    def request_target(self, payload: list[str]) -> tuple[int | None, int | None]:
        if self._is_zone_info_request(payload) or self._is_user_parameter_get_request(payload):
            return self._extract_controller_zone(payload, zone_index=11)

        if self._is_all_keypads_display_frame(payload):
            return None, None

        if self._is_all_power_set_frame(payload):
            return None, None

        if self._is_specific_keypad_display_frame(payload):
            return self._extract_controller_zone(payload, zone_index=2)

        if self._is_power_set_frame(payload) or self._is_volume_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 17)
            if controller_id is None or zone_number is None:
                return None, None
            return controller_id + 1, zone_number + 1

        if self._is_source_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 5)
            if controller_id is None or zone_number is None:
                return None, None
            return controller_id + 1, zone_number + 1

        if self._is_user_parameter_set_frame(payload):
            return self._extract_controller_zone(payload, zone_index=11)

        return None, None

    def _parameter_name(self, parameter_id: int | None, action: str) -> str | None:
        if parameter_id is None:
            return None

        parameter_labels = {
            0: "bass",
            1: "treble",
            2: "loudness",
            3: "balance",
            4: "turn on volume",
            5: "background color",
            6: "do not disturb",
            7: "party mode",
        }
        parameter_label = parameter_labels.get(parameter_id)
        if parameter_label is None:
            return None
        return f"{action} {parameter_label}"

    def _apply_set_frame(self, payload: list[str]) -> None:
        if self._is_all_power_set_frame(payload):
            power_value = self._extract_hex(payload, 15)
            if power_value is None:
                return
            for controller_id, zone_number in self.zone_addresses():
                self.zone(controller_id, zone_number).apply_power_value(power_value)
            return

        if self._is_power_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 17)
            power_value = self._extract_hex(payload, 15)
            if controller_id is None or zone_number is None or power_value is None:
                return
            self.zone(controller_id + 1, zone_number + 1).apply_power_value(power_value)
            return

        if self._is_source_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 5)
            source_value = self._extract_hex(payload, 17)
            if controller_id is None or zone_number is None or source_value is None:
                return
            self.zone(controller_id + 1, zone_number + 1).apply_source_value(source_value)
            return

        if self._is_volume_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 17)
            volume_value = self._extract_hex(payload, 15)
            if controller_id is None or zone_number is None or volume_value is None:
                return
            self.zone(controller_id + 1, zone_number + 1).apply_volume_value(volume_value)
            return

        if self._is_user_parameter_set_frame(payload):
            controller_id = self._extract_hex(payload, 1)
            zone_number = self._extract_hex(payload, 11)
            parameter_id = self._extract_hex(payload, 13)
            value = self._extract_hex(payload, 21)
            if controller_id is None or zone_number is None or parameter_id is None or value is None:
                return
            self.zone(controller_id + 1, zone_number + 1).apply_parameter_value(parameter_id, value)

    def _apply_all_keypads_display_frame(self, payload: list[str]) -> None:
        display_state = self._decode_display_payload(payload)

        for controller_id, zone_number in self.zone_addresses():
            keypad_state = self.keypad_display(controller_id, zone_number, DEFAULT_KEYPAD_ID)
            keypad_state.message = display_state.message
            keypad_state.alignment = display_state.alignment
            keypad_state.flash_time = display_state.flash_time

    def _apply_specific_keypad_display_frame(self, payload: list[str]) -> None:
        controller_id = self._extract_hex(payload, 1)
        zone_number = self._extract_hex(payload, 2)
        keypad_number = self._extract_hex(payload, 3)
        if controller_id is None or zone_number is None or keypad_number is None:
            return
        if keypad_number + 1 != DEFAULT_KEYPAD_ID:
            return

        display_state = self._decode_display_payload(payload)
        keypad_state = self.keypad_display(controller_id + 1, zone_number + 1, DEFAULT_KEYPAD_ID)
        keypad_state.message = display_state.message
        keypad_state.alignment = display_state.alignment
        keypad_state.flash_time = display_state.flash_time

    def _decode_display_payload(self, payload: list[str]) -> KeypadDisplayState:
        alignment = self._extract_hex(payload, 18) or 0
        flash_low = self._extract_hex(payload, 19) or 0
        flash_high = self._extract_hex(payload, 20) or 0
        flash_time = flash_low | (flash_high << 8)
        text_bytes = bytes(self._extract_hex(payload, index) or 0 for index in range(21, 34))
        message = text_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return KeypadDisplayState(message=message, alignment=alignment, flash_time=flash_time)

    def _extract_hex(self, payload: list[str], index: int) -> int | None:
        if index >= len(payload):
            return None
        try:
            return int(payload[index], 16)
        except ValueError:
            return None

    def _extract_controller_zone(self, payload: list[str], zone_index: int) -> tuple[int | None, int | None]:
        controller_id = self._extract_hex(payload, 1)
        zone_number = self._extract_hex(payload, zone_index)
        if controller_id is None or zone_number is None:
            return None, None
        return controller_id + 1, zone_number + 1

    def _is_zone_info_request(self, payload: list[str]) -> bool:
        return len(payload) >= 17 and payload[0:15] == ["F0", payload[1], "00", "7F", "00", "00", "70", "01", "04", "02", "00", payload[11], "07", "00", "00"]

    def _is_user_parameter_get_request(self, payload: list[str]) -> bool:
        return len(payload) >= 18 and payload[0:16] == ["F0", payload[1], "00", "7F", "00", "00", "70", "01", "05", "02", "00", payload[11], "00", payload[13], "00", "00"]

    def _is_power_set_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 22 and payload[0:20] == ["F0", payload[1], "00", "7F", "00", "00", "70", "05", "02", "02", "00", "00", "F1", "23", "00", payload[15], "00", payload[17], "00", "01"]

    def _is_all_power_set_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 21 and payload[0:20] == ["F0", "7E", "00", "7F", "00", "00", "70", "05", "02", "02", "00", "00", "F1", "22", "00", payload[15], "00", "00", "00", "01"]

    def _is_source_set_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 22 and payload[0:20] == ["F0", payload[1], "00", "7F", "00", payload[5], "70", "05", "02", "00", "00", "00", "F1", "3E", "00", "00", "00", payload[17], "00", "01"]

    def _is_volume_set_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 22 and payload[0:20] == ["F0", payload[1], "00", "7F", "00", "00", "70", "05", "02", "02", "00", "00", "F1", "21", "00", payload[15], "00", payload[17], "00", "01"]

    def _is_user_parameter_set_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 24 and payload[0:22] == ["F0", payload[1], "00", "7F", "00", "00", "70", "00", "05", "02", "00", payload[11], "00", payload[13], "00", "00", "00", "01", "00", "01", "00", payload[21]]

    def _is_all_keypads_display_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 36 and payload[0:18] == [
            "F0",
            "7F",
            "00",
            "00",
            "00",
            "00",
            "70",
            "00",
            "02",
            "01",
            "01",
            "00",
            "00",
            "00",
            "01",
            "00",
            "10",
            "00",
        ]

    def _is_specific_keypad_display_frame(self, payload: list[str]) -> bool:
        return len(payload) >= 36 and payload[0] == "F0" and payload[1] != "7F" and payload[4:18] == [
            "00",
            "00",
            "70",
            "00",
            "02",
            "01",
            "01",
            "00",
            "00",
            "00",
            "01",
            "00",
            "10",
            "00",
        ]


class DummyRussoundRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(Any, self.server)
        state = cast(DummyRussoundState, server.state)
        server.register_client(self.request)
        buffer = bytearray()
        LOG.debug("client connected from %s", self.client_address)

        try:
            while True:
                try:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                except TimeoutError:
                    continue
                except OSError:
                    break

                while True:
                    try:
                        end_index = buffer.index(0xF7)
                    except ValueError:
                        break

                    raw_message = bytes(buffer[: end_index + 1])
                    del buffer[: end_index + 1]
                    payload = _to_hex_bytes(raw_message)
                    if len(payload) < 14 or payload[0] != "F0":
                        continue

                    LOG.debug("received frame from %s: %s", self.client_address, " ".join(payload))
                    request_label = state.request_label(payload)
                    if request_label is not None:
                        controller_id, zone_number = state.request_target(payload)
                        if controller_id is None or zone_number is None:
                            LOG.info("%s from %s", request_label, self.client_address)
                        else:
                            LOG.info("%s c%d z%d from %s", request_label, controller_id, zone_number, self.client_address)
                    response = state.handle_frame(payload)
                    if response is not None:
                        LOG.debug("sending response to %s: %s", self.client_address, " ".join(response))
                        self.request.sendall(_from_hex_bytes(response))
        finally:
            server.unregister_client(self.request)
            LOG.debug("client disconnected from %s", self.client_address)


class ThreadedDummyRussoundServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[socketserver.BaseRequestHandler], state: DummyRussoundState):
        self.state = state
        self._client_sockets: set[socket.socket] = set()
        self._client_sockets_lock = threading.Lock()
        super().__init__(server_address, handler_class)

    def register_client(self, client_socket: socket.socket) -> None:
        with self._client_sockets_lock:
            self._client_sockets.add(client_socket)

    def unregister_client(self, client_socket: socket.socket) -> None:
        with self._client_sockets_lock:
            self._client_sockets.discard(client_socket)

    def broadcast_zone_info(self, controller_id: int, zone_number: int) -> None:
        fields = self.state.build_zone_info_response(controller_id, zone_number)
        frame = [
            "F0",
            f"{controller_id - 1:02X}",
            "00",
            "70",
            "00",
            "00",
            "7F",
            "00",
            "00",
            *fields,
        ]
        frame[-2] = f"{(len(frame) - 2 + sum(int(value, 16) for value in frame[:-2])) & 0x7F:02X}"
        frame_bytes = _from_hex_bytes(frame)
        with self._client_sockets_lock:
            clients = list(self._client_sockets)
        for client_socket in clients:
            try:
                client_socket.sendall(frame_bytes)
            except OSError:
                self.unregister_client(client_socket)


def load_state(path: str | Path | None) -> DummyRussoundState:
    return DummyRussoundState.from_file(Path(path) if path else DEFAULT_STATE_PATH)


def save_state(state: DummyRussoundState, state_path: str | Path | None) -> None:
    if state_path is None:
        return
    state.save_to_file(Path(state_path))


def configure_logging(debug: bool, tui_enabled: bool, state_path: str | Path | None) -> TuiLogBuffer | None:
    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    if tui_enabled:
        log_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        file_handler = logging.FileHandler(log_path.with_suffix(".log"), encoding="utf-8")
        file_handler.setFormatter(formatter)
        tui_buffer = TuiLogBuffer()
        tui_handler = TuiLogHandler(tui_buffer)
        tui_handler.setFormatter(formatter)
        logging.basicConfig(level=level, handlers=[file_handler, tui_handler], force=True)
        return tui_buffer

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[stream_handler], force=True)
    return None


def run_tui(state: DummyRussoundState, state_path: str | Path | None, log_buffer: TuiLogBuffer | None) -> None:
    try:
        curses.wrapper(_run_tui, state, Path(state_path) if state_path is not None else None, log_buffer)
    except KeyboardInterrupt:
        return


def _run_tui(stdscr: curses.window, state: DummyRussoundState, state_path: Path | None, log_buffer: TuiLogBuffer | None) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(100)
    curses.use_default_colors()

    selected_zone_index = 0
    selected_field_index = 0
    focus = "zones"
    log_scroll = 0
    status_message = "Tab switches panes. Arrows navigate. +/- adjust. PgUp/PgDn scroll logs. S saves. Q quits."

    def persist(message: str | None = None) -> None:
        nonlocal status_message
        if state_path is None:
            status_message = message or "State updated in memory."
            return
        save_state(state, state_path)
        status_message = message or f"Saved {state_path.name}."

    while True:
        zones = state.zone_addresses()
        if zones:
            selected_zone_index = min(selected_zone_index, len(zones) - 1)
        else:
            selected_zone_index = 0
        selected_field_index = min(selected_field_index, len(ZONE_FIELD_DEFS) - 1)

        stdscr.erase()
        height, width = stdscr.getmaxyx()
        log_box_height = 7 if height >= 14 else max(5, height // 3)
        editor_height = max(1, height - log_box_height)
        log_top = editor_height
        left_width = min(28, max(22, width // 3))
        right_width = max(20, width - left_width - 2)

        stdscr.addnstr(0, 0, "Dummy Russound Backend TUI", width - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, status_message, width - 1)
        stdscr.addnstr(2, 0, f"Focus: {focus}", width - 1)

        stdscr.addnstr(4, 0, "Zones", left_width - 1, curses.A_UNDERLINE)
        for row, (controller_id, zone_number) in enumerate(zones[: max(0, editor_height - 7)]):
            zone_label = f"C{controller_id} Z{zone_number}"
            if row + 5 >= editor_height - 1:
                break
            attribute = curses.A_REVERSE if row == selected_zone_index and focus == "zones" else curses.A_NORMAL
            stdscr.addnstr(row + 5, 0, zone_label.ljust(left_width - 1), left_width - 1, attribute)

        if zones:
            controller_id, zone_number = zones[selected_zone_index]
            zone = state.zone(controller_id, zone_number)
            stdscr.addnstr(4, left_width + 1, f"Zone C{controller_id} Z{zone_number}", right_width - 1, curses.A_UNDERLINE)
            stdscr.addnstr(5, left_width + 1, f"Power: {'On' if zone.power else 'Off'}", right_width - 1)
            zone_keypad_display = state.keypad_display(controller_id, zone_number, 1)
            zone_display_text = zone_keypad_display.message if zone_keypad_display.message else "<none>"
            stdscr.addnstr(6, left_width + 1, f"Display (k1): {zone_display_text}", right_width - 1)
            stdscr.addnstr(
                7,
                left_width + 1,
                f"Display Align/Flash: {zone_keypad_display.alignment}/{zone_keypad_display.flash_time}",
                right_width - 1,
            )
            for row, (field_name, field_label, field_kind, minimum, maximum) in enumerate(ZONE_FIELD_DEFS):
                value = getattr(zone, field_name)
                if field_kind == "bool":
                    display_value = "Yes" if bool(value) else "No"
                else:
                    display_value = str(value)
                    if minimum is not None and maximum is not None:
                        display_value = f"{display_value} [{minimum}..{maximum}]"
                attribute = curses.A_REVERSE if row == selected_field_index and focus == "fields" else curses.A_NORMAL
                line = f"{field_label:<18} {display_value}"
                field_row = 8 + row
                if field_row >= editor_height - 1:
                    break
                stdscr.addnstr(field_row, left_width + 1, line.ljust(right_width - 1), right_width - 1, attribute)

        if log_buffer is not None and editor_height < height:
            stdscr.hline(log_top, 0, curses.ACS_HLINE, width)
            stdscr.addnstr(log_top, 2, " Logs ", width - 4, curses.A_BOLD)
            log_lines = log_buffer.snapshot()
            log_inner_height = max(0, height - log_top - 1)
            visible_lines = max(0, log_inner_height - 1)
            max_scroll = max(0, len(log_lines) - visible_lines)
            log_scroll = min(log_scroll, max_scroll)
            start_index = max(0, len(log_lines) - visible_lines - log_scroll)
            visible_log_lines = log_lines[start_index : start_index + visible_lines]
            for row in range(visible_lines):
                line = visible_log_lines[row] if row < len(visible_log_lines) else ""
                stdscr.addnstr(log_top + 1 + row, 0, line.ljust(width - 1), width - 1)

        stdscr.refresh()
        key = stdscr.getch()

        if key == -1:
            continue

        if key in (ord("q"), ord("Q")):
            if state_path is not None:
                save_state(state, state_path)
            break
        if key == ord("s") or key == ord("S"):
            persist()
            continue
        if key == 9:  # Tab
            focus = "fields" if focus == "zones" else "zones"
            continue
        if key == curses.KEY_RESIZE:
            continue
        if key == curses.KEY_PPAGE and log_buffer is not None:
            log_scroll = min(len(log_buffer.snapshot()), log_scroll + max(1, log_box_height - 2))
            continue
        if key == curses.KEY_NPAGE and log_buffer is not None:
            log_scroll = max(0, log_scroll - max(1, log_box_height - 2))
            continue
        if not zones:
            continue

        if focus == "zones":
            if key in (curses.KEY_UP, ord("k")):
                selected_zone_index = max(0, selected_zone_index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected_zone_index = min(len(zones) - 1, selected_zone_index + 1)
            elif key in (curses.KEY_RIGHT, ord("l")):
                focus = "fields"
            elif key in (curses.KEY_ENTER, 10, 13):
                focus = "fields"
            continue

        controller_id, zone_number = zones[selected_zone_index]
        zone = state.zone(controller_id, zone_number)
        field_name, _, field_kind, _, _ = ZONE_FIELD_DEFS[selected_field_index]

        if key in (curses.KEY_UP, ord("k")):
            selected_field_index = max(0, selected_field_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected_field_index = min(len(ZONE_FIELD_DEFS) - 1, selected_field_index + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            focus = "zones"
        elif key in (curses.KEY_RIGHT, ord("l"), ord("+"), ord("="), ord("-"), ord("_"), ord(" ")):
            delta = 1 if key in (curses.KEY_RIGHT, ord("l"), ord("+"), ord("="), ord(" ")) else -1
            if field_kind == "bool" and key == ord(" "):
                delta = 1
            zone.adjust_field(field_name, delta)
            if field_name in {"power", "volume"}:
                state.notify_zone_update(controller_id, zone_number)
            persist(f"Updated C{controller_id} Z{zone_number} {field_name}.")



def main() -> None:
    parser = argparse.ArgumentParser(description="Run a TCP dummy Russound backend for local testing")
    parser.add_argument("--host", default="127.0.0.1", help="TCP host to bind")
    parser.add_argument("--port", type=int, default=6666, help="TCP port to bind")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Path to the initial JSON state file")
    parser.add_argument("--tui", action="store_true", help="Open an interactive curses TUI to edit zone values")
    parser.add_argument("--serve", action="store_true", help="Run the TCP dummy backend server alongside the TUI")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    log_buffer = configure_logging(args.debug, args.tui, args.state)

    state = load_state(args.state)

    if args.tui and args.serve:
        with ThreadedDummyRussoundServer((args.host, args.port), DummyRussoundRequestHandler, state) as server:
            state.set_zone_update_callback(server.broadcast_zone_info)
            server_thread: threading.Thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            LOG.info("Dummy Russound backend listening on %s:%d", args.host, args.port)
            try:
                run_tui(state, args.state, log_buffer)
            except KeyboardInterrupt:
                pass
            finally:
                server.shutdown()
                server.server_close()
        return

    if args.tui:
        run_tui(state, args.state, log_buffer)
        return

    with ThreadedDummyRussoundServer((args.host, args.port), DummyRussoundRequestHandler, state) as server:
        LOG.info("Dummy Russound backend listening on %s:%d", args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOG.info("Shutting down dummy backend")


if __name__ == "__main__":
    main()