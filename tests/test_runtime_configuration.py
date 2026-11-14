import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mpdrussoundpi.russound.lib.runtimelogging import SafeFormatter, configure_console
from mpdrussoundpi.russound.lib.servicecredentials import read_service_token, validate_service_token
from web.config_types import backend_endpoint_metadata, coerce_russound_config, env_port, resolve_backend_endpoint
from web.playback_status import PlaybackAgentConfig, PlaybackStatusClient
from web.russound_server import _configure_logging, main


class RuntimeConfigurationTests(unittest.TestCase):
    def test_missing_and_partial_saved_endpoints_report_actual_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(backend_endpoint_metadata(None)["configured"])
            for backend in ({}, {"host": "inactive.local"}, {"port": 7000}, {"device": "/private/device"}):
                metadata = backend_endpoint_metadata(coerce_russound_config({"backend": backend}))
                self.assertEqual(metadata["host"], {"value": "127.0.0.1", "source": "default", "overridden": False})
                self.assertEqual(metadata["port"], {"value": 6666, "source": "default", "overridden": False})

    def test_partial_overrides_and_sources_share_runtime_resolution(self):
        for backend in ({"host": "saved.local", "port": 6666}, {"device": "/private/device", "baud": 19200}):
            for variables in ({}, {"RUSSOUND_BACKEND_HOST": "gateway.local"},
                              {"RUSSOUND_BACKEND_PORT": "7000"},
                              {"RUSSOUND_BACKEND_HOST": "gateway.local", "RUSSOUND_BACKEND_PORT": "1"},
                              {"RUSSOUND_BACKEND_PORT": "65535"},
                              {"RUSSOUND_BACKEND_HOST": " ", "RUSSOUND_BACKEND_PORT": ""}):
                with self.subTest(backend=backend, variables=variables), patch.dict(os.environ, variables, clear=True):
                    config = coerce_russound_config({"backend": backend})
                    original = json.dumps(config)
                    endpoint = resolve_backend_endpoint(config)
                    metadata = backend_endpoint_metadata(config)
                    self.assertEqual(metadata["host"]["value"], endpoint.host)
                    self.assertEqual(metadata["port"]["value"], endpoint.port)
                    self.assertEqual(metadata["mode"]["value"], "serial" if endpoint.device else "tcp")
                    self.assertEqual(metadata["host"]["overridden"], bool(variables.get("RUSSOUND_BACKEND_HOST", "").strip()))
                    self.assertEqual(original, json.dumps(config))
                    self.assertNotIn("/private", json.dumps(metadata))

    def test_invalid_nonblank_port_fails_without_exposing_value(self):
        for value in ("0", "65536", "bad-secret", "1.5", "True", "-1", "1_000", "9" * 10000):
            with self.subTest(value=value[:10]), patch.dict(os.environ, {"RUSSOUND_BACKEND_PORT": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "RUSSOUND_BACKEND_PORT must"):
                    resolve_backend_endpoint(None)

    def test_cli_wins_over_invalid_environment_and_can_disable_debug(self):
        with patch.dict(os.environ, {"RUSSOUND_WEB_PORT": "bad", "RUSSOUND_WAITRESS_THREADS": "bad",
                                    "RUSSOUND_DEBUG": "bad"}, clear=True), patch.object(
            sys, "argv", ["web", "--port", "8001", "--waitress-threads", "12", "--no-debug"]
        ), patch("web.russound_server.RussoundHTTPServer") as server, patch(
            "web.russound_server._configure_logging"
        ) as logs:
            main()
            self.assertEqual(server.return_value.run.call_args.kwargs["port"], 8001)
            logs.assert_called_once_with(False)

    def test_invalid_runtime_values_fail_before_server_creation(self):
        for name, value in (("RUSSOUND_WEB_PORT", "0"), ("RUSSOUND_DEBUG", "maybe"),
                            ("RUSSOUND_WAITRESS_THREADS", "4"), ("RUSSOUND_MAX_EVENT_STREAMS", "500"),
                            ("RUSSOUND_TCP_USER_TIMEOUT_SECONDS", "-1"),
                            ("RUSSOUND_TCP_USER_TIMEOUT_SECONDS", "2147484"),
                            ("RUSSOUND_TRUSTED_PROXY_COUNT", "no"), ("RUSSOUND_BACKEND_PORT", "x")):
            with self.subTest(name=name), patch.dict(os.environ, {name: value}, clear=True), patch.object(
                sys, "argv", ["web"]
            ), patch("web.russound_server.RussoundHTTPServer") as server, patch(
                "web.russound_server._configure_logging"
            ) as logs, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    main()
                server.assert_not_called()
                logs.assert_not_called()

    def test_tcp_timeout_exact_socket_integer_boundaries(self):
        for seconds in (0, 2147483):
            with self.subTest(seconds=seconds), patch.dict(
                os.environ, {"RUSSOUND_TCP_USER_TIMEOUT_SECONDS": str(seconds)}, clear=True
            ), patch.object(sys, "argv", ["web"]), patch(
                "web.russound_server.RussoundHTTPServer"
            ) as server, patch("web.russound_server._configure_logging"), patch("sys.stdout", new_callable=io.StringIO):
                main()
                self.assertEqual(server.return_value.run.call_args.kwargs["tcp_user_timeout_seconds"], seconds)

    def test_token_consumers_agree_on_exact_bounds_and_newline_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "private-token"
            config = PlaybackAgentConfig("http://127.0.0.1:8081", "fixture", str(token))
            for size in (32, 4096):
                for ending in (b"", b"\n", b"\r\n"):
                    token.write_bytes(b"x" * size + ending)
                    self.assertEqual(read_service_token(token), "x" * size)
                    self.assertEqual(PlaybackStatusClient(config)._token, "x" * size)
                    validate_service_token("x" * size)
            for value in (b"x" * 31, b"x" * 4097, b"x" * 32 + b"\n\n",
                          b" " + b"x" * 32, b"x" * 32 + b"\x00", b"x" * 32 + b"\x7f", b"\xff" * 32):
                token.write_bytes(value)
                for reader in (lambda: read_service_token(token), lambda: PlaybackStatusClient(config)):
                    with self.assertRaises(ValueError) as error:
                        reader()
                    self.assertNotIn(directory, str(error.exception))

    def test_production_filtering_redaction_and_web_rotation(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        health = logging.getLogger("russound.health")
        previous_health = health.level
        logger_names = ("web", "web.russound_backend", "web.russound_connector", "web.russound_controller",
                        "web.russound_state", "web.zone", "web.russound_server", "russound.russound")
        previous_levels = {name: logging.getLogger(name).level for name in logger_names}
        try:
            root.handlers = []
            stream = io.StringIO()
            configure_console(False, stream=stream)
            logging.info("routine-noise")
            health.info("health-transition")
            logging.warning("https://user:password@host/stream?token=hidden Bearer private-token session_id=private-session")
            logging.warning('{"token": "quoted-secret", "tls_ca_file": "/private/ca.pem"}')
            text = stream.getvalue()
            self.assertNotIn("routine-noise", text)
            self.assertIn("health-transition", text)
            for secret in ("user:password", "hidden", "private-token", "private-session", "quoted-secret", "/private/ca.pem"):
                self.assertNotIn(secret, text)
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"RUSSOUND_LOG_FILE": str(Path(directory) / "web.log")}
            ), patch("sys.stdout", new_callable=io.StringIO):
                _configure_logging(True)
                logging.debug("debug-marker token_file=/private/credential")
                file_handler = next(h for h in root.handlers if hasattr(h, "maxBytes"))
                self.assertEqual((file_handler.maxBytes, file_handler.backupCount), (10 * 1024 * 1024, 5))
                file_handler.flush()
                text = (Path(directory) / "web.log").read_text()
                self.assertIn("debug-marker", text)
                self.assertNotIn("/private/credential", text)
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = handlers
            root.setLevel(level)
            health.setLevel(previous_health)
            for name, previous_level in previous_levels.items():
                logging.getLogger(name).setLevel(previous_level)


if __name__ == "__main__":
    unittest.main()
