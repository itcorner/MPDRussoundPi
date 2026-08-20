from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
import socket
import threading
import time
from types import TracebackType
from typing import Any, Callable, Iterable


LOGGER = logging.getLogger(__name__)
# Recommended command spacing when using source keypad id 0x70.
COMMAND_DELAY = 0.1
# Bounds the initial TCP connect so an unreachable gateway cannot stall startup.
CONNECT_TIMEOUT_SECONDS = 5.0
KEYPAD_CODE = "70"
ZONE_INFO_REQUEST_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 01 04 02 00 @zz 07 00 00"
ZONE_INFO_RESPONSE_SIGNATURE = "04 02 00 @zz 07"
ZONE_USER_PARAMETER_REQUEST_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 01 05 02 00 @zz 00 @pp 00 00"
ZONE_USER_PARAMETER_RESPONSE_SIGNATURE = "05 02 00 @zz 00 @pp"
ZONE_USER_PARAMETER_SET_TEMPLATE = "F0 @cc 00 7F 00 00 @kk 00 05 02 00 @zz 00 @pp 00 00 00 01 00 01 00 @pr"
ZONE_USER_PARAMETER_PATHS = {
    "bass": 0x00,
    "treble": 0x01,
    "loudness": 0x02,
    "balance": 0x03,
    "turn_on_volume": 0x04,
}
ALL_KEYPADS_DISPLAY_TEMPLATE = "F0 7F 00 00 00 00 70 00 02 01 01 00 00 00 01 00 10 00"
SPECIFIC_KEYPAD_DISPLAY_TEMPLATE = "F0 @cc @zz @tk 00 00 70 00 02 01 01 00 00 00 01 00 10 00"
DISPLAY_TEXT_LENGTH = 13


class Russound:
    """Project-local Russound RNET connector over TCP.

    This class intentionally mirrors the third-party API surface used by the
    backend, including private helper names, so existing call sites can remain
    unchanged.
    """

    _sem_comm = 0

    def __init__(self, host: str, port: int, protocol_audit_log_file: str | Path | None = None) -> None:
        self._host = host
        self._port = int(port)
        self.sock: socket.socket | None = None
        self._last_send = time.time()
        self.lock = threading.Lock()
        self._rx_buffer = bytearray()
        self._pending_frames: deque[bytearray] = deque()
        self._pending_zone_updates: deque[dict[str, Any]] = deque()
        self._update_callback: Callable[[dict[str, Any]], None] | None = None
        self._update_listener_stop = threading.Event()
        self._update_listener_thread: threading.Thread | None = None

        self._protocol_audit_log_file: Path | None = None
        if protocol_audit_log_file:
            self._protocol_audit_log_file = Path(protocol_audit_log_file).expanduser().resolve()
            self._protocol_audit_log_file.parent.mkdir(parents=True, exist_ok=True)
        self._audit_rx_buffer = bytearray()

    def connect(self) -> bool:
        with self.lock:
            try:
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(CONNECT_TIMEOUT_SECONDS)
                self.sock.connect((self._host, self._port))
                self.sock.settimeout(None)
                LOGGER.info("Successfully connected to Russound on %s:%s", self._host, self._port)
                return True
            except OSError as exc:
                self.sock = None
                LOGGER.error("Error trying to connect to Russound controller: %s", exc)
                return False

    def is_connected(self) -> bool:
        try:
            return bool(self.sock and self.sock.getpeername() != "")
        except OSError:
            return False

    def disconnect(self) -> None:
        self.stop_update_listener()
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def close(self) -> None:
        self.disconnect()

    def start_update_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Start receiving unsolicited zone power and volume updates."""
        self._update_callback = callback
        if self._update_listener_thread is not None and self._update_listener_thread.is_alive():
            return
        self._update_listener_stop.clear()
        self._update_listener_thread = threading.Thread(
            target=self._update_listener_loop,
            daemon=True,
            name="russound-update-listener",
        )
        self._update_listener_thread.start()

    def stop_update_listener(self) -> None:
        self._update_listener_stop.set()
        listener = self._update_listener_thread
        if listener is not None and listener is not threading.current_thread():
            listener.join(timeout=1.0)
        self._update_listener_thread = None
        self._update_callback = None

    def drain_zone_updates(self) -> list[dict[str, Any]]:
        with self.lock:
            updates = list(self._pending_zone_updates)
            self._pending_zone_updates.clear()
        return updates

    def set_power(self, controller: int, zone: int, power: int) -> None:
        send_msg = self.__create_send_message(
            "F0 @cc 00 7F 00 00 @kk 05 02 02 00 00 F1 23 00 @pr 00 @zz 00 01",
            controller,
            zone,
            power,
        )
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()

    def set_volume(self, controller: int, zone: int, volume: int) -> None:
        send_msg = self.__create_send_message(
            "F0 @cc 00 7F 00 00 @kk 05 02 02 00 00 F1 21 00 @pr 00 @zz 00 01",
            controller,
            zone,
            volume // 2,
        )
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()

    def set_source(self, controller: int, zone: int, source: int) -> None:
        send_msg = self.__create_send_message(
            "F0 @cc 00 7F 00 @zz @kk 05 02 00 00 00 F1 3E 00 00 00 @pr 00 01",
            controller,
            zone,
            source,
        )
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()

    def all_on_off(self, power: int) -> None:
        # RE-corrected frame uses target controller 0x7E for all-zones on/off.
        send_msg = self.__create_send_message(
            "F0 7E 00 7F 00 00 @kk 05 02 02 00 00 F1 22 00 00 @pr 00 00 01",
            1,
            None,
            power,
        )
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()

    def toggle_mute(self, controller: int, zone: int) -> None:
        send_msg = self.__create_send_message(
            "F0 @cc 00 7F 00 @zz @kk 05 02 02 00 00 F1 40 00 00 00 0D 00 01",
            controller,
            zone,
        )
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()

    def get_zone_info(self, controller: int, zone: int, return_variable: int) -> int | list[int] | None:
        resp_msg_signature = self.__create_response_signature("04 02 00 @zz 07", zone)
        send_msg = self.__create_send_message("F0 @cc 00 7F 00 00 @kk 01 04 02 00 @zz 07 00 00", controller, zone)
        with self.lock:
            self.__send_data(send_msg)
            matching_message = self.__get_response_message(resp_msg_signature)
            if matching_message is None:
                LOGGER.warning(
                    "Did not receive expected Russound zone state for controller %s zone %s", controller, zone
                )
                return None
            if return_variable == 4:
                return [matching_message[11], matching_message[12], matching_message[13]]
            return matching_message[return_variable + 11]

    def get_power(self, controller: int, zone: int) -> int | None:
        value = self.get_zone_info(controller, zone, 0)
        return value if isinstance(value, int) else None

    def get_source(self, controller: int, zone: int) -> int | None:
        value = self.get_zone_info(controller, zone, 1)
        return value if isinstance(value, int) else None

    def get_volume(self, controller: int, zone: int) -> int | None:
        volume_level = self.get_zone_info(controller, zone, 2)
        if isinstance(volume_level, int):
            return volume_level * 2
        return None

    def get_zone_extended_info(self, controller: int, zone: int) -> dict[str, Any] | None:
        response_signature = self.__create_response_signature(ZONE_INFO_RESPONSE_SIGNATURE, zone)
        send_msg = self.__create_send_message(ZONE_INFO_REQUEST_TEMPLATE, controller, zone)
        with self.lock:
            self.__send_data(send_msg)
            message = self.__get_response_message(response_signature)
        if message is None or len(message) < 22:
            return None

        zone_info: dict[str, Any] = {
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
        turn_on_volume = self.get_zone_user_parameter(controller, zone, "turn_on_volume")
        if turn_on_volume is not None:
            zone_info["turn_on_volume"] = turn_on_volume
        return zone_info

    def get_zone_user_parameter(self, controller: int, zone: int, parameter: str) -> Any | None:
        parameter_path = ZONE_USER_PARAMETER_PATHS.get(parameter)
        if parameter_path is None:
            return None

        parameter_hex = f"{parameter_path:02X}"
        response_signature = self.__create_response_signature(
            ZONE_USER_PARAMETER_RESPONSE_SIGNATURE.replace("@pp", parameter_hex),
            zone,
        )
        send_msg = self.__create_send_message(
            ZONE_USER_PARAMETER_REQUEST_TEMPLATE.replace("@pp", parameter_hex),
            controller,
            zone,
        )

        with self.lock:
            self.__send_data(send_msg)
            message = self.__get_response_message(response_signature)
        return self._parse_zone_user_parameter_value(parameter, message)

    def set_zone_user_parameter(self, controller: int, zone: int, parameter: str, value: Any) -> bool:
        parameter_path = ZONE_USER_PARAMETER_PATHS.get(parameter)
        if parameter_path is None:
            return False

        normalized_value = self._normalize_zone_user_parameter_value(parameter, value)
        template = ZONE_USER_PARAMETER_SET_TEMPLATE.replace("@pp", f"{parameter_path:02X}")
        send_msg = self.__create_send_message(template, controller, zone, normalized_value)
        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()
        return True

    def display_on_all_keypads(self, message: str, alignment: int = 0, flash_time: int = 0) -> bool:
        if len(message) > DISPLAY_TEXT_LENGTH - 1:
            return False
        if alignment not in (0, 1) or not 0 <= flash_time <= 0xFFFF:
            return False
        try:
            text_bytes = message.encode("ascii")
        except UnicodeEncodeError:
            return False

        payload = bytes([alignment, flash_time & 0xFF, flash_time >> 8]) + text_bytes + bytes(
            DISPLAY_TEXT_LENGTH - len(text_bytes)
        )
        template = " ".join((ALL_KEYPADS_DISPLAY_TEMPLATE, *(f"{value:02X}" for value in payload)))
        send_msg = self.__create_send_message(template, 1)

        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()
        return True

    def display_on_keypad(
        self,
        controller: int,
        zone: int,
        keypad_number: int,
        message: str,
        alignment: int = 0,
        flash_time: int = 0,
    ) -> bool:
        if len(message) > DISPLAY_TEXT_LENGTH - 1:
            return False
        if alignment not in (0, 1) or not 0 <= flash_time <= 0xFFFF:
            return False
        if not 1 <= int(keypad_number) <= 6:
            return False
        try:
            text_bytes = message.encode("ascii")
        except UnicodeEncodeError:
            return False

        keypad_hex = f"{int(keypad_number) - 1:02X}"
        payload = bytes([alignment, flash_time & 0xFF, flash_time >> 8]) + text_bytes + bytes(
            DISPLAY_TEXT_LENGTH - len(text_bytes)
        )
        template = " ".join(
            (
                SPECIFIC_KEYPAD_DISPLAY_TEMPLATE.replace("@tk", keypad_hex),
                *(f"{value:02X}" for value in payload),
            )
        )
        send_msg = self.__create_send_message(template, controller, zone)

        with self.lock:
            self.__send_data(send_msg)
            self.__get_response_message()
        return True

    def __create_send_message(
        self,
        string_message: str,
        controller: int,
        zone: int | None = None,
        parameter: int | None = None,
    ) -> list[str]:
        cc = f"{int(controller) - 1:02X}"
        zz = f"{int(zone) - 1:02X}" if zone is not None else ""
        pr = f"{int(parameter):02X}" if parameter is not None else ""

        string_message = string_message.replace("@cc", cc)
        string_message = string_message.replace("@zz", zz)
        string_message = string_message.replace("@kk", KEYPAD_CODE)
        string_message = string_message.replace("@pr", pr)

        send_msg = string_message.split()
        return self.__calc_checksum(send_msg)

    def __create_response_signature(self, string_message: str, zone: int) -> str:
        zz = f"{int(zone) - 1:02X}"
        return string_message.replace("@zz", zz)

    def __send_data(self, data: Iterable[str], delay: float = COMMAND_DELAY) -> None:
        if self.sock is None:
            raise ConnectionResetError("Russound socket is not connected")

        time_since_last_send = time.time() - self._last_send
        sleep_for = max(0.0, delay - time_since_last_send)
        time.sleep(sleep_for)

        frame_bytes = bytearray()
        for item in data:
            payload = bytes.fromhex(str(item).zfill(2))
            frame_bytes.extend(payload)
            try:
                self.sock.send(payload)
            except ConnectionResetError as exc:
                LOGGER.error("Error sending data to Russound controller: %s", exc)
                raise
        self._last_send = time.time()
        self._audit_frame("TX", frame_bytes)

    def __get_response_message(
        self,
        resp_msg_signature: str | None = None,
        delay: float = COMMAND_DELAY,
    ) -> bytearray | None:
        if self.sock is None:
            raise ConnectionResetError("Russound socket is not connected")

        no_of_socket_reads = 1 if resp_msg_signature is None else 10

        time.sleep(delay)
        self.sock.setblocking(False)

        for _ in range(no_of_socket_reads):
            self._read_available_locked()
            matching_message = self._pop_matching_frame_locked(resp_msg_signature)
            if matching_message is not None:
                return matching_message
            time.sleep(delay)
        return None

    def _update_listener_loop(self) -> None:
        while not self._update_listener_stop.wait(0.05):
            try:
                with self.lock:
                    if self.sock is None:
                        continue
                    try:
                        self.sock.setblocking(False)
                    except OSError:
                        continue
                    self._read_available_locked()
                    updates = list(self._pending_zone_updates)
                    self._pending_zone_updates.clear()
            except (ConnectionResetError, OSError) as exc:
                LOGGER.debug("Russound update listener disconnected: %s", exc)
                with self.lock:
                    if self.sock is not None:
                        try:
                            self.sock.close()
                        except OSError:
                            pass
                        self.sock = None
                return
            if self._update_callback is not None:
                for update in updates:
                    try:
                        self._update_callback(update)
                    except Exception:
                        LOGGER.exception("Russound unsolicited update callback failed")

    def _read_available_locked(self) -> None:
        if self.sock is None:
            return
        while True:
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                break
            except ConnectionResetError as exc:
                LOGGER.error("Error receiving data from Russound controller: %s", exc)
                raise
            if not chunk:
                break
            self._rx_buffer.extend(chunk)
            self._audit_rx_frames(chunk)

        frames, remainder = self._extract_complete_frames(bytes(self._rx_buffer))
        self._rx_buffer = bytearray(remainder)
        for frame in frames:
            zone_updates = self._parse_zone_updates(frame)
            if zone_updates:
                self._pending_zone_updates.extend(zone_updates)
            else:
                self._pending_frames.append(frame)

    def _pop_matching_frame_locked(self, message_signature: str | None) -> bytearray | None:
        if not self._pending_frames:
            return None
        if message_signature is None:
            return self._pending_frames.popleft()

        for frame_index, frame in enumerate(self._pending_frames):
            matching_message, _ = self.__find_signature(bytes(frame), message_signature)
            if matching_message is not None:
                del self._pending_frames[frame_index]
                return matching_message
        return None

    def _parse_zone_update(self, frame: bytearray) -> dict[str, Any] | None:
        updates = self._parse_zone_updates(frame)
        return updates[0] if updates else None

    def _parse_zone_updates(self, frame: bytearray) -> list[dict[str, Any]]:
        if len(frame) < 2 or frame[0] != 0xF0:
            return []
        if frame[7] == 0x00:
            if len(frame) < 23 or frame[9:14] != bytearray((0x04, 0x02, 0x00, frame[12], 0x07)):
                return []
            controller_id = int(frame[1]) + 1
            zone_number = int(frame[12]) + 1
            return [
                {"controller": controller_id, "zone": zone_number, "setting": "power", "value": bool(frame[20])},
                {"controller": controller_id, "zone": zone_number, "setting": "volume", "value": int(frame[22]) * 2},
            ]

        if len(frame) < 21:
            return []
        if frame[1] > 0x05 or frame[7] != 0x05 or frame[8:12] != bytearray((0x02, 0x02, 0x00, 0x00)):
            return []
        if frame[12] != 0xF1 or frame[14] != 0x00 or frame[16] != 0x00 or frame[18] != 0x00 or frame[19] != 0x01:
            return []
        if frame[13] == 0x23:
            setting = "power"
            value: Any = bool(frame[15])
        elif frame[13] == 0x21:
            setting = "volume"
            value = int(frame[15]) * 2
        else:
            return []
        return [{
            "controller": int(frame[1]) + 1,
            "zone": int(frame[17]) + 1,
            "setting": setting,
            "value": value,
        }]

    def __find_signature(self, data_stream: bytes, msg_signature: str) -> tuple[bytearray | None, bytes]:
        signature_match_index: int | None = None
        signature = bytes(int(x, 16) for x in msg_signature.split())
        index_of_last_f7: int | None = None

        for index in range(len(data_stream)):
            if data_stream[index] == 0xF7:
                index_of_last_f7 = index
            # Keep the original matching behavior: once the signature appears
            # and enough bytes exist for a complete response, return the stream
            # from that location.
            if data_stream[index : index + len(signature)] == signature and (len(data_stream) - index >= 24):
                signature_match_index = index
                break

        if signature_match_index is None:
            if index_of_last_f7 is None:
                return None, data_stream
            return None, data_stream[index_of_last_f7:]
        return bytearray(data_stream[signature_match_index:]), b""

    def __calc_checksum(self, data: list[str]) -> list[str]:
        output = len(data)
        for value in data:
            output += int(value, 16)
        checksum = f"{(output & 0x007F):02X}"
        data.append(checksum)
        data.append("F7")
        return data

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.disconnect()

    def _extract_complete_frames(self, data_stream: bytes) -> tuple[list[bytearray], bytes]:
        frames: list[bytearray] = []
        cursor = 0
        while True:
            end = data_stream.find(b"\xF7", cursor)
            if end == -1:
                break
            candidate = data_stream[cursor : end + 1]
            cursor = end + 1
            start = candidate.find(b"\xF0")
            frames.append(bytearray(candidate[start:] if start != -1 else candidate))
        remainder = data_stream[cursor:]
        return frames, remainder

    def _audit_rx_frames(self, chunk: bytes) -> None:
        if self._protocol_audit_log_file is None:
            return
        self._audit_rx_buffer.extend(chunk)
        while True:
            end = self._audit_rx_buffer.find(0xF7)
            if end == -1:
                break
            candidate = bytes(self._audit_rx_buffer[: end + 1])
            del self._audit_rx_buffer[: end + 1]
            start = candidate.find(b"\xF0")
            if start == -1:
                continue
            self._audit_frame("RX", candidate[start:])

    def _audit_frame(self, direction: str, frame: bytes | bytearray) -> None:
        if self._protocol_audit_log_file is None:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        frame_text = " ".join(f"{byte:02X}" for byte in frame)
        with self._protocol_audit_log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {direction} {frame_text}\n")

    def _parse_zone_user_parameter_value(self, parameter: str, message: bytearray | None) -> Any | None:
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
