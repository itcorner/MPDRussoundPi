import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from urllib.request import build_opener, ProxyHandler
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
ENABLED = os.environ.get("RUSSOUND_RUN_CONTAINER_TESTS") == "1"


@unittest.skipUnless(DOCKER and ENABLED, "opt in with RUSSOUND_RUN_CONTAINER_TESTS=1 on local Docker")
class ContainerRuntimeTests(unittest.TestCase):
    def test_dummy_routing_external_mode_and_state_survive_rerun(self):
        self.assertNotIn("DOCKER_HOST", os.environ, "Run this test only against a local Docker socket")
        self.assertNotIn("DOCKER_CONTEXT", os.environ, "Select a local context explicitly before testing")
        context = json.loads(subprocess.check_output([DOCKER, "context", "inspect"], text=True))[0]
        self.assertTrue(context["Endpoints"]["docker"]["Host"].startswith(("unix://", "npipe://")),
                        "A remote Docker endpoint must not be used")
        web_ref = os.environ["RUSSOUND_TEST_WEB_IMAGE"]
        dummy_ref = os.environ["RUSSOUND_TEST_DUMMY_IMAGE"]
        web = json.loads(subprocess.check_output([DOCKER, "image", "inspect", web_ref], text=True))[0]
        dummy = json.loads(subprocess.check_output([DOCKER, "image", "inspect", dummy_ref], text=True))[0]
        self.assertIn("@sha256:", web_ref, "Select a cached published web digest, not a mutable tag")
        self.assertEqual((web["Os"], web["Architecture"]), (dummy["Os"], dummy["Architecture"]))
        project = "russound-test-" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="russound-container-") as directory:
            root = Path(directory)
            for name in ("config", "data", "logs", "secrets", "dummy-data"):
                (root / name).mkdir()
            config = {
                "backend": {"host": "russound-dummy", "port": 6666, "poll_interval_seconds": 1},
                "controllers": [{"id": 1, "zone_count": 2}],
                "zones": [{"id": "test-room", "name": "Test room", "controller": 1, "zone": 1}],
                "inputs": [{"id": 1, "name": "Test source"}],
                "notes": "preserve this operator-owned field",
                "protocol_audit": {"enabled": False},
            }
            config_path = root / "config/russound_config.json"
            original_config = json.dumps(config).encode()
            config_path.write_bytes(original_config)
            (root / "data/preserve.txt").write_text("unrelated state")
            override = root / "isolated.yml"
            override.write_text(yaml.safe_dump({
                "networks": {"default": {"internal": True}, "frontend": {}},
                "services": {
                    "russound-web": {"networks": ["default", "frontend"],
                                     "healthcheck": {"interval": "1s", "start_period": "1s"}},
                    "russound-dummy": {"healthcheck": {"interval": "1s", "start_period": "1s"}},
                },
            }))
            env = {
                **{key: value for key, value in os.environ.items()
                   if not key.startswith(("RUSSOUND_", "COMPOSE_"))},
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "RUSSOUND_WEB_IMAGE": web_ref,
                "RUSSOUND_DUMMY_IMAGE": dummy["Id"],
                "RUSSOUND_PLATFORM": f'{web["Os"]}/{web["Architecture"]}',
                "RUSSOUND_DATA_DIR": str(root),
                "RUSSOUND_UID": str(os.getuid()), "RUSSOUND_GID": str(os.getgid()),
                "RUSSOUND_PUBLISH_HOST": "127.0.0.1", "RUSSOUND_PUBLISH_PORT": "0",
                "RUSSOUND_BACKEND_HOST": "russound-dummy", "RUSSOUND_BACKEND_PORT": "6666",
            }
            base = [DOCKER, "compose", "--env-file", "/dev/null", "-p", project,
                    "-f", str(ROOT / "compose.dev.yml")]
            stack = base + ["-f", str(ROOT / "compose.dummy.yml"), "-f", str(override)]

            def run(args):
                result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=100)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout.strip()

            def wait_for_controller():
                container = run(stack + ["ps", "-q", "russound-web"])
                deadline = time.monotonic() + 40
                last_result = None
                while time.monotonic() < deadline:
                    result = run([DOCKER, "exec", container, "python", "-c",
                                  "import urllib.request; print(urllib.request.urlopen("
                                  "'http://127.0.0.1:8000/api/rooms', timeout=3).read().decode())"])
                    last_result = json.loads(result)
                    if last_result.get("backend_status", {}).get("connected"):
                        return last_result
                    time.sleep(0.5)
                self.fail(f"Dummy controller did not become ready: {last_result}")

            try:
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                self.assertFalse(wait_for_controller().get("config_required", True))
                address = run(stack + ["port", "russound-web", "8000"])
                with build_opener(ProxyHandler({})).open(f"http://{address}/api/rooms", timeout=5) as response:
                    self.assertTrue(json.load(response)["backend_status"]["connected"])
                first_id = run(stack + ["ps", "-q", "russound-web"])
                inspected = json.loads(run([DOCKER, "inspect", first_id]))[0]
                self.assertEqual(inspected["HostConfig"]["LogConfig"]["Config"],
                                 {"max-size": "10m", "max-file": "3"})
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                self.assertEqual(run(stack + ["ps", "-q", "russound-web"]), first_id)
                self.assertEqual(config_path.read_bytes(), original_config)
                self.assertEqual((root / "data/preserve.txt").read_text(), "unrelated state")
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--no-deps",
                             "--force-recreate", "--wait", "--wait-timeout", "60", "russound-web"])
                self.assertNotEqual(run(stack + ["ps", "-q", "russound-web"]), first_id)
                self.assertFalse(wait_for_controller().get("config_required", True))
                self.assertEqual(config_path.read_bytes(), original_config)
                current_id = run(stack + ["ps", "-q", "russound-web"])
                unavailable = subprocess.run(
                    stack + ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "russound-web"],
                    env={**env, "RUSSOUND_WEB_IMAGE": "itcornernet/mpdrussoundpi-web@sha256:" + "0" * 64},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(unavailable.returncode, 0)
                self.assertEqual(run(stack + ["ps", "-q", "russound-web"]), current_id)
                self.assertEqual(config_path.read_bytes(), original_config)
                # The existing dummy now acts as an external gateway on the same isolated network.
                external = root / "external.yml"
                external.write_text(yaml.safe_dump({
                    "networks": {"default": {"internal": True}, "frontend": {}},
                    "services": {"russound-web": {"networks": ["default", "frontend"],
                                                  "healthcheck": {"interval": "1s", "start_period": "1s"}}},
                }))
                external_stack = base + ["-f", str(external)]
                run(external_stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                self.assertFalse(wait_for_controller().get("config_required", True))
                self.assertEqual(config_path.read_bytes(), original_config)
                self.assertTrue((root / "dummy-data/dummy_state.json").is_file())
            finally:
                run(stack + ["down", "--timeout", "5"])
