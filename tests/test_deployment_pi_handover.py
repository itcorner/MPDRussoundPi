import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_deployment_build import fixture_bundle
from test_deployment_native import selection_fixture, spec_fixture
from tool.deployment_compose import ComposeDeployment, encoded, paths
from tool.deployment_load import select_bundle
from tool.deployment_native_source import NativeError
from tool.deployment_pi_handover import prepare


class PiHandoverPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-handover-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.storage = paths(self.root)
        self.native = spec_fixture(self.root)
        self.source = selection_fixture()
        bundle, checksum, _ = fixture_bundle(self.root, "managed")
        self.images = select_bundle(bundle, checksum, "managed", "linux/arm64")
        self.compose_path = self.root / "compose.json"

    def prepare(self):
        return prepare(self.native, self.source, self.images, project="fixture",
                       compose_path=str(self.compose_path))

    def test_prepared_layout_passes_existing_compose_contract_without_mutation(self):
        original = copy.deepcopy((self.native, self.source, self.images))
        spec, config = self.prepare()
        self.assertEqual((self.native, self.source, self.images), original)
        self.assertEqual(json.loads(config["services"]["russound-web"]["environment"][
            "RUSSOUND_PLAYBACK_AGENTS"]), self.native["playback_agents"])
        self.assertEqual(config["services"]["russound-ser2net"]["environment"]["RUSSOUND_SERIAL_OPTIONS"],
                         self.native["serial_options"])
        self.assertEqual(config["services"]["russound-web"]["environment"]["RUSSOUND_DEBUG"], "false")
        self.assertFalse(any(self.storage["assets"].iterdir()))
        self.compose_path.write_bytes(encoded(config))
        self.compose_path.chmod(0o600)
        with patch("tool.deployment_compose.local_engine", return_value="default"):
            target = ComposeDeployment(spec, self.images, self.storage)
        self.assertEqual(target.config, config)
        self.assertFalse((self.storage["state"] / "reservation").exists())

    def test_rejects_different_source_platform_and_gateway(self):
        cases = (
            ("revision", "b" * 40),
            ("platform", "linux/amd64"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self.images["manifest"][field] = value
                with self.assertRaisesRegex(NativeError, "source revision and host platform"):
                    self.prepare()
                self.images["manifest"][field] = {"revision": "a" * 40, "platform": "linux/arm64"}[field]
        bundle, checksum, _ = fixture_bundle(self.root, "external")
        self.images = select_bundle(bundle, checksum, "external", "linux/arm64")
        with self.assertRaisesRegex(NativeError, "source revision and host platform"):
            self.prepare()

    def test_rejects_secret_path_outside_existing_bind(self):
        self.native["playback_agents"][0]["token_file"] = "/tmp/token"
        with self.assertRaisesRegex(NativeError, "protected secrets bind"):
            self.prepare()
        self.assertFalse(any(self.storage["assets"].iterdir()))

    def test_rejects_loopback_agent_and_compose_interpolation(self):
        for url in ("https://127.0.0.1:8001", "https://localhost:8001", "https://[::1]:8001"):
            self.native["playback_agents"][0]["url"] = url
            with self.subTest(url=url), self.assertRaisesRegex(NativeError, "Loopback playback"):
                self.prepare()
        self.native["playback_agents"][0]["url"] = "https://192.0.2.1:8443"
        self.native["playback_agents"][0]["token_file"] = "/run/secrets/${TOKEN}"
        with self.assertRaisesRegex(NativeError, "interpolation"):
            self.prepare()

    def test_invalid_project_and_compose_path_are_rejected(self):
        for project, path in (("../invalid", str(self.compose_path)), ("fixture", "relative.json")):
            with self.subTest(project=project, path=path), self.assertRaises(ValueError):
                prepare(self.native, self.source, self.images, project=project, compose_path=path)

    @unittest.skipUnless(shutil.which("docker") and os.environ.get("RUSSOUND_RUN_CONTAINER_TESTS") == "1",
                         "opt in with RUSSOUND_RUN_CONTAINER_TESTS=1 and Docker Compose")
    def test_prepared_layout_is_accepted_by_compose(self):
        _, config = self.prepare()
        self.compose_path.write_bytes(encoded(config))
        self.compose_path.chmod(0o600)
        result = subprocess.run(
            ["docker", "compose", "--env-file", "/dev/null", "--project-name", "fixture",
             "-f", str(self.compose_path), "config", "--quiet"],
            capture_output=True, text=True, timeout=30,
            env={**{key: value for key, value in os.environ.items()
                    if not key.startswith(("COMPOSE_", "DOCKER_", "RUSSOUND_"))},
                 "COMPOSE_DISABLE_ENV_FILE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
