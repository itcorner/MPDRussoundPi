import logging
import sys
import unittest
from queue import Queue
from unittest.mock import patch
from urllib.parse import urlparse

from web.russound_server import RussoundHTTPServer, RussoundRequestHandler, _configure_logging


class RussoundServerTests(unittest.TestCase):
    def test_broadcast_state_change_increments_revision_and_notifies_clients(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            _client_id, event_queue = server.register_event_client("127.0.0.1", "test-agent")

            self.assertEqual(server.state_revision, 0)

            server.broadcast_state_change()

            self.assertEqual(server.state_revision, 1)
            self.assertEqual(event_queue.get_nowait(), '{"revision": 1}')
        finally:
            server.server_close()

    def test_authorization_accepts_matching_header_token(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            handler = object.__new__(RussoundRequestHandler)
            handler.server = server
            handler.headers = {"X-Russound-Api-Token": server.api_token}

            self.assertTrue(handler._is_authorized(server, urlparse("/api/state")))
        finally:
            server.server_close()

    def test_authorization_accepts_matching_query_token(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            handler = object.__new__(RussoundRequestHandler)
            handler.server = server
            handler.headers = {}

            self.assertTrue(handler._is_authorized(server, urlparse(f"/api/events?token={server.api_token}")))
        finally:
            server.server_close()

    def test_authorization_rejects_missing_token(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            handler = object.__new__(RussoundRequestHandler)
            handler.server = server
            handler.headers = {}

            self.assertFalse(handler._is_authorized(server, urlparse("/api/state")))
        finally:
            server.server_close()

    def test_state_has_zone_address_matches_known_controller_and_zone(self):
        handler = object.__new__(RussoundRequestHandler)

        state = {"zones": [{"controller": 1, "zone": 1}, {"controller": 2, "zone": 3}]}
        self.assertTrue(handler._state_has_zone_address(state, 1, 1))
        self.assertFalse(handler._state_has_zone_address(state, 1, 3))

    def test_match_controller_zone_route_parses_expected_shape(self):
        handler = object.__new__(RussoundRequestHandler)

        self.assertEqual(handler._match_controller_zone_route("/api/controller/1/zone/3/power"), (1, 3, "power"))
        self.assertEqual(handler._match_controller_zone_route("/api/controller/1/zone/3/bass"), (1, 3, "bass"))
        self.assertEqual(handler._match_controller_zone_route("/api/controller/1/zone/3/treble"), (1, 3, "treble"))
        self.assertIsNone(handler._match_controller_zone_route("/api/zones/living/power"))

    def test_state_has_zone_addresses_requires_all_addresses_to_exist(self):
        handler = object.__new__(RussoundRequestHandler)
        state = {"zones": [{"controller": 1, "zone": 1}, {"controller": 1, "zone": 2}]}

        self.assertTrue(handler._state_has_zone_addresses(state, [(1, 1), (1, 2)]))
        self.assertFalse(handler._state_has_zone_addresses(state, [(1, 1), (2, 1)]))

    def test_state_has_input_matches_known_source_id(self):
        handler = object.__new__(RussoundRequestHandler)
        state = {"inputs": [{"id": 1}, {"id": 2}]}

        self.assertTrue(handler._state_has_input(state, 1))
        self.assertFalse(handler._state_has_input(state, 3))

    def test_shortcut_unknown_zone_is_server_configuration_error(self):
        handler = object.__new__(RussoundRequestHandler)
        state = {"zones": [{"controller": 1, "zone": 1}], "inputs": [{"id": 1}]}
        shortcut_zone_addresses = [(1, 1), (2, 1)]

        self.assertFalse(handler._state_has_zone_addresses(state, shortcut_zone_addresses))

    def test_shortcut_unknown_source_is_server_configuration_error(self):
        handler = object.__new__(RussoundRequestHandler)
        state = {"zones": [{"id": "living"}], "inputs": [{"id": 1}]}
        shortcut = {"id": "party", "zone_ids": ["living"], "source": 2}

        self.assertFalse(handler._state_has_input(state, shortcut["source"]))

    def test_read_bool_field_requires_real_boolean(self):
        handler = object.__new__(RussoundRequestHandler)

        self.assertTrue(handler._read_bool_field({"power": True}, "power"))
        self.assertFalse(handler._read_bool_field({"power": False}, "power"))
        self.assertIsNone(handler._read_bool_field({"power": 1}, "power"))
        self.assertIsNone(handler._read_bool_field({}, "power"))

    def test_read_int_field_requires_real_integer_but_not_boolean(self):
        handler = object.__new__(RussoundRequestHandler)

        self.assertEqual(handler._read_int_field({"volume": 10}, "volume"), 10)
        self.assertIsNone(handler._read_int_field({"volume": True}, "volume"))
        self.assertIsNone(handler._read_int_field({"volume": "10"}, "volume"))
        self.assertIsNone(handler._read_int_field({}, "volume"))

    def test_status_payload_lists_connected_clients(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            client_id, _event_queue = server.register_event_client("127.0.0.1", "status-test")

            payload = server.build_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], client_id)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
        finally:
            server.server_close()

    def test_status_client_payload_lists_connected_clients(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            client_id, _event_queue = server.register_event_client("127.0.0.1", "status-test")

            payload = server.build_client_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], client_id)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
        finally:
            server.server_close()

    def test_status_client_payload_deduplicates_clients_from_same_session(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            server.register_event_client("127.0.0.1", "status-test", "session-123")
            server.register_event_client("127.0.0.1", "status-test", "session-123")

            payload = server.build_client_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
        finally:
            server.server_close()

    def test_reconnecting_same_session_keeps_only_latest_active_client(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            first_client_id, _ = server.register_event_client("127.0.0.1", "status-test", "session-123")
            second_client_id, _ = server.register_event_client("127.0.0.1", "status-test", "session-123")

            self.assertEqual(first_client_id, second_client_id)

            payload = server.build_client_status_payload()
            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], second_client_id)

            server.unregister_event_client(second_client_id)
            payload = server.build_client_status_payload()
            self.assertEqual(payload["connected_clients"], [])
        finally:
            server.server_close()

    def test_reconnecting_after_disconnect_reuses_existing_session_entry(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            first_client_id, _ = server.register_event_client("127.0.0.1", "status-test", "session-456")
            server.unregister_event_client(first_client_id)

            second_client_id, _ = server.register_event_client("127.0.0.1", "status-test", "session-456")

            self.assertEqual(first_client_id, second_client_id)
            payload = server.build_client_status_payload()
            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], second_client_id)
        finally:
            server.server_close()

    def test_status_history_payload_keeps_last_fifty_frontend_events(self):
        server = RussoundHTTPServer(("127.0.0.1", 0), RussoundRequestHandler, None, None)
        try:
            for index in range(55):
                server.record_frontend_event("127.0.0.1", f"/api/test/{index}", {"index": index})

            payload = server.build_history_status_payload()

            self.assertEqual(len(payload["recent_events"]), 50)
            self.assertEqual(payload["recent_events"][0]["payload"], {"index": 54})
            self.assertEqual(payload["recent_events"][-1]["payload"], {"index": 5})
        finally:
            server.server_close()

    def test_configure_logging_enables_debug_output(self):
        with patch("web.russound_server.logging.basicConfig") as basic_config, patch("web.russound_server.logging.getLogger") as get_logger:
            _configure_logging(True)

        basic_config.assert_called_once()
        self.assertEqual(basic_config.call_args.kwargs["level"], logging.DEBUG)
        self.assertEqual(basic_config.call_args.kwargs["stream"], sys.stdout)
        self.assertGreater(get_logger.call_count, 1)
        self.assertEqual(get_logger.return_value.setLevel.call_args_list[0].args[0], logging.DEBUG)


if __name__ == "__main__":
    unittest.main()