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
        capabilities = dict(self.capabilities, write_operations=["play", "pause", "stop", "set_gain"] if writes else [])
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
                         ["play", "pause", "stop", "set_gain"])
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
                            {"action": "set_gain", "gain_db": True}, {"action": "set_gain", "gain_db": -10**400}):
                response = browser.post(route, json=self.request(**changes))
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.json["error"]["message"], str)
            self.assertEqual(browser.post(route, data="{", content_type="application/json").status_code, 400)
            self.assertEqual(browser.post(route, data="x" * 3000, content_type="application/json").status_code, 400)
            opener.assert_not_called()

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
