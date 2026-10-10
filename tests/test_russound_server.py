import logging
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from web.russound_server import RussoundHTTPServer, RussoundRequestHandler, _configure_logging, _connection_socket_options
from web.russound_state import RussoundState
from web.zone import Zone


def _close_server(server: RussoundHTTPServer) -> None:
    close_fn = getattr(server, "server_close", None)
    if callable(close_fn):
        close_fn()


class RussoundServerTests(unittest.TestCase):
    def test_system_off_backend_failure_returns_error_without_publishing(self):
        server = RussoundHTTPServer(None, None)
        server.controller = Mock()
        server.controller.handle_system_power_change.side_effect = RuntimeError("Backend command failed")
        with patch.object(server, "broadcast_state_change") as broadcast:
            response = server.app.test_client().post("/api/system/power", json={"power": False})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["error"], "Backend command failed")
        broadcast.assert_not_called()

    def test_state_api_and_events_return_snapshot_while_hardware_poll_is_blocked(self):
        server = RussoundHTTPServer(None, None)
        controller = Mock()
        server.controller = controller
        previous = {"config_required": False, "state": {"zones": [{"volume": 20}]}}
        refreshed = {"config_required": False, "state": {"zones": [{"volume": 42}]}}
        controller.build_view_payload.return_value = previous
        server._read_state_payload()
        current_state = Mock()
        current_state.to_payload.return_value = previous["state"]
        new_state = Mock()
        new_state.to_payload.return_value = refreshed["state"]
        poll_entered = threading.Event()
        release_poll = threading.Event()
        read_done = threading.Event()
        result = {}

        def load_state(**kwargs):
            if kwargs.get("refresh_backend"):
                poll_entered.set()
                if not release_poll.wait(5):
                    raise RuntimeError("Test poll was not released")
                controller.build_view_payload.return_value = refreshed
                return new_state
            return current_state

        def read_state():
            with server.app.test_client() as client:
                response = client.get("/api/state")
                result["http"] = response.status_code
                result["payload"] = response.json
            result["event"] = server._build_state_payload_for_event()
            read_done.set()

        controller.load_state.side_effect = load_state
        poller = threading.Thread(target=server._sync_backend_state_if_changed)
        reader = threading.Thread(target=read_state)
        poller.start()
        try:
            self.assertTrue(poll_entered.wait(2))
            reader.start()
            self.assertTrue(read_done.wait(1), "State reads waited for hardware polling")
            self.assertEqual(result, {"http": 200, "payload": previous, "event": previous})
        finally:
            release_poll.set()
            poller.join(2)
            if reader.ident is not None:
                reader.join(2)
        self.assertFalse(poller.is_alive())
        controller.persist_state.assert_called_once_with(new_state)
        with server.state_lock:
            self.assertEqual(server.app.test_client().get("/api/state").json, refreshed)

    def test_state_api_returns_latest_command_snapshot_while_lock_is_busy(self):
        server = RussoundHTTPServer(None, None)
        server.controller = Mock()
        previous = {"state": {"zones": [{"volume": 20}]}}
        updated = {"state": {"zones": [{"volume": 60}]}}
        server.controller.build_view_payload.return_value = previous
        server._read_state_payload()
        server.controller.handle_zone_setting_change.return_value = updated
        server.controller.build_view_payload.return_value = updated
        with server.app.test_client() as client:
            response = client.post("/api/controller/1/zone/1/volume", json={"volume": 60})
            self.assertEqual(response.status_code, 200)
            with server.state_lock:
                self.assertEqual(client.get("/api/state").json, updated)

    def test_setup_routes_recover_invalid_config_and_replace_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text("{broken")
            server = RussoundHTTPServer(config_path, Path(directory) / "state.json")
            client = server.app.test_client()
            with client.get("/") as response:
                self.assertEqual(response.status_code, 200)
            with client.get("/config") as response:
                self.assertEqual(response.status_code, 200)
            self.assertTrue(client.get("/api/config").json["config_required"])
            self.assertTrue(client.get("/api/state").json["config_required"])
            old_backend = Mock()
            server._event_backend = old_backend
            server.controller.set_shared_backend(old_backend)
            with patch("web.russound_controller.RussoundBackend.connect", side_effect=AssertionError("Save must be offline")) as connect:
                response = client.post("/api/config", json={
                    "backend": {"host": "new-gateway", "port": 7777, "poll_interval_seconds": 7.5},
                    "controllers": [{"id": 1, "zone_count": 6}],
                    "zone_slots": [], "source_slots": [],
                })
                connect.assert_not_called()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["config_required"])
            self.assertEqual(response.json["config"]["backend"]["poll_interval_seconds"], 7.5)
            self.assertEqual(server._backend_poll_interval_seconds, 7.5)
            old_backend.close.assert_called_once()
            self.assertIsNone(server._event_backend)
            self.assertTrue(server._full_poll_requested.is_set())
            with server.state_lock:
                snapshot = client.get("/api/state").json
                self.assertFalse(snapshot["config_required"])
                self.assertEqual(snapshot["config"]["backend"]["host"], "new-gateway")
            with client.get("/") as response:
                self.assertEqual(response.status_code, 200)
            with patch("web.russound_server.RussoundBackend") as backend_class:
                backend_class.return_value.start_update_listener.return_value = False
                server._ensure_update_listener()
                self.assertEqual(backend_class.call_args.kwargs["config"]["backend"]["host"], "new-gateway")
                self.assertIs(server._event_backend, backend_class.return_value)

    def test_old_backend_updates_are_ignored_after_reconfiguration(self):
        server = RussoundHTTPServer(None, None)
        server.controller = Mock()
        server._backend_generation = 1
        server._handle_unsolicited_zone_update({"controller": 1, "zone": 1, "setting": "power", "value": True}, generation=0)
        server.controller.load_state.assert_not_called()

    def test_config_save_preserves_connection_health_when_endpoint_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "backend": {"host": "gateway", "port": 6666},
                "controllers": [{"id": 1, "zone_count": 6}], "zones": [], "inputs": [],
            }))
            server = RussoundHTTPServer(config_path, Path(directory) / "state.json")
            backend = Mock()
            backend.is_connected.return_value = True
            server._event_backend = backend
            server.controller.set_shared_backend(backend)
            server.controller._record_backend_health(True, True, 1)
            generation = server._backend_generation
            client = server.app.test_client()
            response = client.post("/api/config", json={
                "backend": {"host": "gateway", "port": 6666, "poll_interval_seconds": 7.5},
                "controllers": [{"id": 1, "zone_count": 4}],
                "zone_slots": [{"controller": 1, "zone": 1, "name": "Kitchen", "enabled": True, "visible": True}],
                "source_slots": [{"id": 1, "name": "Radio", "enabled": True}],
                "shortcuts": [],
            })
            self.assertEqual(response.status_code, 200)
            self.assertIs(server._event_backend, backend)
            backend.close.assert_not_called()
            self.assertEqual(server._backend_generation, generation)
            self.assertEqual(backend.config["controllers"], [{"id": 1, "zone_count": 4}])
            self.assertEqual(server._backend_poll_interval_seconds, 7.5)
            with server.state_lock:
                self.assertTrue(client.get("/api/state").json["backend_status"]["connected"])

    def test_setup_write_failure_returns_actionable_json_error(self):
        server = RussoundHTTPServer(None, None)
        server.controller = Mock()
        server.controller.update_config_zones.side_effect = PermissionError("read-only volume")
        response = server.app.test_client().post("/api/config", json={})
        self.assertEqual(response.status_code, 500)
        self.assertIn("permissions", response.json["error"])
        self.assertIn("read-only volume", response.json["error"])
        self.assertFalse(server._backend_reconfiguring.is_set())

    def test_unsolicited_zone_update_persists_and_broadcasts_power_and_volume(self):
        server = RussoundHTTPServer(None, None)
        try:
            state = RussoundState(zones=[Zone(name="Living Room", controller=1, zone_number=1)])
            controller = Mock()
            controller.load_state.return_value = state
            controller.build_view_payload.return_value = {"state": {"zones": [{"volume": 46}]}}
            server.controller = controller
            _client_id, event_queue = server.register_event_client("127.0.0.1", "test-agent")

            server._handle_unsolicited_zone_update({"controller": 1, "zone": 1, "setting": "power", "value": True})
            server._handle_unsolicited_zone_update({"controller": 1, "zone": 1, "setting": "volume", "value": 46})

            self.assertTrue(state.zones[0].power)
            self.assertEqual(state.zones[0].volume, 46)
            self.assertEqual(controller.persist_state.call_count, 2)
            self.assertEqual(json.loads(event_queue.get_nowait())["payload"]["state"]["zones"][0]["volume"], 46)
        finally:
            _close_server(server)

    def test_unsolicited_source_update_requires_known_input(self):
        server = RussoundHTTPServer(None, None)
        try:
            state = RussoundState(
                zones=[Zone(name="Living Room", controller=1, zone_number=1, source=1)],
                inputs=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            )
            controller = Mock()
            controller.load_state.return_value = state
            controller.build_view_payload.return_value = {}
            server.controller = controller

            server._handle_unsolicited_zone_update({"controller": 1, "zone": 1, "setting": "source", "value": 9})
            self.assertEqual(state.zones[0].source, 1)

            server._handle_unsolicited_zone_update({"controller": 1, "zone": 1, "setting": "source", "value": 2})
            self.assertEqual(state.zones[0].source, 2)
            controller.persist_state.assert_called_once_with(state)
        finally:
            _close_server(server)

    def test_refresh_hint_queues_targeted_zone_refresh(self):
        server = RussoundHTTPServer(None, None)
        try:
            controller = Mock()
            server.controller = controller

            server._handle_unsolicited_zone_update({"controller": 1, "zone": 2, "setting": "refresh"})

            controller.load_state.assert_not_called()
            self.assertTrue(server._zone_refresh_requested.is_set())
            self.assertEqual(server._take_zone_refresh_addresses(), {(1, 2)})
            self.assertFalse(server._zone_refresh_requested.is_set())
        finally:
            _close_server(server)

    def test_refresh_zones_if_changed_persists_refreshed_state(self):
        server = RussoundHTTPServer(None, None)
        try:
            current_state = Mock()
            refreshed_state = Mock()
            current_state.to_payload.return_value = {"zones": [{"power": False}]}
            refreshed_state.to_payload.return_value = {"zones": [{"power": True}]}
            controller = Mock()
            controller.load_state.return_value = current_state
            controller.refresh_zones.return_value = refreshed_state
            shared_backend = Mock()
            server.controller = controller
            server._event_backend = shared_backend

            self.assertTrue(server._refresh_zones_if_changed({(1, 1)}))

            controller.refresh_zones.assert_called_once_with({(1, 1)}, backend=shared_backend)
            controller.persist_state.assert_called_once_with(refreshed_state)
        finally:
            _close_server(server)

    def test_ensure_update_listener_reconnects_dropped_gateway_with_backoff(self):
        server = RussoundHTTPServer(None, None)
        try:
            backend = Mock()
            backend.client = None
            backend.connect.return_value = False
            server._event_backend = backend

            with patch("web.russound_server.time.monotonic", return_value=100.0):
                server._ensure_update_listener()
                server._ensure_update_listener()
            self.assertEqual(backend.connect.call_count, 1)

            backend.connect.return_value = True
            with patch("web.russound_server.time.monotonic", return_value=200.0):
                server._ensure_update_listener()
            self.assertEqual(backend.connect.call_count, 2)
        finally:
            _close_server(server)

    def test_ensure_update_listener_skips_connected_gateway(self):
        server = RussoundHTTPServer(None, None)
        try:
            backend = Mock()
            backend.client.is_connected.return_value = True
            server._event_backend = backend

            server._ensure_update_listener()

            backend.connect.assert_not_called()
        finally:
            _close_server(server)

    def test_start_listener_keeps_shared_backend_when_gateway_is_down(self):
        server = RussoundHTTPServer(None, None)
        try:
            controller = Mock()
            controller.load_config.return_value = {"backend": {"host": "127.0.0.1", "port": 6666}}
            server.controller = controller

            with patch("web.russound_server.RussoundBackend") as backend_class:
                backend_class.return_value.start_update_listener.return_value = False
                server._start_unsolicited_update_listener()

            self.assertIs(server._event_backend, backend_class.return_value)
            controller.set_shared_backend.assert_called_once_with(backend_class.return_value)
        finally:
            _close_server(server)

    def test_backend_poll_interval_is_loaded_from_config(self):
        server = RussoundHTTPServer(None, None)
        try:
            controller = Mock()
            controller.load_config.return_value = {"backend": {"poll_interval_seconds": 4.5}}
            server.controller = controller

            self.assertEqual(server._resolve_backend_poll_interval_seconds(), 4.5)
        finally:
            _close_server(server)

    def test_backend_poll_interval_is_clamped_to_minimum(self):
        server = RussoundHTTPServer(None, None)
        try:
            controller = Mock()
            controller.load_config.return_value = {"backend": {"poll_interval_seconds": 0.25}}
            server.controller = controller

            self.assertEqual(server._resolve_backend_poll_interval_seconds(), 1.0)
        finally:
            _close_server(server)

    def test_sync_backend_state_if_changed_returns_false_when_payload_same(self):
        server = RussoundHTTPServer(None, None)
        try:
            same_state = Mock()
            same_state.to_payload.return_value = {"system_power": False, "zones": []}
            controller = Mock()
            controller.load_state.side_effect = [same_state, same_state]
            server.controller = controller

            self.assertFalse(server._sync_backend_state_if_changed())
            controller.persist_state.assert_not_called()
        finally:
            _close_server(server)

    def test_sync_backend_state_if_changed_persists_when_payload_differs(self):
        server = RussoundHTTPServer(None, None)
        try:
            current_state = Mock()
            refreshed_state = Mock()
            current_state.to_payload.return_value = {"system_power": False, "zones": []}
            refreshed_state.to_payload.return_value = {
                "system_power": True,
                "zones": [{"controller": 1, "zone": 1, "power": True}],
            }
            controller = Mock()
            controller.load_state.side_effect = [current_state, refreshed_state]
            server.controller = controller

            self.assertTrue(server._sync_backend_state_if_changed())
            controller.persist_state.assert_called_once_with(refreshed_state)
        finally:
            _close_server(server)

    def test_sync_backend_state_uses_shared_event_backend(self):
        server = RussoundHTTPServer(None, None)
        try:
            current_state = Mock()
            refreshed_state = Mock()
            current_state.to_payload.return_value = {"system_power": False, "zones": []}
            refreshed_state.to_payload.return_value = {"system_power": False, "zones": []}
            controller = Mock()
            controller.load_state.side_effect = [current_state, refreshed_state]
            shared_backend = Mock()
            server.controller = controller
            server._event_backend = shared_backend

            self.assertFalse(server._sync_backend_state_if_changed())

            self.assertEqual(
                controller.load_state.call_args_list[1].kwargs,
                {"refresh_backend": True, "backend": shared_backend},
            )
        finally:
            _close_server(server)

    def test_root_response_sets_session_cookie(self):
        server = RussoundHTTPServer(None, None)
        try:
            client = server.app.test_client()
            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            set_cookie_header = response.headers.get("Set-Cookie", "")
            self.assertIn("russound_session_id=", set_cookie_header)
        finally:
            _close_server(server)

    def test_api_request_debug_log_contains_host_and_session_id(self):
        server = RussoundHTTPServer(None, None)
        try:
            client = server.app.test_client()
            with self.assertLogs("web.russound_server", level="DEBUG") as logs:
                response = client.get("/api/state")

            self.assertEqual(response.status_code, 200)
            output = "\n".join(logs.output)
            self.assertIn("API request method=GET path=/api/state", output)
            self.assertIn("host=127.0.0.1", output)
            self.assertIn("session_id=", output)
        finally:
            _close_server(server)

    def test_api_state_is_readable_without_token(self):
        server = RussoundHTTPServer(None, None)
        try:
            response = server.app.test_client().get("/api/state")
            self.assertEqual(response.status_code, 200)
        finally:
            _close_server(server)

    def test_same_origin_json_post_is_accepted(self):
        server = RussoundHTTPServer(None, None)
        try:
            client = server.app.test_client()
            for headers in (
                {"Sec-Fetch-Site": "same-origin"},
                {"Origin": "http://localhost"},
                {},
            ):
                response = client.post("/api/system/power", json={"power": "invalid"}, headers=headers)
                # 400 proves the request passed the origin guard and reached the handler.
                self.assertEqual(response.status_code, 400, headers)
        finally:
            _close_server(server)

    def test_cross_site_post_is_rejected(self):
        server = RussoundHTTPServer(None, None)
        try:
            client = server.app.test_client()
            for headers in (
                {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site"},
                {"Origin": "http://evil.example"},
                {"Origin": "null"},
            ):
                response = client.post("/api/system/power", json={"power": False}, headers=headers)
                self.assertEqual(response.status_code, 403, headers)
        finally:
            _close_server(server)

    def test_configured_allowed_origin_is_accepted(self):
        server = RussoundHTTPServer(None, None, allowed_origins=["https://music.example/"])
        try:
            response = server.app.test_client().post(
                "/api/system/power",
                json={"power": "invalid"},
                headers={"Origin": "https://music.example", "Sec-Fetch-Site": "cross-site"},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            _close_server(server)

    def test_non_json_post_is_rejected(self):
        server = RussoundHTTPServer(None, None)
        try:
            response = server.app.test_client().post(
                "/api/shortcuts/any/activate",
                data={"power": "false"},
                headers={"Sec-Fetch-Site": "same-origin"},
            )
            self.assertEqual(response.status_code, 415)
        finally:
            _close_server(server)

    def test_untrusted_host_is_rejected_when_allowlist_is_configured(self):
        server = RussoundHTTPServer(None, None, allowed_hosts=["musicpi", "192.168.1.10"])
        try:
            client = server.app.test_client()
            self.assertEqual(client.get("/api/state", headers={"Host": "attacker.example"}).status_code, 400)
            self.assertEqual(client.get("/", headers={"Host": "attacker.example:8000"}).status_code, 400)
            self.assertEqual(client.get("/api/state", headers={"Host": "musicpi:8000"}).status_code, 200)
            self.assertEqual(client.get("/api/state", headers={"Host": "192.168.1.10"}).status_code, 200)
        finally:
            _close_server(server)

    def test_index_page_is_served_without_token_placeholder(self):
        server = RussoundHTTPServer(None, None)
        try:
            response = server.app.test_client().get("/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"russound-api-token", response.data)
            response.close()
        finally:
            _close_server(server)

    def test_broadcast_state_change_increments_revision_and_notifies_clients(self):
        server = RussoundHTTPServer(None, None)
        try:
            _client_id, event_queue = server.register_event_client("127.0.0.1", "test-agent")

            self.assertEqual(server.state_revision, 0)

            server.broadcast_state_change()

            self.assertEqual(server.state_revision, 1)
            event_payload = json.loads(event_queue.get_nowait())
            self.assertEqual(event_payload.get("revision"), 1)
            self.assertIsInstance(event_payload.get("payload"), dict)
        finally:
            _close_server(server)

    def test_events_stream_uses_real_sse_line_delimiters(self):
        server = RussoundHTTPServer(None, None)
        response = None
        try:
            client = server.app.test_client()
            response = client.get("/api/events", buffered=False)
            stream = iter(response.response)
            self.assertEqual(next(stream), b"retry: 2000\n: connected\n\n")

            server.broadcast_state_change()

            event_chunk = next(stream)
            self.assertIn(b"event: state-change\ndata: ", event_chunk)
            self.assertTrue(event_chunk.endswith(b"\n\n"))
        finally:
            if response is not None:
                response.close()
            _close_server(server)


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
        server = RussoundHTTPServer(None, None)
        try:
            client_id, _event_queue = server.register_event_client("127.0.0.1", "status-test")

            payload = server.build_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], client_id)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
        finally:
            _close_server(server)

    def test_status_client_payload_lists_connected_clients(self):
        server = RussoundHTTPServer(None, None)
        try:
            client_id, _event_queue = server.register_event_client("127.0.0.1", "status-test")

            payload = server.build_client_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["id"], client_id)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
        finally:
            _close_server(server)

    def test_status_client_payload_groups_streams_from_same_session(self):
        server = RussoundHTTPServer(None, None)
        try:
            server.register_event_client("127.0.0.1", "status-test", "session-123")
            server.register_event_client("127.0.0.1", "status-test", "session-123")

            payload = server.build_client_status_payload()

            self.assertEqual(len(payload["connected_clients"]), 1)
            self.assertEqual(payload["connected_clients"][0]["ip"], "127.0.0.1")
            self.assertEqual(payload["connected_clients"][0]["streams"], 2)
        finally:
            _close_server(server)

    def test_every_tab_of_the_same_session_receives_broadcasts(self):
        server = RussoundHTTPServer(None, None)
        try:
            first_client_id, first_queue = server.register_event_client("127.0.0.1", "tab", "session-123")
            second_client_id, second_queue = server.register_event_client("127.0.0.1", "tab", "session-123")
            self.assertNotEqual(first_client_id, second_client_id)

            server.broadcast_state_change()

            self.assertEqual(json.loads(first_queue.get_nowait())["revision"], 1)
            self.assertEqual(json.loads(second_queue.get_nowait())["revision"], 1)
        finally:
            _close_server(server)

    def test_unregister_removes_stream_from_registry(self):
        server = RussoundHTTPServer(None, None)
        try:
            client_id, _ = server.register_event_client("127.0.0.1", "status-test", "session-456")
            server.unregister_event_client(client_id)

            self.assertEqual(server._event_clients, {})
            self.assertEqual(server.build_client_status_payload()["connected_clients"], [])
            self.assertFalse(server._has_active_event_clients())
        finally:
            _close_server(server)

    def test_event_stream_limit_rejects_additional_streams_with_503(self):
        server = RussoundHTTPServer(None, None, max_event_streams=1)
        try:
            self.assertIsNotNone(server.register_event_client("127.0.0.1", "first"))
            response = server.app.test_client().get("/api/events")

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers.get("Retry-After"), "30")
            self.assertEqual(len(server._event_clients), 1)
        finally:
            _close_server(server)

    def test_event_stream_ends_after_max_lifetime_and_unregisters(self):
        server = RussoundHTTPServer(None, None)
        server._event_stream_max_seconds = 0
        response = None
        try:
            response = server.app.test_client().get("/api/events", buffered=False)
            chunks = list(response.response)

            self.assertEqual(chunks, [b"retry: 2000\n: connected\n\n"])
            self.assertEqual(server._event_clients, {})
        finally:
            if response is not None:
                response.close()
            _close_server(server)

    def test_run_derives_stream_limit_from_thread_count(self):
        server = RussoundHTTPServer(None, None, max_event_streams=100)
        try:
            with patch.object(server, "_start_backend_change_watcher"), patch(
                "web.russound_server.importlib.import_module"
            ) as import_module:
                server.run("127.0.0.1", 8000, waitress_threads=10, tcp_user_timeout_seconds=30)

            self.assertEqual(server._max_event_streams, 6)
            create_server = import_module.return_value.create_server
            self.assertEqual(create_server.call_args.kwargs["threads"], 10)
            waitress_server = create_server.return_value
            self.assertIn((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), waitress_server.adj.socket_options)
            waitress_server.run.assert_called_once_with()
        finally:
            _close_server(server)

    def test_connection_socket_options_include_tcp_user_timeout_when_supported(self):
        options = _connection_socket_options(30)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            self.assertIn((socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 30000), options)
        else:
            self.assertEqual(options, [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
        self.assertEqual(_connection_socket_options(0), [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])

    def test_status_history_payload_keeps_last_fifty_frontend_events(self):
        server = RussoundHTTPServer(None, None)
        try:
            for index in range(55):
                server.record_frontend_event("127.0.0.1", f"/api/test/{index}", {"index": index})

            payload = server.build_history_status_payload()

            self.assertEqual(len(payload["recent_events"]), 50)
            self.assertEqual(payload["recent_events"][0]["payload"], {"index": 54})
            self.assertEqual(payload["recent_events"][-1]["payload"], {"index": 5})
        finally:
            _close_server(server)

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