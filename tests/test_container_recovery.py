import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

import yaml

from tool.deployment_completion import run_checked
from tool.deployment_build import image_identity, verify_bundle
from tool.deployment_images import resolve
from tool.deployment_state import StateError, StateStore
from test_deployment_images import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"
ENABLED = os.environ.get("RUSSOUND_RUN_CONTAINER_TESTS") == "1"


@unittest.skipUnless(DOCKER and ANSIBLE.is_file() and ENABLED,
                     "opt in on local Docker with RUSSOUND_RUN_CONTAINER_TESTS=1")
class ContainerRecoveryTests(unittest.TestCase):
    def test_failed_ansible_update_and_isolated_restored_stack_preserve_pending(self):
        assert DOCKER is not None
        docker = DOCKER
        self.assertNotIn("DOCKER_HOST", os.environ)
        self.assertNotIn("DOCKER_CONTEXT", os.environ)
        context = json.loads(subprocess.check_output([docker, "context", "inspect"], text=True))[0]
        self.assertTrue(context["Endpoints"]["docker"]["Host"].startswith(("unix://", "npipe://")))
        local_bundle = os.environ.get("RUSSOUND_TEST_LOCAL_BUNDLE")
        local = None
        if local_bundle:
            local = verify_bundle(
                Path(local_bundle), os.environ["RUSSOUND_TEST_LOCAL_MANIFEST_SHA256"],
                "managed", "linux/arm64")
            image = local["images"]["web"]["reference"]
        else:
            image = os.environ["RUSSOUND_TEST_WEB_IMAGE"]
            self.assertIn("@sha256:", image)
        cached = json.loads(subprocess.check_output([docker, "image", "inspect", image], text=True))[0]
        if local is not None:
            image_identity(cached, image, local["platform"], local["revision"])
        project = "russound-recovery-" + uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="russound-recovery-") as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            config = {
                "backend": {"host": "russound-dummy", "port": 6666, "poll_interval_seconds": 1},
                "controllers": [{"id": 1, "zone_count": 2}],
                "zones": [{"id": "test-room", "name": "Recovery room", "controller": 1, "zone": 1}],
                "inputs": [{"id": 1, "name": "Test source"}],
                "notes": "preserve operator unknown fields during recovery",
            }
            original_config = json.dumps(config).encode()
            override = root / "fixture.yml"
            # Test-only dummy: run current source with a cached Python-capable
            # web image instead of creating/publishing another image.
            override.write_text(yaml.safe_dump({
                "networks": {"default": {"internal": True}, "frontend": {}},
                "services": {
                    "russound-web": {
                        "networks": ["default", "frontend"],
                        **({} if local_bundle else {
                            "entrypoint": ["python", "-m", "web.russound_server"],
                            "working_dir": "/workspace",
                            "volumes": [{"type": "bind", "source": str(ROOT), "target": "/workspace",
                                         "read_only": True}],
                        }),
                        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
                        "healthcheck": {"interval": "1s", "start_period": "1s"},
                    },
                    "russound-dummy": {
                        "entrypoint": ["python", "-m", "tool.dummy_backend.dummy_backend", "--serve",
                                       "--host", "0.0.0.0", "--port", "6666",
                                       "--state", "/data/dummy_state.json"],
                        "working_dir": "/workspace",
                        "volumes": [{"type": "bind", "source": str(ROOT), "target": "/workspace",
                                     "read_only": True}],
                        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
                        "healthcheck": {
                            "test": ["CMD", "python", "-c",
                                     "import socket; socket.create_connection(('127.0.0.1',6666),3).close()"],
                            "interval": "1s", "start_period": "1s",
                        },
                    },
                },
            }))
            settings: dict[str, str] = {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "RUSSOUND_WEB_IMAGE": image, "RUSSOUND_DUMMY_IMAGE": cached["Id"],
                "RUSSOUND_PLATFORM": f'{cached["Os"]}/{cached["Architecture"]}',
                "RUSSOUND_DATA_DIR": "/not-prepared",
                "RUSSOUND_UID": str(os.getuid()), "RUSSOUND_GID": str(os.getgid()),
                "RUSSOUND_PUBLISH_HOST": "127.0.0.1", "RUSSOUND_PUBLISH_PORT": "0",
                "RUSSOUND_BACKEND_HOST": "russound-dummy", "RUSSOUND_BACKEND_PORT": "6666",
            }
            env = {**{key: value for key, value in os.environ.items()
                      if not key.startswith(("COMPOSE_", "RUSSOUND_"))}, **settings}
            stack = [docker, "compose", "--env-file", "/dev/null", "-p", project,
                     "-f", str(ROOT / "compose.dev.yml"), "-f", str(ROOT / "compose.dummy.yml"),
                     "-f", str(override)]

            def run(args: list[str]) -> str:
                result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=100)
                diagnostics = ""
                if result.returncode and args[:len(stack)] == stack and "up" in args[len(stack):]:
                    evidence = subprocess.run(
                        stack + ["logs", "--no-color", "--tail", "60"], env=env,
                        capture_output=True, text=True, timeout=20)
                    diagnostics = evidence.stdout + evidence.stderr
                    ids = subprocess.run(stack + ["ps", "-a", "-q"], env=env,
                                         capture_output=True, text=True, timeout=20)
                    if ids.returncode == 0 and ids.stdout.strip():
                        health = subprocess.run(
                            [docker, "inspect", "--format", "{{json .State.Health}}", *ids.stdout.split()],
                            env=env, capture_output=True, text=True, timeout=20)
                        diagnostics += health.stdout + health.stderr
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr + diagnostics)
                return result.stdout.strip()

            def routing():
                web = run(stack + ["ps", "-q", "russound-web"])
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    value = json.loads(run([docker, "exec", web, "python", "-c",
                                           "import urllib.request; print(urllib.request.urlopen("
                                           "'http://127.0.0.1:8000/api/rooms',timeout=3).read().decode())"]))
                    if value.get("backend_status", {}).get("connected"):
                        return
                    time.sleep(0.5)
                self.fail("Restored fixture did not establish real web-to-dummy routing")

            def stopped():
                ids = run(stack + ["ps", "-a", "-q"]).splitlines()
                self.assertEqual(len(ids), 2)
                for container in json.loads(run([docker, "inspect", *ids])):
                    self.assertFalse(container["State"]["Running"])
                    self.assertFalse(container["State"]["Restarting"])
                    self.assertFalse(container["State"]["Paused"])

            operation = str(uuid4())
            volume = project + "-data"
            helper = [
                docker, "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", str(ROOT) + ":/workspace:ro",
                "-v", volume + ":/operation", "-w", "/workspace", "--entrypoint", "python", image,
            ]
            state = StateStore(root / "state", owner_uid=os.getuid())
            state.initialize()
            try:
                run([docker, "volume", "create", "--label", "mpdrussoundpi.test=" + project, volume])
                volume_info = json.loads(run([docker, "volume", "inspect", volume]))[0]
                self.assertEqual(volume_info["Driver"], "local")
                mountpoint = volume_info["Mountpoint"]
                self.assertTrue(mountpoint.startswith("/"))
                env["RUSSOUND_DATA_DIR"] = mountpoint + "/live"
                settings["RUSSOUND_DATA_DIR"] = env["RUSSOUND_DATA_DIR"]
                # Initialize only this test's named volume. Subsequent helper
                # runs use the service identity without any added capability.
                setup_helper = [
                    docker, "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                    "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges",
                    "--user", "0:0", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-v", str(ROOT) + ":/workspace:ro", "-v", volume + ":/operation",
                    "-w", "/workspace", "--entrypoint", "python", image,
                ]
                run(setup_helper + ["-c", """
import os, sys
from pathlib import Path
uid, gid = int(sys.argv[1]), int(sys.argv[2])
root = Path('/operation')
root.chmod(0o700)
for name in ('live', 'backups', 'live/config', 'live/data', 'live/logs', 'live/secrets', 'live/dummy-data'):
    (root / name).mkdir(mode=0o700)
(root / 'live/config/russound_config.json').write_bytes(sys.argv[3].encode())
(root / 'live/data/preserved').write_bytes(b'unrelated-state')
(root / 'live/secrets/token').write_bytes(b'synthetic-recovery-token')
(root / 'live/secrets/token').chmod(0o600)
(root / 'live/dummy-data/dummy_state.json').write_bytes(
    Path('/workspace/tool/dummy_backend/example_state.json').read_bytes())
for path in sorted([root, *root.rglob('*')], key=lambda path: len(path.parts), reverse=True):
    os.chown(path, uid, gid)
""", str(os.getuid()), str(os.getgid()), original_config.decode()])
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                routing()
                baseline = run(stack + ["ps", "-q", "russound-web"])
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                self.assertEqual(run(stack + ["ps", "-q", "russound-web"]), baseline)
                # Inject an unavailable candidate solely to exercise failure,
                # not to claim successful registry verification or adoption.
                candidate = resolve("dummy", "linux/arm64", FixtureRegistry())
                candidate["platform"] = settings["RUSSOUND_PLATFORM"]
                state.begin("fixture", operation, candidate, "sha256:" + "d" * 64, ["apply", "routing"])
                state.advance("fixture", operation, "inputs-verified")
                with closing(http.client.HTTPConnection(run(stack + ["port", "russound-web", "8000"]),
                                                        timeout=5)) as events:
                    events.request("GET", "/api/events")
                    stream = events.getresponse()
                    self.assertEqual(stream.status, 200)
                    self.assertTrue(stream.readline().startswith(b"retry:"))
                    started = time.monotonic()
                    run(stack + ["stop", "--timeout", "8", "russound-web"])
                    self.assertLess(time.monotonic() - started, 6)
                    web_state = json.loads(run([docker, "inspect", baseline]))[0]["State"]
                    self.assertEqual(web_state["ExitCode"], 0)
                    self.assertFalse(web_state["OOMKilled"])
                    self.assertNotIn("thread(s) still running", run([docker, "logs", baseline]))
                run(stack + ["stop", "--timeout", "5", "russound-dummy"])
                stopped()
                # All writers in these disposable trees are the two stopped
                # fixture containers. Only this test controls their lifecycle.
                manifest_identity = run(helper + ["-c", """
import sys
from pathlib import Path
from tool.deployment_backup import FileBackup
backup = FileBackup.capture(Path('/operation/backups'), sys.argv[1],
                          {'web': Path('/operation/live')}, verify_quiescent=lambda: None)
print(backup.manifest_sha256)
""", operation])
                stopped()
                state.advance("fixture", operation, "backup-complete", backup=operation)
                before = (state.root / "fixture.json").read_bytes()
                evidence_dir = root / "completion"
                evidence_dir.mkdir(mode=0o700)
                inventory = root / "inventory.yml"
                inventory.write_text(yaml.safe_dump({"all": {"hosts": {"fixture": {
                    "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                    "ansible_async_dir": str(root / "async"),
                }}}}))
                playbook = root / "failed-apply.yml"
                playbook.write_text(yaml.safe_dump([{
                    "hosts": "all", "gather_facts": False, "tasks": [{
                        "ansible.builtin.command": {"argv": stack + [
                            "up", "-d", "--no-build", "--pull", "never", "--no-deps", "russound-web"]},
                        "environment": {**settings, "RUSSOUND_WEB_IMAGE": candidate["images"]["web"]["reference"]},
                        "no_log": True, "tags": ["deployment_required_apply"],
                    }],
                }]))
                with self.assertRaises(StateError):
                    run_checked(ansible=ANSIBLE, playbook=playbook, inventory=inventory,
                                evidence=evidence_dir / "failed.jsonl", operation=operation,
                                host="fixture", required=["apply"])
                stopped()
                self.assertEqual((state.root / "fixture.json").read_bytes(), before)
                self.assertTrue(state.status()["blocked"])
                with self.assertRaises(StateError):
                    state.begin("another", str(uuid4()), candidate, "sha256:" + "e" * 64, ["apply"])
                run(helper + ["-c", """
import sys
from pathlib import Path
from tool.deployment_backup import FileBackup
backup = FileBackup(Path('/operation/backups'), sys.argv[1], sys.argv[2])
backup.restore_new(Path('/operation/recovered'), verify_quiescent=lambda: None)
""", operation, manifest_identity])
                stopped()
                run(helper + ["-c", """
import sys
from pathlib import Path
root = Path('/operation/recovered/web')
assert (root / 'config/russound_config.json').read_bytes() == sys.argv[1].encode()
assert (root / 'data/preserved').read_bytes() == b'unrelated-state'
assert (root / 'secrets/token').read_bytes() == b'synthetic-recovery-token'
""", original_config.decode()])
                env["RUSSOUND_DATA_DIR"] = mountpoint + "/recovered/web"
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                routing()
                restored_id = run(stack + ["ps", "-q", "russound-web"])
                run(stack + ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60"])
                self.assertEqual(run(stack + ["ps", "-q", "russound-web"]), restored_id)
                self.assertEqual((state.root / "fixture.json").read_bytes(), before)
                self.assertTrue(state.status()["blocked"])
                run(helper + ["-c", """
import sys
from pathlib import Path
for tree in ('live', 'recovered/web'):
    assert (Path('/operation') / tree / 'config/russound_config.json').read_bytes() == sys.argv[1].encode()
""", original_config.decode()])
            finally:
                try:
                    run(stack + ["down", "--timeout", "5"])
                finally:
                    run([docker, "volume", "rm", volume])


if __name__ == "__main__":
    unittest.main()
from contextlib import closing
import http.client
