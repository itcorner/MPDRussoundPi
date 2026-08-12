import unittest
from contextlib import nullcontext
from unittest.mock import patch

from web.russound_backend import RussoundBackend
from web.zone import Zone


class RussoundBackendTests(unittest.TestCase):
    def test_close_client_falls_back_to_socket_close_when_disconnect_missing(self):
        class DummySocket:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class DummyRussoundClient:
            def __init__(self) -> None:
                self.sock = DummySocket()

            def connect(self):
                return True

            def is_connected(self):
                return True

        backend = RussoundBackend()
        client = DummyRussoundClient()
        backend.client = client

        backend.close()

        self.assertTrue(client.sock.closed)
        self.assertIsNone(backend.client)

    def test_backend_connect_stores_reusable_client_member(self):
        class DummyRussoundClient:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                self.connected = True

            def connect(self):
                return True

            def is_connected(self):
                return self.connected

            def disconnect(self):
                self.connected = False

        backend = RussoundBackend()
        with patch("web.russound_backend.Russound", DummyRussoundClient):
            self.assertFalse(backend.is_connected())
            first_client = backend._connect()
            second_client = backend._connect()

        self.assertIsNotNone(first_client)
        self.assertIs(first_client, second_client)
        self.assertIs(backend.client, first_client)
        self.assertTrue(backend.is_connected())

    def test_backend_connect_sets_client_none_when_unavailable(self):
        class DummyFailingRussoundClient:
            def __init__(self, host, port):
                self.host = host
                self.port = port

            def connect(self):
                return False

            def is_connected(self):
                return False

            def disconnect(self):
                return None

        backend = RussoundBackend()
        with patch("web.russound_backend.Russound", DummyFailingRussoundClient):
            self.assertIsNone(backend._connect())

        self.assertIsNone(backend.client)
        self.assertFalse(backend.is_connected())

    def test_repeated_failed_connect_attempts_log_once_per_failure_state(self):
        class DummyFailingRussoundClient:
            def __init__(self, host, port):
                self.host = host
                self.port = port

            def connect(self):
                return False

            def is_connected(self):
                return False

            @property
            def sock(self):
                return None

        backend = RussoundBackend()
        backend._connect_backoff_seconds = 0.0

        with patch("web.russound_backend.Russound", DummyFailingRussoundClient), patch("web.russound_backend.logging.debug") as debug_log:
            self.assertIsNone(backend._connect())
            self.assertIsNone(backend._connect())

        matching_calls = [call for call in debug_log.call_args_list if call.args and isinstance(call.args[0], str) and "Russound backend unavailable at %s:%d; retrying in %.1fs" in call.args[0]]
        self.assertEqual(len(matching_calls), 1)

    def test_backend_endpoint_is_loaded_from_config(self):
        backend = RussoundBackend(config={"backend": {"host": "192.168.1.50", "port": 6100}})

        self.assertEqual(backend.host, "192.168.1.50")
        self.assertEqual(backend.port, 6100)

    def test_backend_endpoint_defaults_when_config_missing(self):
        backend = RussoundBackend(config={"controllers": [{"id": 1, "zone_count": 6}]})

        self.assertEqual(backend.host, "127.0.0.1")
        self.assertEqual(backend.port, 6666)

    def test_read_zone_parameters_parses_cav_zone_info_and_discrete_parameters(self):
        class DummyClient:
            def __init__(self) -> None:
                self.lock = nullcontext()
                self.requests = []

            def _Russound__create_response_signature(self, template, zone):
                signature = template.replace("@zz", f"{zone - 1:02X}")
                self.requests.append(("signature", signature))
                return signature

            def _Russound__create_send_message(self, template, controller, zone=None, parameter=None):
                self.requests.append(("send_message", template, controller, zone, parameter))
                return [template]

            def _Russound__send_data(self, send_msg):
                self.requests.append(("send", send_msg))

            def _Russound__get_response_message(self, signature):
                if signature.endswith("07"):
                    message = bytearray(24)
                    message[11:22] = bytearray([1, 2, 15, 12, 9, 1, 13, 1, 0, 2, 1])
                    return message
                message = bytearray(14)
                if signature.endswith("04"):
                    message[12] = 18
                elif signature.endswith("05"):
                    message[12] = 2
                elif signature.endswith("08"):
                    message[12] = 1
                return message

        backend = RussoundBackend()
        client = DummyClient()

        with patch.object(backend, "_connect", return_value=client):
            parameters = backend.read_zone_parameters(Zone(name="Zone 1", controller=1, zone_number=1))

        self.assertEqual(
            parameters,
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
        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.read_zone_user_parameter(Zone(name="Zone 1", controller=1, zone_number=1), "power"))
            self.assertEqual(backend.read_zone_user_parameter(Zone(name="Zone 1", controller=1, zone_number=1), "volume"), 30)

    def test_read_zone_parameter_accessors_return_normalized_values(self):
        backend = RussoundBackend()

        with patch.object(backend, "read_zone_parameters", return_value={"bass": 4, "treble": -2, "balance": 1, "loudness": True}):
            self.assertEqual(backend.read_zone_bass(Zone(name="Zone 1", controller=1, zone_number=1)), 4)
            self.assertEqual(backend.read_zone_treble(Zone(name="Zone 1", controller=1, zone_number=1)), -2)
            self.assertEqual(backend.read_zone_balance(Zone(name="Zone 1", controller=1, zone_number=1)), 1)
            self.assertTrue(backend.read_zone_loudness(Zone(name="Zone 1", controller=1, zone_number=1)))

    def test_read_discrete_zone_parameter_accessors_use_protocol_value_parsing(self):
        backend = RussoundBackend()

        with patch.object(backend, "_connect", return_value=object()), patch.object(backend, "_request_zone_user_parameter_message", return_value=bytearray([0] * 12 + [19, 0])), patch.object(backend, "_resolve_zone_address", return_value=(1, 1)):
            self.assertEqual(backend.read_zone_turn_on_volume(Zone(name="Zone 1", controller=1, zone_number=1)), 38)

        self.assertIsNone(backend.read_zone_user_parameter({"controller": 1, "zone": 1}, "background_color"))
        self.assertIsNone(backend.read_zone_user_parameter({"controller": 1, "zone": 1}, "do_not_disturb"))
        self.assertIsNone(backend.read_zone_user_parameter({"controller": 1, "zone": 1}, "party_mode"))
        self.assertIsNone(backend.read_zone_user_parameter({"controller": 1, "zone": 1}, "front_av_enable"))

    def test_set_zone_user_parameter_normalizes_bass_treble_balance_and_loudness(self):
        class DummyClient:
            def __init__(self) -> None:
                self.lock = nullcontext()
                self.calls = []

            def _Russound__create_send_message(self, template, controller, zone=None, parameter=None):
                self.calls.append((template, controller, zone, parameter))
                return [template]

            def _Russound__send_data(self, send_msg):
                self.calls.append(("send", send_msg))

            def _Russound__get_response_message(self):
                self.calls.append(("response",))

        backend = RussoundBackend()
        client = DummyClient()

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.set_zone_bass(Zone(name="Zone 1", controller=1, zone_number=1), -4))
            self.assertTrue(backend.set_zone_treble(Zone(name="Zone 1", controller=1, zone_number=1), -4))
            self.assertTrue(backend.set_zone_loudness(Zone(name="Zone 1", controller=1, zone_number=1), True))
            self.assertTrue(backend.set_zone_balance(Zone(name="Zone 1", controller=1, zone_number=1), 3))

        self.assertEqual(client.calls[0][3], 6)
        self.assertEqual(client.calls[3][3], 6)
        self.assertEqual(client.calls[6][3], 1)
        self.assertEqual(client.calls[9][3], 13)

    def test_zone_address_uses_controller_limits_when_configured(self):
        backend = RussoundBackend(config={"controllers": [{"id": 2, "zone_count": 4}]})

        self.assertTrue(backend.is_address_in_scope((2, 4)))
        self.assertFalse(backend.is_address_in_scope((2, 6)))

        self.assertEqual(backend._resolve_zone_address(Zone(name="Zone 4", controller=2, zone_number=4)), (2, 4))

    def test_zone_address_raises_when_zone_is_out_of_scope(self):
        backend = RussoundBackend(config={"controllers": [{"id": 2, "zone_count": 4}]})

        with self.assertRaisesRegex(ValueError, "out of scope"):
            backend._resolve_zone_address(Zone(name="Zone 6", controller=2, zone_number=6))

    def test_zone_address_raises_when_controller_is_unknown(self):
        backend = RussoundBackend(config={"controllers": [{"id": 2, "zone_count": 4}]})

        with self.assertRaisesRegex(ValueError, "Unsupported controller id"):
            backend._resolve_zone_address(Zone(name="Zone 3", controller=9, zone_number=3))

    def test_set_zone_power_returns_false_when_backend_is_unavailable(self):
        backend = RussoundBackend()
        with patch.object(backend, "_connect", return_value=None):
            self.assertFalse(backend.set_zone_power(Zone(name="Zone 1", controller=1, zone_number=1), True))

    def test_set_zone_source_returns_false_for_unknown_source(self):
        backend = RussoundBackend()
        with patch.object(backend, "_connect", return_value=object()):
            self.assertFalse(backend.set_zone_source(Zone(name="Zone 1", controller=1, zone_number=1), 3, [{"id": 1, "name": "Radio"}]))

    def test_set_zone_source_uses_zero_based_index_for_known_source(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def set_source(self, controller, zone, source):
                self.calls.append((controller, zone, source))

        backend = RussoundBackend()
        client = DummyClient()
        zone = Zone(name="Zone 1", controller=1, zone_number=1)

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.set_zone_source(zone, 2, [{"id": 1, "name": "Radio"}, {"id": 2, "name": "TV"}]))

        self.assertEqual(client.calls, [(1, 1, 1)])

    def test_write_methods_accept_zone_objects_without_config(self):
        backend = RussoundBackend()
        zone = Zone(name="Living Room", controller=2, zone_number=3)

        with patch.object(backend, "_connect", return_value=object()):
            self.assertFalse(backend.set_zone_power(zone, True))
            self.assertFalse(backend.set_zone_volume(zone, 50))

    def test_turn_all_zones_off_uses_client_all_off(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def all_on_off(self, power):
                self.calls.append(("all_on_off", power))

            def set_power(self, controller, zone, power):
                self.calls.append(("set_power", controller, zone, power))

        backend = RussoundBackend()
        client = DummyClient()

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.turn_all_zones_off())

        self.assertEqual(client.calls, [("all_on_off", 0)])

    def test_turn_all_zones_on_only_turns_on_provided_zones(self):
        class DummyClient:
            def __init__(self) -> None:
                self.calls = []

            def all_on_off(self, power):
                self.calls.append(("all_on_off", power))

            def set_power(self, controller, zone, power):
                self.calls.append(("set_power", controller, zone, power))

        backend = RussoundBackend()
        client = DummyClient()
        zones = [Zone(name="Zone 1", controller=2, zone_number=1), Zone(name="Zone 3", controller=2, zone_number=3)]

        with patch.object(backend, "_connect", return_value=client):
            self.assertTrue(backend.turn_all_zones_on(zones))

        self.assertEqual(
            client.calls,
            [
                ("set_power", 2, 1, 1),
                ("set_power", 2, 3, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
