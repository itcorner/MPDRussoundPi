from __future__ import annotations

import json
import os
from collections.abc import Iterator
from http.client import HTTPMessage
from pathlib import Path
import tempfile
import threading
import time
import unittest
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch
from unittest.mock import Mock
from werkzeug.test import TestResponse

from web.playback_status import NoRedirectHandler, PlaybackAgentConfig, PlaybackStatusClient
from web.russound_server import EVENT_QUEUE_MAXSIZE, RussoundHTTPServer


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "playback-read-v1.json"


class FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        _ = limit
        return self.payload


class FakeOpener:
    def __init__(self, responses: list[Any], request_headers: list[tuple[str, str | None, float]] | None = None) -> None:
        self.responses = iter(responses)
        self.request_headers = request_headers if request_headers is not None else []

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.request_headers.append((request.full_url, request.get_header("Authorization"), timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class BlockingOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.entered = threading.Event()
        self.release = threading.Event()

    def open(self, request: Any, timeout: float) -> FakeResponse:
        _ = request
        self.entered.set()
        if not self.release.wait(timeout * 2):
            raise TimeoutError
        return self.response


class PlaybackStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.health: dict[str, Any] = {
            "api_version": 1,
            "agent_id": "musicpi",
            "epoch": "agent-1",
            "status": "alive",
        }
        self.capabilities: dict[str, Any] = {
            "api_version": 1,
            "agent_id": "musicpi",
            "epoch": "agent-1",
            "read_features": ["inventory", "playback"],
            "write_operations": [],
        }

    def create_client(self, token_path: Path) -> PlaybackStatusClient:
        token_path.write_text("mock-value\n", encoding="utf-8")
        config = PlaybackAgentConfig("http://127.0.0.1:8081", "musicpi", str(token_path))
        return PlaybackStatusClient(config)

    def test_configuration_requires_all_values_and_restricts_plain_http(self) -> None:
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": "not-json"}, clear=True):
            with self.assertRaisesRegex(ValueError, "JSON array"):
                PlaybackAgentConfig.from_environment()
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": json.dumps([
            {"agent_id": "musicpi", "url": "http://127.0.0.1:8081", "token_file": "/tmp/one"},
            {"agent_id": "musicpi", "url": "https://audio.example", "token_file": "/tmp/two"},
        ])}, clear=True):
            with self.assertRaisesRegex(ValueError, "unique"):
                PlaybackAgentConfig.from_environment()
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": json.dumps([
            {"agent_id": "musicpi", "url": "http://127.0.0.1:8081", "token_file": "/tmp/one"},
            {"agent_id": "second-pi", "url": "https://audio.example", "token_file": "/tmp/two"},
        ])}, clear=True):
            configurations = PlaybackAgentConfig.from_environment()
            assert configurations is not None
            self.assertEqual(len(configurations), 2)
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": json.dumps([
            {
                "agent_id": "musicpi",
                "url": "https://audio.example",
                "token_file": "/tmp/token",
                "tls_ca_file": "/run/secrets/playback-agent.crt",
            },
        ])}, clear=True):
            configurations = PlaybackAgentConfig.from_environment()
            assert configurations is not None
            self.assertEqual(configurations[0].tls_ca_file, "/run/secrets/playback-agent.crt")
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": json.dumps([
            {
                "agent_id": "musicpi",
                "url": "http://127.0.0.1:8081",
                "token_file": "/tmp/token",
                "tls_ca_file": "/run/secrets/playback-agent.crt",
            },
        ])}, clear=True):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                PlaybackAgentConfig.from_environment()
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": json.dumps([
            {
                "agent_id": "musicpi",
                "url": "https://audio.example",
                "token_file": "/tmp/token",
                "unexpected": "value",
            },
        ])}, clear=True):
            with self.assertRaisesRegex(ValueError, "tls_ca_file is optional"):
                PlaybackAgentConfig.from_environment()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            PlaybackAgentConfig("http://example.com", "musicpi", "/unused")
        self.assertEqual(
            PlaybackAgentConfig("http://localhost:8081/", "musicpi", "/unused").base_url,
            "http://localhost:8081",
        )

    def test_client_loads_configured_tls_ca_for_https(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("mock-value\n", encoding="utf-8")
            ca_path = Path(directory) / "agent-ca.pem"
            ca_path.write_text("test certificate placeholder", encoding="ascii")
            config = PlaybackAgentConfig(
                "https://audio.example",
                "musicpi",
                str(token_path),
                str(ca_path),
            )
            with patch("web.playback_status.ssl.create_default_context") as create_context:
                context = create_context.return_value
                client = PlaybackStatusClient(config)
            create_context.assert_called_once_with(cafile=str(ca_path))
            with patch("web.playback_status.HTTPSHandler") as https_handler:
                with patch("web.playback_status.build_opener", return_value=FakeOpener([
                    FakeResponse(self.health),
                    FakeResponse(self.capabilities),
                    FakeResponse(self.fixture["healthy_inventory"]),
                ])) as build_opener_mock:
                    self.assertTrue(client.poll_once())
            self.assertEqual(https_handler.call_count, 3)
            for call in https_handler.call_args_list:
                self.assertEqual(call.kwargs["context"], context)
            self.assertEqual(len(build_opener_mock.call_args.args), 2)

    def test_agent_bearer_requests_are_not_redirected(self) -> None:
        handler = NoRedirectHandler()
        redirect = handler.redirect_request(
            Request("https://audio.example/v1/health"),
            None,
            302,
            "Found",
            HTTPMessage(),
            "https://other.example/",
        )
        self.assertIsNone(redirect)

    def test_background_client_fetches_and_caches_only_read_only_inventory(self) -> None:
        replies: list[dict[str, Any]] = [self.health, self.capabilities, self.fixture["healthy_inventory"]]
        request_headers: list[tuple[str, str | None, float]] = []
        fake_opener = FakeOpener([FakeResponse(reply) for reply in replies], request_headers)

        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            with patch("web.playback_status.build_opener", return_value=fake_opener):
                self.assertTrue(client.poll_once())
            result = client.snapshot()

        self.assertTrue(result["configured"])
        self.assertEqual(result["agents"][0]["status"], "ok")
        self.assertEqual(result["agents"][0]["snapshot"]["outputs"][0]["id"], "front")
        self.assertEqual(len(request_headers), 3)
        self.assertTrue(all(header is not None and header.startswith("Bearer ") for _, header, _ in request_headers))
        self.assertNotIn("mock-value", json.dumps(result))

    def test_client_revision_ignores_observation_age_but_tracks_playback_and_stale_changes(self) -> None:
        initial = self.fixture["healthy_inventory"]
        elapsed_only = json.loads(json.dumps(initial))
        elapsed_only["revision"] += 1
        elapsed_only["snapshot_at"] = "2026-10-14T18:50:02Z"
        elapsed_only["snapshot_age_ms"] = 0
        for output in elapsed_only["outputs"]:
            output["playback"]["attempted_at"] = "2026-10-14T18:50:02Z"
            output["playback"]["observed_at"] = "2026-10-14T18:50:02Z"
            output["playback"]["age_ms"] = 0
            output["playback"]["value"]["elapsed_seconds"] = 1236.5
            for probe in output["audio"].values():
                probe["attempted_at"] = "2026-10-14T18:50:02Z"
                probe["observed_at"] = "2026-10-14T18:50:02Z"
                probe["age_ms"] = 0
        metadata_changed = json.loads(json.dumps(elapsed_only))
        metadata_changed["revision"] += 1
        metadata_changed["outputs"][0]["playback"]["value"]["now_playing"]["title"] = "A different track"
        stale_changed = json.loads(json.dumps(metadata_changed))
        stale_changed["revision"] += 1
        stale_changed["outputs"][0]["playback"]["stale"] = True

        replies = [
            reply
            for inventory in (initial, elapsed_only, metadata_changed, stale_changed)
            for reply in (self.health, self.capabilities, inventory)
        ]
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            with patch("web.playback_status.build_opener", return_value=FakeOpener([
                FakeResponse(reply) for reply in replies
            ])):
                self.assertTrue(client.poll_once())
                self.assertEqual(client.snapshot()["revision"], 1)
                self.assertTrue(client.poll_once())
                self.assertEqual(client.snapshot()["revision"], 1)
                self.assertTrue(client.poll_once())
                self.assertEqual(client.snapshot()["revision"], 2)
                self.assertTrue(client.poll_once())
                self.assertEqual(client.snapshot()["revision"], 3)

    def test_agent_failure_preserves_last_snapshot_and_hides_upstream_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            with patch("web.playback_status.build_opener", return_value=FakeOpener([
                FakeResponse(self.health),
                FakeResponse(self.capabilities),
                FakeResponse(self.fixture["healthy_inventory"]),
            ])):
                self.assertTrue(client.poll_once())
            unauthorized = HTTPError("http://127.0.0.1/v1/health", 401, "private detail", HTTPMessage(), None)
            with patch("web.playback_status.build_opener", return_value=FakeOpener([unauthorized])):
                self.assertFalse(client.poll_once())

        agent = client.snapshot()["agents"][0]
        error = agent["error"]
        snapshot = agent["snapshot"]
        assert isinstance(error, dict)
        assert isinstance(snapshot, dict)
        self.assertEqual(agent["status"], "unavailable")
        self.assertEqual(error["code"], "agent_unauthorized")
        self.assertEqual(error["message"], "Playback agent authentication failed.")
        self.assertEqual(snapshot["outputs"][0]["id"], "front")

    def test_forbidden_agent_response_reports_http_403(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            forbidden = HTTPError(
                "https://audio.example/v1/health", 403, "forbidden", HTTPMessage(), None,
            )
            with patch("web.playback_status.build_opener", return_value=FakeOpener([forbidden])):
                self.assertFalse(client.poll_once())

        agent = client.snapshot()["agents"][0]
        self.assertEqual(agent["status"], "unavailable")
        self.assertEqual(agent["error"]["code"], "agent_forbidden")
        self.assertEqual(agent["error"]["message"], "Playback agent denied access (HTTP 403).")

    def test_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            with patch("web.playback_status.build_opener", return_value=FakeOpener([
                FakeResponse({**self.health, "agent_id": "wrong-agent"}),
            ])):
                self.assertFalse(client.poll_once())
        error = client.snapshot()["agents"][0]["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "agent_identity_mismatch")

    def test_cached_playback_routes_and_disabled_routes_do_not_require_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            with patch("web.playback_status.build_opener", return_value=FakeOpener([
                FakeResponse(self.health),
                FakeResponse(self.capabilities),
                FakeResponse(self.fixture["healthy_inventory"]),
            ])):
                self.assertTrue(client.poll_once())
            server = RussoundHTTPServer(None, None, playback_clients=[client])
            response = server.app.test_client().get("/api/playback")
            self.assertEqual(response.status_code, 200)
            inventory_value: Any = response.json
            assert isinstance(inventory_value, dict)
            inventory_payload = cast(dict[str, Any], inventory_value)
            agents_value = inventory_payload.get("agents")
            assert isinstance(agents_value, list)
            agents = cast(list[dict[str, Any]], agents_value)
            self.assertEqual(agents[0]["status"], "ok")
            output = server.app.test_client().get("/api/playback/agents/musicpi/outputs/front")
            output_value: Any = output.json
            assert isinstance(output_value, dict)
            output_payload = cast(dict[str, Any], output_value)
            output_value = output_payload.get("output")
            assert isinstance(output_value, dict)
            output_body = cast(dict[str, Any], output_value)
            self.assertEqual(output_body["label"], "Front stream")
            health_payload = server.app.test_client().get("/api/playback/health").json
            assert isinstance(health_payload, dict)
            self.assertEqual(health_payload["status"], "ok")
            client.close()

        disabled = RussoundHTTPServer(None, None)
        disabled_payload = disabled.app.test_client().get("/api/playback").json
        assert isinstance(disabled_payload, dict)
        self.assertEqual(disabled_payload, {
            "api_version": 1,
            "configured": False,
            "revision": 0,
            "agents": [],
        })
        disabled_health = disabled.app.test_client().get("/api/playback/health").json
        assert isinstance(disabled_health, dict)
        self.assertEqual(disabled_health["status"], "disabled")

    def test_multiple_browser_sessions_share_the_cached_agent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            request_headers: list[tuple[str, str | None, float]] = []
            fake_opener = FakeOpener([
                FakeResponse(self.health),
                FakeResponse(self.capabilities),
                FakeResponse(self.fixture["healthy_inventory"]),
            ], request_headers)
            with patch("web.playback_status.build_opener", return_value=fake_opener):
                self.assertTrue(client.poll_once())

            server = RussoundHTTPServer(None, None, playback_clients=[client])
            responses: list[TestResponse] = []
            for _ in range(8):
                with server.app.test_client() as browser_session:
                    responses.append(browser_session.get("/api/playback"))

            self.assertTrue(all(response.status_code == 200 for response in responses))
            revisions: set[int] = set()
            for response in responses:
                response_value: Any = response.json
                assert isinstance(response_value, dict)
                response_payload = cast(dict[str, Any], response_value)
                revision = response_payload.get("revision")
                assert isinstance(revision, int)
                revisions.add(revision)
            self.assertEqual(len(revisions), 1)
            self.assertEqual(len(request_headers), 3)
            client.close()

    def test_blocked_agent_reads_do_not_hold_russound_control_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_client(Path(directory) / "agent-token")
            opener = BlockingOpener(FakeResponse({**self.health, "agent_id": "unexpected"}))
            server = RussoundHTTPServer(None, None, playback_clients=[client])
            updated: dict[str, Any] = {"config_required": False, "state": {"zones": []}}
            server.controller = Mock()
            server.controller.build_view_payload.return_value = updated
            server.controller.handle_zone_setting_change.return_value = updated

            with patch("web.playback_status.build_opener", return_value=opener):
                poller = threading.Thread(target=client.poll_once)
                poller.start()
                self.assertTrue(opener.entered.wait(1))
                started_at = time.monotonic()
                response = server.app.test_client().post(
                    "/api/controller/1/zone/1/volume",
                    json={"volume": 40},
                )
                self.assertEqual(response.status_code, 200)
                self.assertLess(time.monotonic() - started_at, 1)
                opener.release.set()
                poller.join(timeout=3)
                self.assertFalse(poller.is_alive())

    def test_playback_sse_is_coalesced_and_client_queue_is_bounded(self) -> None:
        server = RussoundHTTPServer(None, None)
        registration = server.register_event_client("127.0.0.1", "test-agent")
        assert registration is not None
        _, event_queue = registration
        for _ in range(20):
            server.broadcast_playback_change()
        self.assertEqual(event_queue.qsize(), 1)
        playback_event = json.loads(event_queue.get_nowait())
        self.assertEqual(playback_event["event"], "playback-change")
        self.assertEqual(playback_event["revision"], 1)

        for _ in range(20):
            server.broadcast_state_change()
        self.assertLessEqual(event_queue.qsize(), EVENT_QUEUE_MAXSIZE)
        queued_events = [json.loads(event_queue.get_nowait()) for _ in range(event_queue.qsize())]
        self.assertEqual(queued_events[-1]["revision"], 20)

        response = server.app.test_client().get("/api/events", buffered=False)
        stream: Iterator[bytes] = cast(Iterator[bytes], iter(response.response))
        next(stream)
        for _ in range(20):
            server.broadcast_playback_change()
        event_chunk = next(stream).decode()
        self.assertIn("event: playback-change", event_chunk)
        self.assertEqual(json.loads(event_chunk.split("data: ", 1)[1])["revision"], 40)
        response.close()
