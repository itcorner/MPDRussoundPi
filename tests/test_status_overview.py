from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web import audio_status, status_overview as overview
from web.playback_status import PlaybackAgentConfig, PlaybackStatusClient
from web.russound_controller import backend_status_kind
from web.russound_server import RussoundHTTPServer

FIXTURES = Path(__file__).parent / "fixtures"
AUDIO = json.loads((FIXTURES / "audio-info-v1.json").read_text())
PLAYBACK = json.loads((FIXTURES / "playback-read-v1.json").read_text())
NOW = datetime(2026, 10, 14, 18, 50, 5, tzinfo=timezone.utc)


def audio_response():
    return copy.deepcopy(AUDIO["agent_response"])


def agent(status="ok"):
    return {"agent_id": "musicpi", "status": status, "last_success_at": "2026-10-14T18:50:00Z", "error": None,
            "snapshot": copy.deepcopy(PLAYBACK["healthy_inventory"]), "write_operations": ["play", "set_gain"]}


class AudioValidationTests(unittest.TestCase):
    def test_fixture_is_accepted_and_rebuilt(self):
        clean = audio_status.validate_audio_response(audio_response())
        self.assertEqual(set(clean), {"config_revision", "devices", "outputs"})

    def test_unknown_keys_and_bad_values_are_rejected(self):
        for change in (
            lambda r: r["devices"][0].update(serial="ABC"),
            lambda r: r["devices"][0]["usb"].update(vendor="0D8C"),
            lambda r: r["devices"][0]["sinks"][0].update(id="a b"),
            lambda r: r["devices"][0]["sinks"][0].update(card_channels=["fl"]),
            lambda r: r["devices"][0].update(profile="x" * 65),
            lambda r: r.update(config_revision="short"),
            lambda r: r["outputs"].append({"id": "Bad Id", "sink": "s"}),
        ):
            response = audio_response()
            change(response)
            with self.subTest(), self.assertRaises(audio_status.Invalid):
                audio_status.validate_audio_response(response)


class SectionTests(unittest.TestCase):
    def test_web_revision_wording(self):
        shown = overview.web_section(uptime_seconds=90000, runtime="container", revision="a" * 40, clients=2)
        self.assertIn("aaaaaaa (reported by the build)", [row["value"] for row in shown["rows"]])
        unknown = overview.web_section(uptime_seconds=5, runtime="native", revision="not-a-sha", clients=0)
        self.assertIn("not reported by this build", [row["value"] for row in unknown["rows"]])

    def test_certificate_levels(self):
        row = lambda days: overview.certificate_row({"status": "ok", "days_left": days, "expires_at": "2027-10-16T00:00:00Z"})
        self.assertEqual([row(400)["level"], row(10)["level"], row(-1)["level"]], ["ok", "warning", "problem"])
        self.assertIsNone(overview.certificate_row({"status": "pending"}))

    def test_agent_unavailable_is_a_problem(self):
        section = overview.agent_section(agent("unavailable"), {"certificate": {}, "read_features": []}, NOW)
        self.assertEqual(section["level"], "problem")

    def test_agent_section_lists_features_and_controls(self):
        section = overview.agent_section(agent(), {"certificate": {"status": "ok", "days_left": 300, "expires_at": "2027-10-16T00:00:00Z"},
                                                   "read_features": ["inventory", "audio_info"]}, NOW)
        values = {row["label"]: row["value"] for row in section["rows"]}
        self.assertEqual(values["Reported features"], "inventory, audio hardware")
        self.assertEqual(values["Controls"], "Manual control enabled: transport, gain")
        self.assertEqual(section["level"], "ok")

    def test_gateway_states(self):
        backend = {"mode": {"value": "tcp", "source": "environment"}, "host": {"value": "192.0.2.9", "source": "saved"}, "port": {"value": 9621, "source": "default"}}
        config = {"controllers": [{"id": 1, "zone_count": 6}], "zones": [{"controller": 1, "enabled": True}, {"controller": 1, "enabled": False}]}
        section = overview.gateway_section(config=config, effective_backend=backend, kind="silent", observed_age="3 s ago")
        self.assertEqual(section["level"], "problem")
        values = {row["label"]: row["value"] for row in section["rows"]}
        self.assertEqual(values["Controller 1"], "6 zones configured, 1 enabled")
        self.assertIn("192.0.2.9:9621", values["Endpoint"])
        self.assertEqual(overview.gateway_section(config=None, effective_backend={}, kind="unknown", observed_age=None)["level"], "warning")
        serial = overview.gateway_section(config=config, effective_backend={"mode": {"value": "serial"}}, kind="responding", observed_age=None)
        self.assertNotIn("/dev", json.dumps(serial))

    def test_audio_section_with_hardware(self):
        data = audio_status.validate_audio_response(audio_response())
        section = overview.audio_section(agent(), {"status": "ok", "data": data})
        titles = [group["title"] for group in section["groups"]]
        self.assertIn("Front stream", titles)
        self.assertIn("Sound card", titles)
        front = section["groups"][0]
        text = json.dumps(front)
        self.assertIn("Correct", text)
        self.assertEqual(front["level"], "ok")

    def test_wrong_route_and_unreachable_mpd_are_problems(self):
        item = agent()
        output = item["snapshot"]["outputs"][0]
        output["audio"]["route"]["value"].update(actual_sink="sink.other", correct=False)
        output["playback"]["value"].update(reachable=False)
        group = overview.output_group(output, None)
        self.assertEqual(group["level"], "problem")
        self.assertIn("Wrong", json.dumps(group))

    def test_missing_hardware_report_is_explained(self):
        section = overview.audio_section(agent(), {"status": "unsupported", "data": None})
        self.assertIn("update the agent", json.dumps(section))

    def test_untrusted_labels_are_cleaned(self):
        output = copy.deepcopy(agent()["snapshot"]["outputs"][0])
        output["selection"]["label"] = "Radio\x00\x1b[31m"
        text = json.dumps(overview.output_group(output, None))
        self.assertNotIn("\\u0000", text)
        self.assertNotIn("\\u001b", text)


class BackendKindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(backend_status_kind({"connected": True}), "responding")
        self.assertEqual(backend_status_kind(None), "unknown")
        self.assertEqual(backend_status_kind({"connected": False, "message": "x"}), "unknown")


class EndpointTests(unittest.TestCase):
    def test_endpoint_without_agents(self):
        server = RussoundHTTPServer(None, None)
        try:
            body = server.app.test_client().get("/api/status/overview").json
            self.assertEqual(body["api_version"], 1)
            ids = [section["id"] for section in body["sections"]]
            self.assertEqual(ids, ["web", "gateway", "agent"])
        finally:
            server.server_close()

    def test_endpoint_with_agent_never_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("mock-value-" + "x" * 32 + "\n")
            client = PlaybackStatusClient(PlaybackAgentConfig("http://127.0.0.1:8081", "musicpi", str(token)))
            client._set_audio("ok", audio=audio_status.validate_audio_response(audio_response()))
            server = RussoundHTTPServer(None, None, playback_clients=[client])
            try:
                with patch.object(client, "_get_json", side_effect=AssertionError("no probing")):
                    body = server.app.test_client().get("/api/status/overview").json
                self.assertEqual([s["id"] for s in body["sections"]], ["web", "gateway", "agent:musicpi", "audio:musicpi"])
                text = json.dumps(body)
                for forbidden in AUDIO["forbidden_text"]:
                    self.assertNotIn(forbidden, text)
            finally:
                server.server_close()


class ClientPollingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        token = Path(directory.name) / "token"
        token.write_text("mock-value-" + "x" * 32 + "\n")
        self.client = PlaybackStatusClient(PlaybackAgentConfig("http://127.0.0.1:8081", "musicpi", str(token)))
        self.features = ["inventory", "playback", "audio_info"]
        self.audio: object = dict(audio_response(), config_revision=PLAYBACK["healthy_inventory"]["config_revision"])
        self.paths: list[str] = []
        self.clock = [1000.0]
        for target, effect in (("web.playback_status.time.monotonic", lambda: self.clock[0]),):
            patcher = patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(self.client, "_get_json", side_effect=self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, path):
        self.paths.append(path)
        identity = {"api_version": 1, "agent_id": "musicpi", "epoch": "e"}
        if path == "/v1/health":
            return {**identity, "status": "alive"}
        if path == "/v1/capabilities":
            return {**identity, "read_features": self.features, "write_operations": []}
        if path == "/v1/outputs":
            return PLAYBACK["healthy_inventory"]
        if path == "/v1/audio":
            if isinstance(self.audio, Exception):
                raise self.audio
            return {**identity, **self.audio}
        raise AssertionError(path)

    def test_audio_is_fetched_when_advertised(self):
        self.client.poll_once()
        audio = self.client.overview_snapshot()["audio"]
        self.assertEqual(audio["status"], "ok")
        self.assertEqual(audio["data"]["devices"][0]["usb"], {"vendor": "0d8c", "product": "0102"})
        self.assertEqual(self.client.overview_snapshot()["read_features"], ["audio_info", "inventory", "playback"])

    def test_older_agents_are_unsupported_and_never_asked(self):
        self.features = ["inventory", "playback"]
        self.client.poll_once()
        self.assertNotIn("/v1/audio", self.paths)
        self.assertEqual(self.client.overview_snapshot()["audio"]["status"], "unsupported")

    def test_audio_is_polled_slowly_and_failures_are_contained(self):
        self.client.poll_once()
        self.client.poll_once()
        self.assertEqual(self.paths.count("/v1/audio"), 1)
        self.clock[0] += 61
        self.audio = TimeoutError()
        self.assertTrue(self.client.poll_once())
        audio = self.client.overview_snapshot()["audio"]
        self.assertEqual((audio["status"], audio["data"], audio["error"]["code"]), ("unavailable", None, "agent_timeout"))

    def test_malformed_audio_is_an_error_not_data(self):
        self.audio = dict(audio_response(), config_revision="x")
        self.client.poll_once()
        audio = self.client.overview_snapshot()["audio"]
        self.assertEqual((audio["status"], audio["data"]), ("unavailable", None))

    def test_certificate_expiry_is_read_from_the_verified_handshake(self):
        self.client._config = PlaybackAgentConfig("https://musicpi.home.arpa:8443", "musicpi", self.client._config.token_file)
        with patch("web.playback_status._peer_certificate", return_value={"notAfter": "Oct 16 00:00:00 2027 GMT"}), \
                patch("web.playback_status.time.time", return_value=datetime(2026, 11, 21, tzinfo=timezone.utc).timestamp()):
            self.client._poll_certificate()
            certificate = self.client.overview_snapshot()["certificate"]
        self.assertEqual((certificate["status"], certificate["expires_at"]), ("ok", "2027-10-16T00:00:00Z"))
        self.assertEqual(certificate["days_left"], 329)

    def test_certificate_failure_is_reported_as_unavailable(self):
        self.client._config = PlaybackAgentConfig("https://musicpi.home.arpa:8443", "musicpi", self.client._config.token_file)
        with patch("web.playback_status._peer_certificate", side_effect=OSError("boom")):
            self.client._poll_certificate()
        self.assertEqual(self.client.overview_snapshot()["certificate"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
