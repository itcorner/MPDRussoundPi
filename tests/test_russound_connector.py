from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from web.russound_connector import Russound


class _FakeSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.recv_queue: list[bytes] = []
        self.closed = False

    def connect(self, address) -> None:
        _ = address

    def send(self, payload: bytes) -> None:
        self.sent.extend(payload)

    def recv(self, _size: int) -> bytes:
        if self.recv_queue:
            return self.recv_queue.pop(0)
        raise BlockingIOError()

    def setblocking(self, _flag: bool) -> None:
        return

    def getpeername(self):
        return ("127.0.0.1", 6666)

    def close(self) -> None:
        self.closed = True


class RussoundConnectorTests(unittest.TestCase):
    def test_update_listener_handles_connection_reset(self) -> None:
        class ResetSocket(_FakeSocket):
            def recv(self, _size: int) -> bytes:
                raise ConnectionResetError(54, "Connection reset by peer")

        connector = Russound("127.0.0.1", 6666)
        connector.sock = ResetSocket()
        with patch.object(connector._update_listener_stop, "wait", return_value=False):
            connector._update_listener_loop()

        self.assertIsNone(connector.sock)

    def test_parse_unsolicited_power_set_data_update(self) -> None:
        connector = Russound("127.0.0.1", 6666)
        frame = bytearray.fromhex("F0 00 00 7F 00 00 70 05 02 02 00 00 F1 23 00 01 00 02 00 01 F7")

        self.assertEqual(
            connector._parse_zone_update(frame),
            {"controller": 1, "zone": 3, "setting": "power", "value": True},
        )

    def test_parse_unsolicited_volume_set_data_update_converts_wire_units(self) -> None:
        connector = Russound("127.0.0.1", 6666)
        frame = bytearray.fromhex("F0 01 00 7F 00 00 70 05 02 02 00 00 F1 21 00 19 00 00 00 01 F7")

        self.assertEqual(
            connector._parse_zone_update(frame),
            {"controller": 2, "zone": 1, "setting": "volume", "value": 50},
        )

    def test_parse_controller_set_data_all_zone_info_emits_power_and_volume(self) -> None:
        connector = Russound("127.0.0.1", 6666)
        frame = bytearray.fromhex("F0 00 00 70 00 00 7F 00 00 04 02 00 01 07 00 00 00 00 00 00 01 00 19 0A 0A 00 0A 00 00 00 00 F7")

        self.assertEqual(
            connector._parse_zone_updates(frame),
            [
                {"controller": 1, "zone": 2, "setting": "power", "value": True},
                {"controller": 1, "zone": 2, "setting": "volume", "value": 50},
            ],
        )

    def test_calc_checksum_includes_pre_checksum_byte_count(self) -> None:
        connector = Russound("127.0.0.1", 6666)
        frame = connector._Russound__calc_checksum(["F0", "00", "67", "7C", "F1", "0F"])

        self.assertEqual(frame[-2], "59")
        self.assertEqual(frame[-1], "F7")

    def test_all_on_off_uses_re_target_controller_marker(self) -> None:
        fake_sock = _FakeSocket()
        with patch("web.russound_connector.socket.socket", return_value=fake_sock):
            connector = Russound("127.0.0.1", 6666)
            self.assertTrue(connector.connect())
            connector.all_on_off(0)

        sent_hex = " ".join(f"{byte:02X}" for byte in fake_sock.sent)
        self.assertTrue(sent_hex.startswith("F0 7E 00 7F 00 00 70 05 02 02 00 00 F1 22"))

    def test_protocol_audit_logs_tx_and_rx_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "protocol-audit.log"
            fake_sock = _FakeSocket()
            fake_sock.recv_queue.append(bytes.fromhex("F0 00 00 70 00 00 7F 00 00 04 02 00 00 07 00 00 01 00 0C 00 0A 0A 0A F7"))

            with patch("web.russound_connector.socket.socket", return_value=fake_sock):
                connector = Russound("127.0.0.1", 6666, protocol_audit_log_file=log_path)
                self.assertTrue(connector.connect())
                connector.get_zone_info(1, 1, 0)

            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn(" TX ", log_text)
            self.assertIn(" RX ", log_text)
            self.assertIn("F0", log_text)
            self.assertIn("F7", log_text)

    def test_get_zone_extended_info_reads_zone_info_and_turn_on_volume(self) -> None:
        fake_sock = _FakeSocket()
        fake_sock.recv_queue.append(
            bytes(
                [
                    0x04,
                    0x02,
                    0x00,
                    0x00,
                    0x07,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x01,
                    0x02,
                    0x0F,
                    0x0C,
                    0x09,
                    0x01,
                    0x0D,
                    0x01,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0xF7,
                ]
            )
        )
        fake_sock.recv_queue.append(
            bytes(
                [
                    0x05,
                    0x02,
                    0x00,
                    0x00,
                    0x00,
                    0x04,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x12,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0xF7,
                ]
            )
        )

        with patch("web.russound_connector.socket.socket", return_value=fake_sock):
            connector = Russound("127.0.0.1", 6666)
            self.assertTrue(connector.connect())
            info = connector.get_zone_extended_info(1, 1)

        self.assertEqual(
            info,
            {
                "power": True,
                "source_index": 2,
                "volume": 30,
                "bass": 2,
                "treble": -1,
                "loudness": True,
                "balance": 3,
                "system_power": True,
                "shared_source": False,
                "turn_on_volume": 36,
            },
        )

    def test_set_zone_user_parameter_normalizes_bass(self) -> None:
        fake_sock = _FakeSocket()
        with patch("web.russound_connector.socket.socket", return_value=fake_sock):
            connector = Russound("127.0.0.1", 6666)
            self.assertTrue(connector.connect())
            self.assertTrue(connector.set_zone_user_parameter(1, 1, "bass", -4))

        sent_hex = " ".join(f"{byte:02X}" for byte in fake_sock.sent)
        self.assertIn("00 00 00 01 00 01 00 06", sent_hex)

    def test_display_methods_emit_protocol_frames(self) -> None:
        fake_sock = _FakeSocket()
        with patch("web.russound_connector.socket.socket", return_value=fake_sock):
            connector = Russound("127.0.0.1", 6666)
            self.assertTrue(connector.connect())
            self.assertTrue(connector.display_on_all_keypads("Hello", alignment=1, flash_time=25))
            self.assertTrue(connector.display_on_keypad(2, 3, 1, "Hi", alignment=1, flash_time=40))

        sent_hex = " ".join(f"{byte:02X}" for byte in fake_sock.sent)
        self.assertIn("F0 7F 00 00 00 00 70 00 02 01 01 00 00 00 01 00 10 00 01 19 00 48 65 6C 6C 6F", sent_hex)
        self.assertIn("F0 01 02 00 00 00 70 00 02 01 01 00 00 00 01 00 10 00 01 28 00 48 69", sent_hex)


if __name__ == "__main__":
    unittest.main()
