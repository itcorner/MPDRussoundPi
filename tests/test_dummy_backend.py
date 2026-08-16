from __future__ import annotations

import tempfile
from pathlib import Path
import threading
import time
import unittest

from web.russound_connector import Russound

from tool.dummy_backend.dummy_backend import DummyRussoundRequestHandler, ThreadedDummyRussoundServer, load_state


class DummyRussoundBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadedDummyRussoundServer(("127.0.0.1", 0), DummyRussoundRequestHandler, load_state(None))
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.server.state = load_state(None)
        self.client = Russound("127.0.0.1", self.port)
        self.assertTrue(self.client.connect())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_zone_read_and_write_round_trip(self) -> None:
        initial_power = self.client.get_power(1, 1)
        initial_source = self.client.get_source(1, 1)
        initial_volume = self.client.get_volume(1, 1)
        initial_zone_info = self.client.get_zone_info(1, 1, 4)

        self.assertIsInstance(initial_power, int)
        self.assertIsInstance(initial_source, int)
        self.assertIsInstance(initial_volume, int)
        self.assertIsInstance(initial_zone_info, list)

        self.client.set_power(1, 1, 1)
        self.client.set_source(1, 1, 2)
        self.client.set_volume(1, 1, 50)

        self.assertEqual(self.client.get_power(1, 1), 1)
        self.assertEqual(self.client.get_source(1, 1), 2)
        self.assertEqual(self.client.get_volume(1, 1), 50)
        self.assertEqual(self.client.get_zone_info(1, 1, 4), [1, 2, 25])

    def test_system_off_turns_all_dummy_zones_off(self) -> None:
        for controller_id, zone_number in self.server.state.zone_addresses():
            self.server.state.zone(controller_id, zone_number).power = True

        self.client.all_on_off(0)

        self.assertTrue(self.server.state.zone_addresses())
        self.assertTrue(all(not self.server.state.zone(controller_id, zone_number).power for controller_id, zone_number in self.server.state.zone_addresses()))

    def test_state_save_and_reload_round_trip(self) -> None:
        state = load_state(None)
        initial_power = state.zone(2, 1).power
        initial_volume = state.zone(2, 1).volume
        state.zone(2, 1).adjust_field("volume", 7)
        state.zone(2, 1).adjust_field("power", 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state.save_to_file(state_path)

            reloaded = load_state(state_path)
            expected_volume = min(100, initial_volume + 14)
            if expected_volume % 2 != 0:
                expected_volume -= 1
            self.assertEqual(reloaded.zone(2, 1).volume, expected_volume)
            self.assertEqual(reloaded.zone(2, 1).volume % 2, 0)
            self.assertEqual(reloaded.zone(2, 1).power, (not initial_power))

    def test_out_of_band_zone_change_is_broadcast_to_connected_clients(self) -> None:
        updates: list[dict[str, object]] = []
        update_received = threading.Event()
        listener = Russound("127.0.0.1", self.port)
        self.assertTrue(listener.connect())
        listener.start_update_listener(lambda update: (updates.append(update), update_received.set()))
        self.server.state.set_zone_update_callback(self.server.broadcast_zone_info)
        initial_client_count = len(self.server._client_sockets)
        registration_deadline = time.monotonic() + 1.0
        while len(self.server._client_sockets) <= initial_client_count and time.monotonic() < registration_deadline:
            time.sleep(0.01)

        self.server.state.zone(1, 1).apply_volume_value(25)
        self.server.state.notify_zone_update(1, 1)

        self.assertTrue(update_received.wait(1.0))
        self.assertIn({"controller": 1, "zone": 1, "setting": "volume", "value": 50}, updates)
        listener.close()

    def test_broadcast_zone_info_checksum_includes_pre_checksum_byte_count(self) -> None:
        class CaptureSocket:
            def __init__(self) -> None:
                self.frames: list[bytes] = []

            def sendall(self, frame: bytes) -> None:
                self.frames.append(frame)

        capture_socket = CaptureSocket()
        self.server.register_client(capture_socket)
        try:
            self.server.broadcast_zone_info(1, 1)
        finally:
            self.server.unregister_client(capture_socket)

        frame = capture_socket.frames[0]
        self.assertEqual(frame[-2], (sum(frame[:-2]) + len(frame) - 2) & 0x7F)

    def test_display_on_all_keypads_round_trip(self) -> None:
        self.server.state.zone(1, 1)
        self.server.state.zone(2, 1)
        self.assertTrue(self.client.display_on_all_keypads("Hello", alignment=1, flash_time=25))

        keypad_display_c1z1 = self.server.state.keypad_display(1, 1, 1)
        keypad_display_c2z1 = self.server.state.keypad_display(2, 1, 1)
        self.assertEqual(keypad_display_c1z1.message, "Hello")
        self.assertEqual(keypad_display_c1z1.alignment, 1)
        self.assertEqual(keypad_display_c1z1.flash_time, 25)
        self.assertEqual(keypad_display_c2z1.message, "Hello")
        self.assertEqual(keypad_display_c2z1.alignment, 1)
        self.assertEqual(keypad_display_c2z1.flash_time, 25)

    def test_display_on_specific_keypad_round_trip(self) -> None:
        self.assertTrue(self.client.display_on_keypad(2, 1, 1, "Zone2", alignment=1, flash_time=40))

        keypad_display = self.server.state.keypad_display(2, 1, 1)
        self.assertEqual(keypad_display.message, "Zone2")
        self.assertEqual(keypad_display.alignment, 1)
        self.assertEqual(keypad_display.flash_time, 40)

    def test_display_for_an_unsimulated_keypad_is_ignored(self) -> None:
        self.assertTrue(self.client.display_on_keypad(2, 1, 3, "Zone2", alignment=1, flash_time=40))

        self.assertEqual(self.server.state.keypad_display(2, 1, 1).message, "")


if __name__ == "__main__":
    unittest.main()
