from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from test_playbackstatus import status

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    agent = importlib.import_module("lib.playbackagent")

TOKEN = "test-service-token-" + "x" * 32


class FakeCollector:
    def __init__(self, revision="a" * 64):
        self.config_revision = revision
        self.outputs = (status.OutputObservation("front", "Front stream", "Stereo Front", -7.1),)
        self.refresh = Mock()
        self.close = Mock()

    def snapshot(self):
        return self.outputs


class PlaybackAgentTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.configuration_snapshot.return_value = ({}, (), "a" * 64)
        self.observer = agent.AgentObserver("unused.json", backend=self.backend)
        self.addCleanup(self.observer.close)
        self.collector = FakeCollector()
        patcher = patch.object(agent, "PlaybackCollector", return_value=self.collector)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.app = agent.create_app(self.observer, "musicpi", TOKEN)
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer " + TOKEN}
        self.fixtures = json.loads((Path(__file__).parent / "fixtures/playback-read-v1.json").read_text())

    def get(self, path="/v1/outputs", **kwargs):
        return self.client.get(path, headers=self.auth, **kwargs)

    def test_all_routes_and_errors_require_authentication(self):
        for path in ("/v1/health", "/v1/capabilities", "/v1/outputs",
                     "/v1/outputs/front", "/missing"):
            for headers in ({}, {"Authorization": "Bearer wrong"},
                            {"Authorization": "Bearer \u00e9"}):
                response = self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json, self.fixtures["unauthorized"])
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.backend.assert_not_called()
        self.backend.configuration_snapshot.assert_not_called()

    def test_liveness_and_capabilities_match_fixtures_without_config(self):
        for path, fixture in (("/v1/health", "health"), ("/v1/capabilities", "capabilities")):
            value = self.get(path).json
            expected = dict(self.fixtures[fixture], epoch=value["epoch"])
            self.assertEqual(value, expected)
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.get().json, self.fixtures["snapshot_unavailable"])
        self.assertEqual(self.get("/v1/outputs/front").status_code, 503)

    def test_epoch_is_distinct_for_new_app(self):
        other = agent.create_app(self.observer, "musicpi", TOKEN).test_client()
        self.assertNotEqual(self.get("/v1/health").json["epoch"],
                            other.get("/v1/health", headers=self.auth).json["epoch"])

    def test_identity_and_token_are_required(self):
        for identity, token in (("MusicPi", TOKEN), ("x/y", TOKEN), ("musicpi", ""),
                                ("musicpi", "x" * 32 + "\n"), ("musicpi", "\u00e9" * 32)):
            with self.assertRaises(ValueError):
                agent.create_app(self.observer, identity, token)

    def test_cold_inventory_and_single_output_are_cached(self):
        self.observer.tick()
        self.backend.configuration_snapshot.reset_mock()
        self.collector.refresh.reset_mock()
        inventory = self.get().json
        output = self.get("/v1/outputs/front").json
        self.assertEqual(output["output"], inventory["outputs"][0])
        self.assertEqual(output["revision"], inventory["revision"])
        self.assertEqual(inventory["outputs"][0]["playback"], self.fixtures["pending_probe"])
        self.assertEqual(self.get("/v1/outputs/absent").json, self.fixtures["not_found"])
        self.assertEqual(self.get("/v1/outputs/absent").status_code, 404)
        self.backend.configuration_snapshot.assert_not_called()
        self.collector.refresh.assert_not_called()

    def test_head_matches_get_without_body(self):
        self.observer.tick()
        for path in ("/v1/health", "/v1/capabilities", "/v1/outputs", "/v1/outputs/front",
                     "/v1/outputs/absent", "/unknown"):
            head = self.client.head(path, headers=self.auth)
            get = self.get(path)
            self.assertEqual(head.status_code, get.status_code)
            self.assertEqual(head.data, b"")
            self.assertEqual(head.headers["Content-Type"], get.headers["Content-Type"])
            self.assertEqual(head.headers["Cache-Control"], "no-store")

    def test_malformed_and_mutation_requests_return_json(self):
        for path in ("/v1/health?q=x", "/v1/outputs/UPPER", "/v1/outputs/x%20y"):
            self.observer.tick()
            self.assertEqual(self.get(path).status_code, 400)
        self.assertEqual(self.get(data="body").status_code, 400)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            response = self.client.open("/v1/outputs", method=method, headers=self.auth)
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response.json, self.fixtures["method_not_allowed"])
            self.assertEqual(response.headers["Allow"], "GET, HEAD")
        self.assertEqual(self.get("/not-found").json["error"]["code"], "route_not_found")

    def test_snapshot_staleness_uses_monotonic_age(self):
        with patch.object(agent.time, "monotonic", return_value=100):
            self.observer.tick()
        with patch.object(agent.time, "monotonic", return_value=109.999):
            self.assertFalse(self.get().json["stale"])
        with patch.object(agent.time, "monotonic", return_value=110):
            result = self.get().json
        self.assertTrue(result["stale"])
        self.assertEqual(result["snapshot_age_ms"], 10000)

    def test_invalid_config_removes_old_inventory_and_recovers(self):
        self.observer.tick()
        self.backend.configuration_snapshot.side_effect = ValueError("secret config path")
        self.observer._config_due = 0
        self.observer.tick()
        result = self.get()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json, self.fixtures["configuration_invalid"])
        self.assertNotIn("secret", result.text)
        self.collector.close.assert_called_once()
        self.assertEqual(self.get("/v1/health").status_code, 200)
        self.backend.configuration_snapshot.side_effect = None
        self.observer._config_due = 0
        self.observer.tick()
        self.assertEqual(self.get().status_code, 200)

    def test_config_failure_backoff_is_bounded(self):
        self.backend.configuration_snapshot.side_effect = agent.BackendError("offline")
        with patch.object(agent.time, "monotonic", return_value=100):
            self.observer.tick()
            self.assertEqual(self.observer._config_due, 102)
            self.observer.tick()
            self.assertEqual(self.backend.configuration_snapshot.call_count, 1)
            for _ in range(8):
                self.observer._config_due = 0
                self.observer.tick()
            self.assertEqual(self.observer._config_due, 130)

    def test_replacement_published_before_old_collector_shutdown(self):
        self.observer.tick()
        replacement = FakeCollector("b" * 64)
        replacement.outputs = (replace(replacement.outputs[0], label="Changed"),)
        self.factory.return_value = replacement
        self.backend.configuration_snapshot.return_value = ({}, (), "b" * 64)
        self.collector.close.side_effect = lambda: self.assertEqual(
            self.get().json["outputs"][0]["label"], "Changed")
        self.observer._config_due = 0
        self.observer.tick()
        self.assertEqual(self.get().json["config_revision"], "b" * 64)

    def test_partial_errors_do_not_hide_healthy_outputs(self):
        self.observer.tick()
        healthy = status.Playback(
            "play", "1", 1, 1234.5, None, 128, "44100:24:2", None,
            status.NowPlaying("Example track", None, None, "Stereo Front"),
        )
        first = replace(self.collector.outputs[0], playback=status.Probe(
            "ok", "2026-10-14T18:50:00Z",
            status.TimedValue("2026-10-14T18:50:00Z", time.monotonic(), healthy)))
        second = replace(first, id="rear", playback=status.Probe(
            "error", error=status.ObservationError("mpd_unavailable", "MPD is unreachable.")))
        self.collector.outputs = (first, second)
        self.observer.tick()
        result = self.get()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["outputs"][0]["playback"]["value"],
                         self.fixtures["healthy_inventory"]["outputs"][0]["playback"]["value"])
        self.assertEqual(result.json["outputs"][1]["playback"]["status"], "error")

    def test_playback_failure_backoff_is_per_output_and_audio_is_separate(self):
        with patch.object(agent.time, "monotonic", return_value=100):
            self.observer.tick()
            first = self.collector.outputs[0]
            failed = replace(first, playback=status.Probe(
                "error", attempted_at="first",
                error=status.ObservationError("mpd_unavailable", "MPD is unreachable.")))
            self.collector.outputs = (failed, replace(first, id="rear"))
            self.observer.tick()
            self.assertEqual(self.observer._due["front"], 102)
            self.collector.refresh.reset_mock()
            self.observer._due["rear"] = 0
            self.observer.tick()
            self.collector.refresh.assert_called_once_with(output_ids=("rear",))
            for number in range(7):
                self.collector.outputs = (replace(failed, playback=replace(
                    failed.playback, attempted_at=str(number))),)
                self.observer.tick()
            self.assertEqual(self.observer._due["front"], 130)
            self.observer._audio_due = 0
            self.collector.refresh.reset_mock()
            self.observer.tick()
            self.collector.refresh.assert_called_once_with(audio=True, playback=False)

    def test_clean_shutdown_and_no_restart(self):
        self.observer.tick()
        self.observer.start()
        self.observer.close()
        self.assertFalse(self.observer._thread.is_alive())
        self.collector.close.assert_called_once()
        with self.assertRaises(RuntimeError):
            self.observer.start()

    def test_unexpected_scheduler_failure_is_explicit_and_sanitized(self):
        with patch.object(self.observer, "tick", side_effect=TypeError("private")):
            with self.assertLogs(level="ERROR") as logs:
                self.observer._run()
        self.assertNotIn("private", str(logs.output))
        self.assertEqual(self.get().status_code, 503)
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            self.observer.close()
        self.observer._failed = False

    def test_unexpected_http_failure_is_sanitized(self):
        with patch.object(self.observer, "snapshot", side_effect=TypeError("private URL")):
            with self.assertLogs(level="ERROR"):
                result = self.get()
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.json["error"]["code"], "internal_error")
        self.assertNotIn("private", result.text)

    def test_real_waitress_loopback_is_responsive_without_config(self):
        from waitress import create_server
        server = create_server(self.app, host="127.0.0.1", port=0, threads=2)
        errors = []
        def run():
            try:
                server.run()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.effective_port}/v1/health"
            with urlopen(Request(url, headers=self.auth), timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["status"], "alive")
        finally:
            server.trigger.pull_trigger(server.close)
            thread.join(timeout=5)
            server.task_dispatcher.shutdown()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_real_backend_invalid_config_is_diagnosable(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = agent.AgentObserver(Path(directory) / "missing.json")
            try:
                observer.tick()
                client = agent.create_app(observer, "musicpi", TOKEN).test_client()
                result = client.get("/v1/outputs", headers=self.auth)
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json["error"]["code"], "configuration_invalid")
            finally:
                observer.close()

    def test_real_collector_dead_mpd_and_audio_are_cached_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = json.loads((SCRIPTS / "config.json").read_text())
            for entry, identity in zip(config["streams"], ("front", "centerlfe", "rear")):
                entry.update(id=identity, label=identity)
            path.write_text(json.dumps(config))
            original = path.read_bytes()
            observer = agent.AgentObserver(path)
            client = agent.create_app(observer, "musicpi", TOKEN).test_client()
            with patch.object(agent, "PlaybackCollector", status.PlaybackCollector):
                with patch.object(agent.MPDBackend, "observation_connection",
                                  side_effect=status.BackendError("private endpoint")) as connections:
                    with patch.object(status, "audio_ready_config", side_effect=OSError("private audio")):
                        with patch.object(status, "read_sink_gain", side_effect=OSError("private mixer")):
                            try:
                                observer.tick()
                                for job in observer._collector._jobs.values():
                                    job.result(timeout=5)
                                observer.tick()
                                result = client.get("/v1/outputs", headers=self.auth)
                                self.assertEqual(result.status_code, 200)
                                for output in result.json["outputs"]:
                                    self.assertEqual(output["playback"]["status"], "error")
                                    self.assertEqual(output["audio"]["readiness"]["status"], "error")
                                    self.assertEqual(output["audio"]["gain"]["status"], "error")
                                self.assertEqual(connections.call_count, 3)
                                self.assertNotIn("private", result.text)
                                self.assertEqual(path.read_bytes(), original)
                                self.assertEqual(list(Path(directory).iterdir()), [path])
                            finally:
                                observer.close()

    def test_get_does_not_wait_for_collector_retirement(self):
        self.observer.tick()
        entered, release = threading.Event(), threading.Event()
        def retire():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test retirement timed out")
        self.collector.close.side_effect = retire
        self.factory.return_value = FakeCollector("b" * 64)
        self.backend.configuration_snapshot.return_value = ({}, (), "b" * 64)
        self.observer._config_due = 0
        thread = threading.Thread(target=self.observer.tick)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.get().json["config_revision"], "b" * 64)
            self.assertEqual(self.get("/v1/health").status_code, 200)
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_failed_replacement_is_not_published_as_old_success(self):
        self.observer.tick()
        self.backend.configuration_snapshot.return_value = ({}, (), "b" * 64)
        self.factory.side_effect = ValueError("Invalid output identity")
        self.observer._config_due = 0
        self.observer.tick()
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(self.get().json["error"]["code"], "configuration_invalid")
        self.collector.close.assert_called_once()

    def test_recovery_resets_output_backoff(self):
        with patch.object(agent.time, "monotonic", return_value=100):
            self.observer.tick()
            first = self.collector.outputs[0]
            self.collector.outputs = (replace(first, playback=status.Probe(
                "error", error=status.ObservationError("mpd_unavailable", "Failed"))),)
            self.observer.tick()
            self.observer._delays["front"] = 30
            value = status.Playback(
                "stop", None, 0, None, None, None, None, None,
                status.NowPlaying(None, None, None, None),
            )
            self.collector.outputs = (replace(first, playback=status.Probe(
                "ok", current=status.TimedValue("2026-10-15T00:00:00Z", 100, value))),)
            self.observer.tick()
            self.assertEqual(self.observer._delays["front"], 2)
            self.assertEqual(self.observer._due["front"], 102)

    def test_cli_serves_liveness_without_config_and_exits_on_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(TOKEN)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([
                sys.executable, str(SCRIPTS / "playbackagent.py"),
                str(Path(directory) / "missing.json"), "--agent-id", "musicpi",
                "--token-file", str(token_file), "--port", str(port),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with urlopen(Request(
                            f"http://127.0.0.1:{port}/v1/health", headers=self.auth,
                        ), timeout=1) as response:
                            self.assertEqual(json.load(response)["status"], "alive")
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("CLI did not become responsive")
                        time.sleep(0.05)
            finally:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    self.fail("CLI did not shut down cleanly")
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertNotIn(TOKEN, stdout + stderr)
            self.assertNotIn(str(Path(directory) / "missing.json"), stdout + stderr)


if __name__ == "__main__":
    unittest.main()
