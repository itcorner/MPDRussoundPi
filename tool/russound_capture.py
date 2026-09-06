"""Capture and decode Russound RNET traffic from a serial device or hex input."""

from __future__ import annotations

import argparse
import curses
import json
import logging
import os
import select
import socket
import sys
import termios
import time
import textwrap
from collections.abc import Iterable
from collections import deque
from pathlib import Path
from typing import Any, Callable, Protocol, cast

from tool.dummy_backend.dummy_backend import DummyRussoundState
from web.russound_connector import Russound


DEFAULT_DEVICE = "/dev/cu.PL2303G-USBtoUART1120"
DEFAULT_BAUD = 19200
DEFAULT_PORT = 6666
DEFAULT_LOG_FILE = "russound_capture.log"
CONNECT_TIMEOUT_SECONDS = 5.0
KEYPAD_EVENTS = {
    0x64: "setup",
    0x67: "previous",
    0x68: "next",
    0x69: "plus",
    0x6A: "minus",
    0x6B: "source_toggle",
    0x6C: "power",
    0x6D: "stop",
    0x6E: "pause",
    0x6F: "favorite_1",
    0x70: "favorite_2",
    0x73: "play",
    0x7F: "volume_up",
    0x80: "volume_down",
}


class RussoundTrafficDecoder:
    """Apply the live connector and dummy backend's protocol recognition rules."""

    def __init__(self) -> None:
        self.connector = Russound("127.0.0.1", 6666)
        self.dummy_state = DummyRussoundState()

    def decode(self, frame: bytes) -> dict[str, Any]:
        payload = [f"{byte:02X}" for byte in frame]
        semantic: dict[str, Any] = {}
        decoded_frame = _unescape_rnet_frame(frame)
        if len(decoded_frame) >= 7:
            semantic["address"] = self._decode_device_addresses(decoded_frame)
        request_label = self.dummy_state.request_label(payload)
        if request_label is not None:
            semantic["message"] = request_label
            controller, zone = self.dummy_state.request_target(payload)
            if controller is not None and zone is not None:
                semantic["target"] = {"controller": controller, "zone": zone}

        parse_updates = getattr(self.connector, "_parse_zone_updates")
        updates = parse_updates(bytearray(frame))
        if updates:
            semantic["zone_updates"] = updates

        handshake = self._decode_handshake(decoded_frame)
        if handshake is not None:
            semantic["handshake"] = handshake

        event = self._decode_event(frame)
        if event is not None:
            semantic["event"] = event

        response = self._decode_response(frame)
        if response is not None:
            semantic["response"] = response

        request = self._decode_request_data(frame)
        if request is not None:
            semantic["request"] = request

        display = self._decode_display_feedback(frame)
        if display is not None:
            semantic["display"] = display

        set_data = self._decode_set_data(frame)
        if set_data is not None:
            semantic["set_data"] = set_data

        if not semantic:
            semantic["message"] = "unknown RNET frame"

        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "raw": " ".join(payload),
            "length": len(frame),
            "checksum": self._checksum_status(frame),
            "semantic": semantic,
        }

    @staticmethod
    def _decode_handshake(frame: bytes) -> dict[str, Any] | None:
        if len(frame) != 11 or frame[7] != 0x02 or frame[8] != 0x06:
            return None
        return {
            "message": "event handshake",
            "kind": frame[8],
            "target": {
                "controller": frame[1],
                "zone": frame[2],
                "keypad": frame[3],
            },
        }

    def _decode_set_data(self, frame: bytes) -> dict[str, Any] | None:
        decoded = self._unescape_body(frame)
        if len(decoded) < 10 or decoded[7] != 0x00:
            return None

        paths = _split_rnet_paths(decoded[8:-2])
        if paths is None:
            return None
        target_path, source_path, body = paths
        if target_path[:2] == bytes((0x04, 0x04)):
            return {
                "message": "source object update",
                "path": [*target_path],
                "source_path": [*source_path],
                "leaf": target_path[2] if len(target_path) > 2 else None,
                "value": body[-1] if body else None,
                "idle": bool(body) and body[-1] == 0x48,
            }
        if target_path == bytes((0x04, 0x02, 0x00)) and body:
            return {
                "message": "zone info update",
                "path": [*target_path],
                "source_path": [*source_path],
                "body": [*body],
            }

        return {
            "message": "controller set-data message",
            "description": "undocumented controller/device data body",
            "target": {
                "controller": decoded[1],
                "zone": decoded[2],
                "keypad": decoded[3],
            },
            "source": {
                "controller": decoded[4],
                "zone": decoded[5],
                "keypad": decoded[6],
            },
            "target_path": [*target_path],
            "source_path": [*source_path],
            "body": " ".join(f"{byte:02X}" for byte in body),
        }

    def _decode_request_data(self, frame: bytes) -> dict[str, Any] | None:
        decoded = self._unescape_body(frame)
        if len(decoded) < 10 or decoded[7] != 0x01:
            return None

        paths = _split_rnet_paths(decoded[8:-2])
        if paths is None:
            return None
        target_path, source_path, body = paths
        body_text = " ".join(f"{byte:02X}" for byte in body)
        if decoded[8] == 0x05 and target_path[:2] == bytes((0x02, 0x00)):
            body_text = " ".join(f"{byte:02X}" for byte in decoded[8:-2])

        return {
            "message": "request-data message",
            "description": "request for parameter/path data",
            "target": {
                "controller": decoded[1],
                "zone": decoded[2],
                "keypad": decoded[3],
            },
            "source": {
                "controller": decoded[4],
                "zone": decoded[5],
                "keypad": decoded[6],
            },
            "target_path": [*target_path],
            "source_path": [*source_path],
            "path": [*target_path],
            "body": body_text,
        }

    @staticmethod
    def _unescape_body(frame: bytes) -> bytes:
        return _unescape_rnet_frame(frame)

    @staticmethod
    def _decode_device_addresses(frame: bytes) -> dict[str, Any]:
        def controller(value: int) -> dict[str, Any]:
            if value == 0x7F:
                return {"raw": "7F", "meaning": "all controllers"}
            return {"raw": f"{value:02X}", "number": value + 1, "meaning": f"controller {value + 1}"}

        def zone(value: int) -> dict[str, Any]:
            special = {
                0x7F: "reserved zone",
                0x7E: "controller link",
                0x7D: "peripheral device",
                0x7C: "trace",
            }
            if value in special:
                return {"raw": f"{value:02X}", "meaning": special[value]}
            return {"raw": f"{value:02X}", "number": value + 1, "meaning": f"zone {value + 1}"}

        def keypad(value: int) -> dict[str, Any]:
            special = {
                0x7F: "controller itself / all devices",
                0x7E: "system class",
                0x7D: "source/tuner class",
                0x70: "third-party / fire-and-forget source",
                0x60: "handshake keypad class",
                0x7C: "keypad ID request",
                0x79: "source broadcast display",
            }
            if value in special:
                return {"raw": f"{value:02X}", "meaning": special[value]}
            return {"raw": f"{value:02X}", "number": value + 1, "meaning": f"keypad {value + 1}"}

        return {
            "target": {
                "controller": controller(frame[1]),
                "zone": zone(frame[2]),
                "keypad": keypad(frame[3]),
            },
            "source": {
                "controller": controller(frame[4]),
                "zone": zone(frame[5]),
                "keypad": keypad(frame[6]),
            },
        }

    def _decode_display_feedback(self, frame: bytes) -> dict[str, Any] | None:
        decoded = self._unescape_body(frame)
        if len(decoded) < 12 or decoded[7] != 0x00:
            return None
        paths = _split_rnet_paths(decoded[8:-2])
        if paths is None:
            return None
        target_path, source_path, body = paths
        if target_path != bytes((0x01, 0x01)):
            return None

        if source_path == bytes((0x01, 0x01)) and len(decoded) >= 24:
            payload_length = decoded[18]
            flash_low = decoded[21]
            text_bytes = decoded[22:-2]
            flash_high = text_bytes[0] if text_bytes else 0
            text_bytes = text_bytes[1:]
        else:
            if len(body) < 3:
                return None
            payload_length = len(body)
            flash_low = body[1]
            flash_high = body[2]
            text_bytes = body[3:]

        text = bytes(text_bytes).split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return {
            "message": "direct display feedback",
            "target": {"controller": frame[1], "zone": frame[2], "keypad": frame[3]},
            "source": {"controller": frame[4], "zone": frame[5], "keypad": frame[6]},
            "payload_length": payload_length,
            "flash_time_10ms": flash_low | (flash_high << 8),
            "text": text,
        }

    def _decode_event(self, frame: bytes) -> dict[str, Any] | None:
        decoded = self._unescape_body(frame)
        if len(decoded) < 10 or decoded[7] != 0x05:
            return None
        paths = _split_rnet_paths(decoded[8:-2])
        if paths is None:
            return None
        target_path, source_path, body = paths
        if not body:
            return None
        event_id = body[0]
        event_data = body[4] | (body[5] << 8) if len(body) >= 7 else 0
        priority = body[6] if len(body) >= 7 else body[-1]
        event_name = KEYPAD_EVENTS.get(event_id, f"event_0x{event_id:02X}")
        if event_id == 0xDC:
            event_name = "set_state"
        elif event_id == 0xC8:
            event_name = "zone_activity"
        elif event_id in {0xBF, 0xC0}:
            event_name = "source_control"
        event: dict[str, Any] = {
            "name": event_name,
            "id": f"{event_id:02X}",
            "target_path": [*target_path],
            "source_path": [*source_path],
            "target": {"controller": frame[1] + 1, "zone": frame[2] + 1},
            "data": event_data,
            "priority": priority,
        }
        if event_id == 0xC8 and len(body) >= 6:
            zone_mask = body[4] | (body[5] << 8)
            event["zone_mask"] = zone_mask
            event["zones_on"] = [zone + 1 for zone in range(6) if zone_mask & (1 << zone)]
        return event

    def _decode_response(self, frame: bytes) -> dict[str, Any] | None:
        decoded_frame = self._unescape_body(frame)
        if len(decoded_frame) < 9 or decoded_frame[7] != 0x00:
            return None
        paths = _split_rnet_paths(decoded_frame[8:-2])
        if paths is None:
            return None
        target_path, source_path, body = paths
        path = source_path if not target_path else target_path
        if len(path) >= 4 and path[:2] == bytes((0x02, 0x00)) and path[3] == 0x07 and len(body) >= 11:
            return {
                "message": "zone info response",
                "path": [*path],
                "target": {"controller": decoded_frame[1] + 1, "zone": path[2] + 1},
                "zone": {
                    "power": bool(body[0]),
                    "source_index": body[1],
                    "volume": body[2] * 2,
                    "bass": body[3] - 10,
                    "treble": body[4] - 10,
                    "loudness": bool(body[5]),
                    "balance": body[6] - 10,
                    "system_power": bool(body[7]),
                    "shared_source": bool(body[8]),
                    "party": bool(body[9]),
                    "do_not_disturb": bool(body[10]),
                },
            }

        if len(path) >= 4 and path[:2] == bytes((0x02, 0x00)) and len(body) >= 1:
            parameter_id = path[3]
            value = body[-1]
            parameter_names = {
                0: "bass",
                1: "treble",
                2: "loudness",
                3: "balance",
                4: "turn_on_volume",
            }
            name = parameter_names.get(
                parameter_id,
                f"parameter_{parameter_id:02X}",
            )
            if name in {"bass", "treble", "balance"}:
                value -= 10
            elif name == "turn_on_volume":
                value *= 2
            elif name == "loudness":
                value = bool(value)
            return {
                "message": "zone user parameter response",
                "path": [*path],
                "target": {"controller": decoded_frame[1] + 1, "zone": path[2] + 1},
                "parameter": name,
                "value": value,
            }

        return None

    @staticmethod
    def _checksum_status(frame: bytes) -> str | dict[str, Any]:
        if len(frame) < 3 or frame[0] != 0xF0 or frame[-1] != 0xF7:
            return {"valid": False, "reason": "missing RNET frame boundaries"}
        expected = (len(frame) - 2 + sum(frame[:-2])) & 0x7F
        if expected == frame[-2]:
            return "ok"
        return {"valid": False, "expected": f"{expected:02X}", "actual": f"{frame[-2]:02X}"}


def extract_frames(chunks: Iterable[bytes]) -> Iterable[bytes]:
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        while 0xF7 in buffer:
            end = buffer.index(0xF7)
            candidate = bytes(buffer[: end + 1])
            del buffer[: end + 1]
            start = candidate.find(b"\xF0")
            if start >= 0:
                yield candidate[start:]


def parse_hex_input(lines: Iterable[str]) -> Iterable[bytes]:
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            yield bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError(f"invalid hex input: {text}") from exc


def _split_rnet_paths(payload: bytes) -> tuple[bytes, bytes, bytes] | None:
    """Split decoded post-type bytes into target path, source path, and body."""
    if not payload:
        return None
    target_length = payload[0]
    target_end = 1 + target_length
    if target_end >= len(payload):
        return None
    source_length = payload[target_end]
    source_start = target_end + 1
    source_end = source_start + source_length
    if source_end > len(payload):
        return None
    return payload[1:target_end], payload[source_start:source_end], payload[source_end:]


def configure_serial(fd: int, baud: int) -> None:
    settings = termios.tcgetattr(fd)
    settings[0] = 0
    settings[1] = 0
    settings[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    settings[3] = 0
    baud_constant = getattr(termios, f"B{baud}")
    settings[4] = baud_constant
    settings[5] = baud_constant
    settings[6][termios.VMIN] = 0
    settings[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, settings)


def build_rnet_frame(payload: list[int]) -> bytes:
    checksum = (len(payload) + sum(payload)) & 0x7F
    return bytes([*payload, checksum, 0xF7])


def build_volume_event_frame(controller: int, zone: int, event_id: int) -> bytes:
    if not 1 <= controller <= 6:
        raise ValueError("controller must be between 1 and 6")
    if not 1 <= zone <= 6:
        raise ValueError("zone must be between 1 and 6")
    if event_id not in {0x7F, 0x80}:
        raise ValueError("volume event must be 0x7F (up) or 0x80 (down)")

    encoded_event = [event_id]
    if event_id > 0x7F:
        encoded_event = [0xF1, event_id ^ 0xFF]
    payload = [
        0xF0,
        controller - 1,
        0x00,
        0x7F,
        0x00,
        zone - 1,
        0x70,
        0x05,
        0x02,
        0x02,
        0x00,
        0x00,
        *encoded_event,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x01,
    ]
    return build_rnet_frame(payload)


def build_set_volume_frame(controller: int, zone: int, volume: int = 42) -> bytes:
    if not 1 <= controller <= 6:
        raise ValueError("controller must be between 1 and 6")
    if not 1 <= zone <= 6:
        raise ValueError("zone must be between 1 and 6")
    if not 0 <= volume <= 100 or volume % 2:
        raise ValueError("volume must be an even value between 0 and 100")

    payload = [
        0xF0,
        controller - 1,
        0x00,
        0x7F,
        0x00,
        0x00,
        0x70,
        0x05,
        0x02,
        0x02,
        0x00,
        0x00,
        0xF1,
        0x21,
        0x00,
        volume // 2,
        0x00,
        zone - 1,
        0x00,
        0x01,
    ]
    return build_rnet_frame(payload)


def build_get_zone_info_frame(controller: int, zone: int) -> bytes:
    if not 1 <= controller <= 6:
        raise ValueError("controller must be between 1 and 6")
    if not 1 <= zone <= 6:
        raise ValueError("zone must be between 1 and 6")

    payload = [
        0xF0,
        controller - 1,
        0x00,
        0x7F,
        0x00,
        0x00,
        0x70,
        0x01,
        0x04,
        0x02,
        0x00,
        zone - 1,
        0x07,
        0x00,
        0x00,
    ]
    return build_rnet_frame(payload)


def build_mute_frame(controller: int, zone: int) -> bytes:
    if not 1 <= controller <= 6:
        raise ValueError("controller must be between 1 and 6")
    if not 1 <= zone <= 6:
        raise ValueError("zone must be between 1 and 6")

    payload = [
        0xF0,
        controller - 1,
        0x00,
        0x7F,
        0x00,
        zone - 1,
        0x70,
        0x05,
        0x02,
        0x02,
        0x00,
        0x00,
        0xF1,
        0x40,
        0x00,
        0x00,
        0x00,
        0x0D,
        0x00,
        0x01,
    ]
    return build_rnet_frame(payload)


def build_power_frame(controller: int, zone: int, power: bool) -> bytes:
    if not 1 <= controller <= 6:
        raise ValueError("controller must be between 1 and 6")
    if not 1 <= zone <= 6:
        raise ValueError("zone must be between 1 and 6")

    payload = [
        0xF0,
        controller - 1,
        0x00,
        0x7F,
        0x00,
        0x00,
        0x70,
        0x05,
        0x02,
        0x02,
        0x00,
        0x00,
        0xF1,
        0x23,
        0x00,
        int(power),
        0x00,
        zone - 1,
        0x00,
        0x01,
    ]
    return build_rnet_frame(payload)


class Transport(Protocol):
    """A byte stream to a Russound gateway, either a local serial port or a ser2net TCP host."""

    def fileno(self) -> int: ...

    def read_available(self) -> bytes: ...

    def write(self, frame: bytes) -> None: ...

    def close(self) -> None: ...


class SerialTransport:
    def __init__(self, device: str, baud: int) -> None:
        self._fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        configure_serial(self._fd, baud)

    def fileno(self) -> int:
        return self._fd

    def read_available(self) -> bytes:
        try:
            return os.read(self._fd, 4096)
        except BlockingIOError:
            return b""

    def write(self, frame: bytes) -> None:
        view = memoryview(frame)
        while view:
            view = view[os.write(self._fd, view) :]

    def close(self) -> None:
        os.close(self._fd)


class TcpTransport:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
        self._sock.setblocking(False)

    def fileno(self) -> int:
        return self._sock.fileno()

    def read_available(self) -> bytes:
        try:
            return self._sock.recv(4096)
        except BlockingIOError:
            return b""

    def write(self, frame: bytes) -> None:
        self._sock.setblocking(True)
        try:
            self._sock.sendall(frame)
        finally:
            self._sock.setblocking(False)

    def close(self) -> None:
        self._sock.close()


class ConnectionTarget:
    """Describes how to reach the Russound gateway, without opening it yet."""

    def describe(self) -> str:
        raise NotImplementedError

    def connect(self) -> Transport:
        raise NotImplementedError


class SerialTarget(ConnectionTarget):
    def __init__(self, device: str, baud: int) -> None:
        self.device = device
        self.baud = baud

    def describe(self) -> str:
        return f"{self.device} {self.baud} baud"

    def connect(self) -> Transport:
        return SerialTransport(self.device, self.baud)


class TcpTarget(ConnectionTarget):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def describe(self) -> str:
        return f"{self.host}:{self.port} (ser2net)"

    def connect(self) -> Transport:
        return TcpTransport(self.host, self.port)


def resolve_target(args: argparse.Namespace) -> ConnectionTarget:
    if args.host:
        return TcpTarget(args.host, args.port)
    return SerialTarget(args.device, args.baud)


def configure_capture_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("russound_capture")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def log_record(logger: logging.Logger, direction: str, record: dict[str, Any]) -> None:
    logger.debug("%s raw=%s checksum=%s", direction, record["raw"], record["checksum"])
    frame = bytes.fromhex(record["raw"])
    decoded_frame = _unescape_rnet_frame(frame)
    message_type = {
        0x00: "set data",
        0x01: "request data",
        0x02: "handshake",
        0x05: "event",
    }.get(decoded_frame[7] if len(decoded_frame) > 7 else -1, f"0x{decoded_frame[7]:02X}" if len(decoded_frame) > 7 else "unknown")

    address = record["semantic"].get("address", {})
    target = _format_device_address(address.get("target", {}))
    source = _format_device_address(address.get("source", {}))
    logger.info("%s header target=%s source=%s type=%s", direction, target, source, message_type)

    logger.info("%s content=%s", direction, summarize_semantic(record["semantic"]))


def _format_device_address(address: dict[str, Any]) -> str:
    def field(name: str) -> str:
        raw_value = address.get(name, {})
        if not isinstance(raw_value, dict):
            return f"{name}={raw_value}"
        value = cast(dict[str, Any], raw_value)
        meaning = value.get("meaning", "unknown")
        raw = value.get("raw", "??")
        return f"{name}={meaning}[{raw}]"

    return f"{field('controller')} {field('zone')} {field('keypad')}"


def _format_compact_target(target: dict[str, Any]) -> str:
    return f"c{target.get('controller', '?')}z{target.get('zone', '?')}"


def _format_compact_fields(values: dict[str, Any], skip: tuple[str, ...] = ("message", "target", "source", "description")) -> str:
    parts: list[str] = []
    for key, value in values.items():
        if key in skip:
            continue
        if isinstance(value, dict):
            nested = cast(dict[str, Any], value)
            parts.append(" ".join(f"{k}={v}" for k, v in nested.items()))
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def summarize_semantic(semantic: dict[str, Any]) -> str:
    """Render one short, human-readable line for a decoded frame's semantic content."""
    parts: list[str] = []

    if "event" in semantic:
        event = semantic["event"]
        parts.append(f"event={event['name']}(0x{event['id']}) {_format_compact_target(event['target'])} data={event['data']} priority={event['priority']}")
    elif "handshake" in semantic:
        handshake = semantic["handshake"]
        parts.append(f"{handshake['message']} kind=0x{handshake['kind']:02X}")
    elif "message" in semantic:
        label = semantic["message"]
        target_text = f" {_format_compact_target(semantic['target'])}" if "target" in semantic else ""
        parts.append(f"{label}{target_text}")

    if "zone_updates" in semantic:
        updates = semantic["zone_updates"]
        parts.append("; ".join(f"{_format_compact_target(update)} {update['setting']}={update['value']}" for update in updates))

    for key in ("response", "request", "set_data", "display"):
        if key not in semantic:
            continue
        payload = semantic[key]
        label = payload.get("message", key)
        details = _format_compact_fields(payload)
        parts.append(f"{label} {details}".rstrip())

    if not parts:
        return "unknown"
    return " | ".join(parts)


def _unescape_rnet_frame(frame: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(frame):
        if frame[index] == 0xF1 and index + 1 < len(frame):
            decoded.append(frame[index + 1] ^ 0xFF)
            index += 2
            continue
        decoded.append(frame[index])
        index += 1
    return bytes(decoded)

def run_tui(target: ConnectionTarget, log_file: str) -> int:
    actions: tuple[tuple[str, Callable[[int], bytes]], ...] = (
        ("Get zone information", lambda zone: build_get_zone_info_frame(1, zone)),
        ("Turn zone on", lambda zone: build_power_frame(1, zone, True)),
        ("Turn zone off", lambda zone: build_power_frame(1, zone, False)),
        ("Volume up", lambda zone: build_volume_event_frame(1, zone, 0x7F)),
        ("Volume down", lambda zone: build_volume_event_frame(1, zone, 0x80)),
        ("Set volume to 42", lambda zone: build_set_volume_frame(1, zone, 42)),
        ("Mute toggle", lambda zone: build_mute_frame(1, zone)),
    )

    def draw(stdscr: Any, records: deque[tuple[str, dict[str, Any]]], status: str, scroll: int, zone: int, action: int) -> int:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f"Russound RNET  {target.describe()}  log={log_file}", width - 1, curses.A_BOLD)
        zones = "  ".join(f"[{item}]" if item == zone else str(item) for item in (1, 2, 3))
        stdscr.addnstr(1, 0, f"Zone {zones}   Action: {actions[action][0]}", width - 1)
        stdscr.addnstr(2, 0, "Left/Right zone | Up/Down action | Enter send | j/k/PgUp/PgDn/g/G scroll | q quit", width - 1)
        log_height = max(1, height - 6)
        log_lines: list[str] = []
        for direction, record in records:
            for prefix, text in ((f"{direction} ", f"{record['raw']} checksum={record['checksum']}"), ("  ", summarize_semantic(record["semantic"]))):
                wrapped = textwrap.wrap(text, width=max(1, width - len(prefix))) or [""]
                log_lines.extend(prefix + part if index == 0 else " " * len(prefix) + part for index, part in enumerate(wrapped))
        max_scroll = max(0, len(log_lines) - log_height)
        scroll = min(max(scroll, 0), max_scroll)
        for line, text in enumerate(log_lines[scroll : scroll + log_height], start=4):
            stdscr.addnstr(line, 0, text, width - 1)
        footer = f"{status} | lines {scroll + 1 if log_lines else 0}-{min(scroll + log_height, len(log_lines))}/{len(log_lines)}"
        stdscr.addnstr(height - 1, 0, footer, width - 1, curses.A_DIM)
        stdscr.refresh()
        return max_scroll

    def app(stdscr: Any) -> int:
        transport = target.connect()
        records: deque[tuple[str, dict[str, Any]]] = deque(maxlen=40)
        decoder = RussoundTrafficDecoder()
        logger = configure_capture_logger(log_file)
        rx_buffer = bytearray()
        status = "connected"
        scroll = 0
        last_max_scroll = 0
        follow_bottom = True
        selected_zone = 1
        selected_action = 0
        stdscr.nodelay(True)
        stdscr.keypad(True)
        try:
            while True:
                key = stdscr.getch()
                if key in (ord("q"), ord("Q")):
                    return 0
                if key == curses.KEY_LEFT:
                    selected_zone = max(1, selected_zone - 1)
                elif key == curses.KEY_RIGHT:
                    selected_zone = min(3, selected_zone + 1)
                elif key == curses.KEY_UP:
                    selected_action = (selected_action - 1) % len(actions)
                elif key == curses.KEY_DOWN:
                    selected_action = (selected_action + 1) % len(actions)
                elif key in (curses.KEY_PPAGE, ord("u"), ord("U")):
                    if follow_bottom:
                        scroll = last_max_scroll
                    scroll = max(0, scroll - max(1, stdscr.getmaxyx()[0] - 8))
                    follow_bottom = False
                elif key in (curses.KEY_NPAGE, ord("d"), ord("D")):
                    if follow_bottom:
                        scroll = last_max_scroll
                    scroll += max(1, stdscr.getmaxyx()[0] - 8)
                    follow_bottom = scroll >= last_max_scroll
                elif key in (ord("k"), ord("K")):
                    if follow_bottom:
                        scroll = last_max_scroll
                    scroll = max(0, scroll - 1)
                    follow_bottom = False
                elif key in (ord("j"), ord("J")):
                    if follow_bottom:
                        scroll = last_max_scroll
                    scroll += 1
                    follow_bottom = scroll >= last_max_scroll
                elif key in (curses.KEY_HOME, ord("g")):
                    scroll = 0
                    follow_bottom = False
                elif key in (curses.KEY_END, ord("G")):
                    follow_bottom = True
                if key in (curses.KEY_ENTER, 10, 13):
                    frame = actions[selected_action][1](selected_zone)
                    transport.write(frame)
                    record = decoder.decode(frame)
                    records.append(("TX", record))
                    log_record(logger, "TX", record)
                    status = f"sent {actions[selected_action][0].lower()} zone {selected_zone}"

                readable, _, _ = select.select([transport], [], [], 0.1)
                if readable:
                    rx_buffer.extend(transport.read_available())
                    while 0xF7 in rx_buffer:
                        end = rx_buffer.index(0xF7)
                        candidate = bytes(rx_buffer[: end + 1])
                        del rx_buffer[: end + 1]
                        start = candidate.find(b"\xF0")
                        if start < 0:
                            continue
                        record = decoder.decode(candidate[start:])
                        records.append(("RX", record))
                        log_record(logger, "RX", record)
                        status = "receiving"
                render_scroll = 10**9 if follow_bottom else scroll
                last_max_scroll = draw(stdscr, records, status, render_scroll, selected_zone, selected_action)
        finally:
            transport.close()

    try:
        return int(curses.wrapper(app))
    except OSError as exc:
        print(f"russound-capture: {exc}", file=sys.stderr)
        return 1


def capture_stream(target: ConnectionTarget) -> Iterable[bytes]:
    transport = target.connect()
    try:
        while True:
            readable, _, _ = select.select([transport], [], [], 1.0)
            if readable:
                chunk = transport.read_available()
                if chunk:
                    yield chunk
    finally:
        transport.close()


def format_record(record: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(record, sort_keys=True)
    return f"{record['timestamp']}  {record['raw']}  checksum={record['checksum']}  {summarize_semantic(record['semantic'])}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="file of whitespace-separated hex frames; use '-' for stdin")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="serial device to capture or monitor (ignored if --host is set)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="serial baud rate (default: 19200)")
    parser.add_argument("--host", help="ser2net host to connect to over TCP instead of a local serial device")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="ser2net TCP port (default: 6666)")
    parser.add_argument("--tui", action="store_true", help="open the interactive serial monitor")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="TUI log file (default: russound_capture.log)")
    parser.add_argument("--format", choices=("text", "json"), default="text", dest="output_format")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    decoder = RussoundTrafficDecoder()
    try:
        if args.tui:
            return run_tui(resolve_target(args), args.log_file)

        if args.input is None:
            for frame in extract_frames(capture_stream(resolve_target(args))):
                print(format_record(decoder.decode(frame), args.output_format), flush=True)
        elif str(args.input) == "-":
            for frame in extract_frames(parse_hex_input(sys.stdin)):
                print(format_record(decoder.decode(frame), args.output_format), flush=True)
        else:
            with args.input.open("r", encoding="utf-8") as handle:
                for frame in extract_frames(parse_hex_input(handle)):
                    print(format_record(decoder.decode(frame), args.output_format), flush=True)
    except (OSError, ValueError) as exc:
        print(f"russound-capture: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())