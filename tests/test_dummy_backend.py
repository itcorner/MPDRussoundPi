from __future__ import annotations

import tempfile
from pathlib import Path
import threading
import unittest

from russound.russound import Russound

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
        self.client = Russound("127.0.0.1", self.port)
        self.assertTrue(self.client.connect())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_zone_read_and_write_round_trip(self) -> None:
        self.assertEqual(self.client.get_power(1, 1), 0)
        self.assertEqual(self.client.get_source(1, 1), 0)
        self.assertEqual(self.client.get_volume(1, 1), 20)
        self.assertEqual(self.client.get_zone_info(1, 1, 4), [0, 0, 10])

        self.client.set_power(1, 1, 1)
        self.client.set_source(1, 1, 2)
        self.client.set_volume(1, 1, 50)

        self.assertEqual(self.client.get_power(1, 1), 1)
        self.assertEqual(self.client.get_source(1, 1), 2)
        self.assertEqual(self.client.get_volume(1, 1), 50)
        self.assertEqual(self.client.get_zone_info(1, 1, 4), [1, 2, 25])

    def test_state_save_and_reload_round_trip(self) -> None:
        state = load_state(None)
        state.zone(2, 1).adjust_field("volume", 7)
        state.zone(2, 1).adjust_field("power", 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state.save_to_file(state_path)

            reloaded = load_state(state_path)
            self.assertEqual(reloaded.zone(2, 1).volume, 41)
            self.assertFalse(reloaded.zone(2, 1).power)


if __name__ == "__main__":
    unittest.main()
