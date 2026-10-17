import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import test_playback_status as status_tests
from web.playback_status import PlaybackCommandError
from web.russound_server import RussoundHTTPServer


class PlaybackCommandTests(unittest.TestCase):
    setUp = status_tests.PlaybackStatusTests.setUp
    create_client = status_tests.PlaybackStatusTests.create_client

    def prepare(self, *, writes=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        client = self.create_client(Path(directory.name) / "token")
        self.addCleanup(client.close)
        capabilities = dict(self.capabilities, write_operations=[
            "play", "pause", "stop", "set_gain", "set_tunein_station",
        ] if writes else [])
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            status_tests.FakeResponse(reply) for reply in
            (self.health, capabilities, self.fixture["healthy_inventory"])
        ])):
            self.assertTrue(client.poll_once())
        self.server = RussoundHTTPServer(None, None, playback_clients=[client])
        return client, self.server.app.test_client()

    def request(self, **changes):
        return {
            "action": "stop", "idempotency_key": "test-command-key-001",
            "expected_config_revision": "a" * 64,
            "expected_control_revision": "b" * 64, **changes,
        }

    def response(self, http_status=202, **changes):
        payload = {
            "api_version": 1, "agent_id": "musicpi", "operation": {
                "id": "c" * 32, "output_id": "front", "action": "stop",
                "status": "queued", "error": None, **changes,
            },
        }
        response = status_tests.FakeResponse(payload)
        response.status = http_status
        return response

    def test_capabilities_support_old_read_only_and_new_write_agents(self):
        client, browser = self.prepare()
        self.assertEqual(browser.get("/api/playback").json["agents"][0]["write_operations"],
                         ["play", "pause", "stop", "set_gain", "set_tunein_station"])
        self.assertNotIn("mock-value", json.dumps(client.snapshot()))

    def test_web_submission_is_typed_bounded_and_keeps_idempotency_fields(self):
        client, browser = self.prepare()
        _ = client
        calls = []

        class Opener:
            def open(inner, request, timeout):
                calls.append((request, timeout))
                return self.response()

        with patch("web.playback_status.build_opener", return_value=Opener()):
            response = browser.post("/api/playback/agents/musicpi/outputs/front/operations", json=self.request())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["operation"]["status"], "queued")
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8081/v1/outputs/front/operations")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), self.request())
        self.assertEqual(timeout, 2)
        self.assertEqual(request.get_header("Authorization"), "Bearer mock-value")

    def test_status_lookup_uses_registered_agent_and_fixed_path(self):
        client, browser = self.prepare()
        _ = client
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            self.response(200, status="applied"),
        ])):
            response = browser.get("/api/playback/agents/musicpi/operations/" + "c" * 32)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["operation"]["status"], "applied")
        self.assertEqual(browser.get("/api/playback/agents/unconfigured/operations/" + "c" * 32).status_code, 404)
        self.assertEqual(browser.get("/api/playback/agents/musicpi/operations/not-an-id").status_code, 400)

    def test_cross_origin_invalid_and_oversized_requests_never_reach_agent(self):
        _, browser = self.prepare()
        route = "/api/playback/agents/musicpi/outputs/front/operations"
        with patch("web.playback_status.build_opener") as opener:
            self.assertEqual(browser.post(route, json=self.request(), headers={"Origin": "http://evil.example"}).status_code, 403)
            for changes in ({"action": "shell"}, {"extra": "field"}, {"expected_control_revision": "old"},
                            {"action": "set_gain", "gain_db": True}, {"action": "set_gain", "gain_db": -10**400},
                            {"action": "set_source", "source_url": "https://radio.example/live"},
                            {"action": "set_tunein_station", "station_id": "https://radio.example", "station_name": "Radio"},
                            {"action": "set_tunein_station", "station_id": "s123", "station_name": ""}):
                response = browser.post(route, json=self.request(**changes))
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.json["error"]["message"], str)
            self.assertEqual(browser.post(route, data="{", content_type="application/json").status_code, 400)
            self.assertEqual(browser.post(route, data="x" * 3000, content_type="application/json").status_code, 400)
            opener.assert_not_called()

    def test_tunein_station_submission_forwards_only_catalog_identity(self):
        client, browser = self.prepare()
        source_url = "https://radio-user:private-pass@radio.example/live?token=private-value"
        request = self.request(
            action="set_tunein_station", station_id="s231065", station_name="Radio Example",
        )
        calls = []

        class Opener:
            def open(inner, forwarded, timeout):
                calls.append((forwarded, timeout))
                return self.response(action="set_tunein_station", requested_source_persisted=False)

        with patch("web.playback_status.build_opener", return_value=Opener()):
            response = browser.post("/api/playback/agents/musicpi/outputs/front/operations", json=request)
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("private-value", json.dumps(response.json))
        forwarded, timeout = calls[0]
        self.assertEqual(json.loads(forwarded.data), request)
        self.assertEqual(timeout, 2)

    def test_tunein_search_route_returns_normalized_station_results(self):
        _, browser = self.prepare()
        with patch.object(self.server._tunein, "search", return_value=[
            {"id": "s231065", "name": "Radio Example", "description": "News"},
        ]) as search:
            response = browser.get("/api/playback/catalogs/tunein/search?q=radio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "api_version": 1,
            "provider": "tunein",
            "stations": [{"id": "s231065", "name": "Radio Example", "description": "News"}],
        })
        search.assert_called_once_with("radio")

    def test_read_only_capability_prevents_writes(self):
        _, browser = self.prepare(writes=False)
        with patch("web.playback_status.build_opener") as opener:
            response = browser.post("/api/playback/agents/musicpi/outputs/front/operations", json=self.request())
        self.assertEqual(response.status_code, 503)
        opener.assert_not_called()

    def test_transport_failure_does_not_claim_command_failed_or_retry_with_new_key(self):
        _, browser = self.prepare()
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([URLError("offline")])):
            response = browser.post("/api/playback/agents/musicpi/outputs/front/operations", json=self.request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "operation_outcome_unknown")
        self.assertIn("same fields and idempotency key", response.json["error"]["message"])

    def test_upstream_conflicts_capacity_and_denials_remain_explicit_without_raw_errors(self):
        client, _ = self.prepare()
        for code in (400, 401, 403, 404, 409, 429, 503):
            error = HTTPError("https://private.example", code, "credential-bearing-upstream-error", None, None)
            with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([error])):
                with self.assertRaises(PlaybackCommandError) as caught:
                    client.submit_operation("front", self.request())
            self.assertEqual(caught.exception.status, code)
            self.assertNotIn("credential-bearing", caught.exception.message)

    def test_mismatched_operation_response_is_not_accepted_as_success(self):
        client, _ = self.prepare()
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            self.response(output_id="other"),
        ])):
            with self.assertRaises(PlaybackCommandError) as caught:
                client.submit_operation("front", self.request())
        self.assertEqual(caught.exception.status, 502)


class FavoritesCommandTests(unittest.TestCase):
    setUp = status_tests.PlaybackStatusTests.setUp
    create_client = status_tests.PlaybackStatusTests.create_client
    request = PlaybackCommandTests.request
    response = PlaybackCommandTests.response
    WRITES = ["play", "pause", "stop", "set_gain", "set_tunein_station",
              "select_favorite", "add_favorite", "remove_favorite", "move_favorite"]

    def favorites(self, **changes):
        return {
            "api_version": 1, "agent_id": "musicpi",
            "config_revision": "a" * 64, "control_revision": "b" * 64,
            "favorites": [
                {"key": "radio886", "name": "Radio 88.6", "type": "url", "station_id": None},
                {"key": "tunein:s8007", "name": "ORF Hitradio OE3", "type": "tunein", "station_id": "s8007"},
            ], **changes,
        }

    def prepare(self, favorites_reply=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        client = self.create_client(Path(directory.name) / "token")
        self.addCleanup(client.close)
        capabilities = dict(self.capabilities, write_operations=self.WRITES)
        reply = favorites_reply if favorites_reply is not None else status_tests.FakeResponse(self.favorites())
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            status_tests.FakeResponse(self.health), status_tests.FakeResponse(capabilities),
            status_tests.FakeResponse(self.fixture["healthy_inventory"]), reply,
        ])):
            ok = client.poll_once()
        self.server = RussoundHTTPServer(None, None, playback_clients=[client])
        return ok, client, self.server.app.test_client()

    def test_favorites_are_polled_without_stream_urls(self):
        ok, _, browser = self.prepare()
        self.assertTrue(ok)
        agent = browser.get("/api/playback").json["agents"][0]
        favorites = agent["snapshot"]["favorites"]
        self.assertEqual(favorites["control_revision"], "b" * 64)
        self.assertEqual([item["key"] for item in favorites["stations"]], ["radio886", "tunein:s8007"])
        self.assertIn("select_favorite", agent["write_operations"])

    def test_favorites_unavailable_before_first_agent_observation(self):
        error = HTTPError("http://127.0.0.1:8081/v1/favorites", 503, "pending", None, None)
        ok, client, _ = self.prepare(error)
        self.assertTrue(ok)
        self.assertIsNone(client.snapshot()["agents"][0]["snapshot"]["favorites"])

    def test_invalid_favorites_reply_marks_agent_unavailable(self):
        for changes in ({"favorites": [{"key": "x", "name": "X", "type": "url", "station_id": None,
                                        "url": "https://private.example/stream"}]},
                        {"favorites": [{"key": "Bad Key", "name": "X", "type": "url", "station_id": None}]},
                        {"control_revision": "old"}):
            with self.subTest(changes=changes):
                ok, _, _ = self.prepare(status_tests.FakeResponse(self.favorites(**changes)))
                self.assertFalse(ok)

    def test_favorite_operations_are_validated_and_forwarded(self):
        _, _, browser = self.prepare()
        route = "/api/playback/agents/musicpi/favorites/operations"
        calls = []

        class Opener:
            def open(inner, request, timeout):
                calls.append(request)
                return self.response(output_id=None, action=json.loads(request.data)["action"])

        valid = (
            {"action": "add_favorite", "station_id": "s8235", "station_name": "ORF Radio FM4"},
            {"action": "remove_favorite", "key": "radio886"},
            {"action": "move_favorite", "key": "tunein:s8007", "index": 0},
        )
        with patch("web.playback_status.build_opener", return_value=Opener()):
            for changes in valid:
                self.assertEqual(browser.post(route, json=self.request(**changes)).status_code, 202)
        self.assertEqual([request.full_url for request in calls],
                         ["http://127.0.0.1:8081/v1/favorites/operations"] * 3)
        with patch("web.playback_status.build_opener") as opener:
            for changes in ({"action": "stop"}, {"action": "select_favorite", "key": "radio886"},
                            {"action": "move_favorite", "key": "radio886", "index": 20},
                            {"action": "move_favorite", "key": "radio886", "index": True},
                            {"action": "remove_favorite", "key": "../etc"},
                            {"action": "add_favorite", "station_id": "https://x", "station_name": "X"}):
                self.assertEqual(browser.post(route, json=self.request(**changes)).status_code, 400, changes)
            self.assertEqual(browser.post(route, json=self.request(action="remove_favorite", key="radio886"),
                                          headers={"Origin": "http://evil.example"}).status_code, 403)
            opener.assert_not_called()

    def test_select_favorite_is_an_output_operation(self):
        _, _, browser = self.prepare()
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            self.response(action="select_favorite"),
        ])):
            response = browser.post("/api/playback/agents/musicpi/outputs/front/operations",
                                    json=self.request(action="select_favorite", key="tunein:s8007"))
        self.assertEqual(response.status_code, 202)

    def test_favorite_operation_must_not_target_an_output(self):
        _, client, _ = self.prepare()
        with patch("web.playback_status.build_opener", return_value=status_tests.FakeOpener([
            self.response(action="remove_favorite"),
        ])):
            with self.assertRaises(PlaybackCommandError) as caught:
                client.submit_favorites_operation(self.request(action="remove_favorite", key="radio886"))
        self.assertEqual(caught.exception.status, 502)
