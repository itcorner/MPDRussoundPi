import unittest
from unittest.mock import patch

from web.russound_backend import RussoundBackend
from web.zone import Zone


class RussoundBackendTests(unittest.TestCase):
    def test_zone_address_uses_controller_limits_when_configured(self):
        backend = RussoundBackend()
        config = {"controllers": [{"id": 2, "zone_count": 4}]}

        controller, zone_number = backend._resolve_zone_address({"controller": 2, "zone": 6}, config)

        self.assertEqual((controller, zone_number), (2, 4))

    def test_zone_address_falls_back_to_default_controller_when_unknown(self):
        backend = RussoundBackend()
        config = {"controllers": [{"id": 2, "zone_count": 4}]}

        controller, zone_number = backend._resolve_zone_address({"controller": 9, "zone": 3}, config)

        self.assertEqual((controller, zone_number), (1, 3))

    def test_set_zone_power_returns_false_when_backend_is_unavailable(self):
        backend = RussoundBackend()
        with patch.object(backend, "_connect", return_value=None):
            self.assertFalse(backend.set_zone_power({"controller": 1, "zone": 1}, True))

    def test_set_zone_source_returns_false_for_unknown_source(self):
        backend = RussoundBackend()
        with patch.object(backend, "_connect", return_value=object()):
            self.assertFalse(backend.set_zone_source({"controller": 1, "zone": 1}, 3, [{"id": 1, "name": "Radio"}]))

    def test_set_zone_source_returns_false_for_non_string_source(self):
        backend = RussoundBackend()
        with patch.object(backend, "_connect", return_value=object()):
            self.assertFalse(backend.set_zone_source({"controller": 1, "zone": 1}, 1, [{"id": 1, "name": "Radio"}]))

    def test_write_methods_accept_zone_objects_without_config(self):
        backend = RussoundBackend()
        zone = Zone(name="Living Room", controller=2, zone_number=3)

        with patch.object(backend, "_connect", return_value=object()):
            self.assertFalse(backend.set_zone_power(zone, True))
            self.assertFalse(backend.set_zone_volume(zone, 50))

    def test_set_all_power_uses_explicit_client_all_off_for_power_down(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def all_on_off(self, power):
                self.calls.append(("all_on_off", power))

            def set_power(self, controller, zone, power):
                self.calls.append(("set_power", controller, zone, power))

        backend = RussoundBackend(controller=2)
        client = DummyClient()

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.set_all_power(False, 6))

        self.assertEqual(client.calls, [("all_on_off", 0)])

    def test_set_all_power_keeps_zonewise_power_on(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def all_on_off(self, power):
                self.calls.append(("all_on_off", power))

            def set_power(self, controller, zone, power):
                self.calls.append(("set_power", controller, zone, power))

        backend = RussoundBackend(controller=2)
        client = DummyClient()

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.set_all_power(True, 3))

        self.assertEqual(
            client.calls,
            [
                ("set_power", 2, 1, 1),
                ("set_power", 2, 2, 1),
                ("set_power", 2, 3, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
