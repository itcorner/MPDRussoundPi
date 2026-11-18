import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import yaml

from tool.deployment_backup import FileBackup
from tool.deployment_build import archive_identity, digest_stream
from tool.deployment_compose import (
    ComposeDeployment, ComposeError, encoded, paths, runtime_checks, specification, validate_runtime_files,
)
from tool.deployment_images import REPOSITORIES, resolve, validate_selection
from tool.deployment_load import select_bundle
from tool.deployment_preflight import PreflightError, check_containers, validate_request
from tool.deployment_recovery import recovery_assets
from tool.deployment_state import StateError, StateStore
from test_deployment_build import fixture_bundle, image_archive
from test_deployment_images import FixtureRegistry
from test_deployment_operator import spec_fixture


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"


def backend_spec(root):
    return {
        **spec_fixture(root), "gateway": "standalone", "publish_host": "192.0.2.10",
        "publish_port": 6666, "serial_gid": 20,
    }


def backend_compose(spec, image):
    return {
        "name": spec["project"], "services": {"russound-ser2net": {
            "image": image, "platform": spec["platform"], "user": f'{spec["uid"]}:{spec["gid"]}',
            "restart": "unless-stopped",
            "devices": [{"source": spec["serial_device"], "target": spec["serial_device"], "permissions": "rwm"}],
            "group_add": [str(spec["serial_gid"])],
            "ports": [{"host_ip": spec["publish_host"], "target": 6666,
                       "published": str(spec["publish_port"]), "protocol": "tcp"}],
            "environment": {"RUSSOUND_SERIAL_DEVICE": spec["serial_device"],
                            "RUSSOUND_SERIAL_OPTIONS": "19200n81",
                            "RUSSOUND_SER2NET_BIND": "0.0.0.0", "RUSSOUND_SER2NET_PORT": "6666"},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }},
    }


class SplitDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-split-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)

    def target(self, spec, selection, config):
        path = Path(spec["compose"])
        path.write_bytes(encoded(config))
        path.chmod(0o600)
        with patch("tool.deployment_compose.local_engine", return_value="default"):
            return ComposeDeployment(spec, selection, paths(self.root))

    def test_gateway_only_registry_and_local_selection_never_include_web(self):
        registry = FixtureRegistry()
        selected = resolve("standalone", "linux/arm64", registry)
        self.assertEqual(set(selected["images"]), {"ser2net"})
        self.assertEqual(validate_selection(selected, "standalone", "linux/arm64"), selected)
        self.assertTrue(all(repository == REPOSITORIES["ser2net"] for repository, _ in registry.requests))
        bundle, checksum, _ = fixture_bundle(self.root, "standalone")
        local = select_bundle(bundle, checksum, "standalone", "linux/arm64")
        self.assertEqual(set(local["manifest"]["images"]), {"ser2net"})

    def test_backend_requires_explicit_lan_listener_and_serial_ownership(self):
        spec = backend_spec(self.root)
        specification(spec)
        request = {key: spec[key] for key in (
            "project", "data_dir", "uid", "gid", "gateway", "platform", "publish_host",
            "publish_port", "serial_device", "serial_gid")}
        for key, value in (("publish_host", "0.0.0.0"), ("publish_host", "::"),
                           ("publish_host", "127.0.0.1"), ("publish_host", "::1"),
                           ("publish_host", "224.0.0.1"), ("serial_gid", None)):
            with self.subTest(key=key, value=value), self.assertRaises(PreflightError):
                validate_request({**request, key: value})
        for flag in ("require_controller", "require_playback"):
            with self.subTest(flag=flag), self.assertRaises(ComposeError):
                specification({**spec, flag: True})
        with self.assertRaisesRegex(PreflightError, "Serial adapter"):
            check_containers(request, [{"HostConfig": {"Devices": [{"PathOnHost": spec["serial_device"]}]}}])

    def test_gateway_only_scope_rejects_unreviewed_services_mounts_and_listeners(self):
        spec = backend_spec(self.root)
        selection = resolve("standalone", "linux/arm64", FixtureRegistry())
        config = backend_compose(spec, selection["images"]["ser2net"]["reference"])
        target = self.target(spec, selection, config)
        self.assertEqual(target.services, {"russound-ser2net": "ser2net"})
        target.baseline = []
        target.backup_estimate = 1024
        self.assertEqual(target.scope()["native_services"], "untouched")
        self.assertEqual(target.scope()["services"], ["russound-ser2net"])
        for change in (
            {"ports": []},
            {"ports": [{"host_ip": "0.0.0.0", "published": "6666", "target": 6666}]},
            {"ports": [{"host_ip": spec["publish_host"], "published": "6666", "target": 6666, "protocol": "udp"}]},
            {"devices": []}, {"group_add": []},
            {"volumes": [{"type": "bind", "source": str(Path(spec["data_dir"]) / "config"), "target": "/config"}]},
            {"environment": {"RUSSOUND_SER2NET_BIND": "127.0.0.1"}},
            {"depends_on": {"native-agent": {"condition": "service_healthy"}}},
        ):
            invalid = copy.deepcopy(config)
            invalid["services"]["russound-ser2net"].update(change)
            with self.subTest(change=change), self.assertRaises(ComposeError):
                self.target(spec, selection, invalid)
        invalid = copy.deepcopy(config)
        invalid["services"]["russound-web"] = {}
        with self.assertRaises(ComposeError):
            self.target(spec, selection, invalid)

    def test_gateway_runtime_checks_published_tcp_without_claiming_controller_or_audio(self):
        spec = backend_spec(self.root)
        selection = resolve("standalone", "linux/arm64", FixtureRegistry())
        target = self.target(spec, selection, backend_compose(spec, selection["images"]["ser2net"]["reference"]))
        container = {
            "Id": "gateway", "Image": "sha256:" + "a" * 64, "RestartCount": 0,
            "Config": {"Labels": {"com.docker.compose.project": spec["project"],
                                  "com.docker.compose.service": "russound-ser2net"}},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        with patch.object(target, "inspect_all", return_value=[container]):
            with patch("tool.deployment_compose.socket.create_connection") as connect:
                self.assertEqual(runtime_checks(target, {"ser2net": container["Image"]}),
                                 {"containers": "passed", "gateway": "passed"})
                connect.assert_called_once_with((spec["publish_host"], spec["publish_port"]), timeout=5)
        with patch.object(target, "inspect_all", return_value=[container]):
            with patch("tool.deployment_compose.socket.create_connection", side_effect=OSError):
                with patch("tool.deployment_compose.time.monotonic", side_effect=[0, 61]):
                    with self.assertRaises(ComposeError):
                        runtime_checks(target, {"ser2net": container["Image"]})

    def test_gateway_preflight_uses_no_web_config_and_checks_its_device_and_port(self):
        spec = backend_spec(self.root)
        selection = resolve("standalone", "linux/arm64", FixtureRegistry())
        target = self.target(spec, selection, backend_compose(spec, selection["images"]["ser2net"]["reference"]))
        Path(spec["data_dir"]).mkdir(mode=0o700)
        with patch("tool.deployment_compose.sys.platform", "linux"), \
                patch("tool.deployment_compose.os.geteuid", return_value=0), \
                patch("tool.deployment_compose.check_data"), \
                patch("tool.deployment_compose.validate_runtime_files") as files, \
                patch("tool.deployment_compose.check_serial") as serial, \
                patch("tool.deployment_compose.check_port") as port, \
                patch.object(target, "inspect_all", return_value=[]), \
                patch.object(target, "command", return_value=encoded({
                    "Architecture": "aarch64", "OSType": "linux", "SecurityOptions": [], "ID": "fixture"})):
            target.preflight()
            files.assert_called_once_with(spec, {})
            serial.assert_called_once_with(spec)
            port.assert_called_once_with(spec["publish_host"], spec["publish_port"])
        self.assertEqual(target.baseline, [])
        self.assertFalse((Path(spec["data_dir"]) / "config/russound_config.json").exists())
        for folder in ("config", "data", "logs", "secrets"):
            (Path(spec["data_dir"]) / folder).mkdir(mode=0o700)
        with patch("tool.deployment_compose.extended_metadata"):
            validate_runtime_files({**spec, "uid": os.getuid(), "gid": os.getgid()}, {})

    def test_gateway_runner_and_recovery_require_only_the_gateway_image_and_checks(self):
        spec = backend_spec(self.root)
        bundle, checksum, manifest = fixture_bundle(self.root, "standalone")
        selection = select_bundle(bundle, checksum, "standalone", "linux/arm64")
        target = self.target(spec, selection, backend_compose(spec, manifest["images"]["ser2net"]["reference"]))
        target.baseline = []
        target.backup_estimate = 1024
        with patch.object(target, "preflight"), patch("tool.deployment_compose.DevelopmentRun") as run:
            target.runner(lambda backup: None)
            self.assertEqual(run.call_args.kwargs["required_checks"], ["containers", "gateway"])
        reference = image_archive(target.asset / "ser2net.tar",
                                  f"mpdrussoundpi-recovery/ser2net:{target.operation}", manifest["revision"])
        image = {"reference": reference, "tag": f"mpdrussoundpi-recovery/ser2net:{target.operation}"}
        with (target.asset / "ser2net.tar").open("rb") as source:
            image["config"] = archive_identity(source, image, spec["platform"], None)
            source.seek(0)
            image["sha256"], image["bytes"] = digest_stream(source)
        image["archive"] = "ser2net.tar"
        # runner() wrote an absent baseline; replace it with a synthetic prior gateway.
        baseline = target.asset / "baseline.json"
        baseline.chmod(0o600)
        baseline.write_bytes(encoded([{
            "image": reference, "config": {"Labels": {"com.docker.compose.service": "russound-ser2net"}},
        }]))
        baseline.chmod(0o400)
        images = target.asset / "images.json"
        images.write_bytes(encoded({"ser2net": image}))
        images.chmod(0o400)
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            backup = FileBackup.capture(target.storage["backups"], target.operation,
                                        {"deployment": target.asset}, verify_quiescent=lambda: None)
        restored_spec, restored_images = recovery_assets(backup.path, backup.operation, backup.manifest_sha256)
        self.assertEqual(restored_spec, spec)
        self.assertEqual(set(restored_images), {"ser2net"})

    def test_recovery_state_requires_the_correct_layout_specific_receipt(self):
        for gateway in ("standalone", "external"):
            for local in (False, True):
                with self.subTest(gateway=gateway, local=local):
                    parent = self.root / str(uuid4())
                    parent.mkdir(mode=0o700)
                    store = StateStore(parent / "state", owner_uid=os.getuid())
                    store.initialize()
                    if local:
                        bundle, checksum, _ = fixture_bundle(parent, gateway)
                        selection = select_bundle(bundle, checksum, gateway, "linux/arm64")
                    else:
                        selection = resolve(gateway, "linux/arm64", FixtureRegistry())
                    operation = str(uuid4())
                    store.begin("fixture", operation, selection, "sha256:" + "a" * 64, ["apply"])
                    expected = {"containers": "passed", "gateway": "passed"} if gateway == "standalone" else {
                        "containers": "passed", "http": "passed", "backend": "passed", "playback": "passed"}
                    receipt = {"operation": operation, "review_digest": "sha256:" + "b" * 64,
                               "required": expected}
                    for invalid in (
                        {"containers": "passed"},
                        {"containers": "passed", "gateway": "passed"} if gateway == "external" else {
                            "containers": "passed", "http": "passed", "backend": "passed", "playback": "passed"},
                        {**expected, "containers": "failed"},
                    ):
                        with self.assertRaises(StateError):
                            store.resolve_recovery("fixture", operation, {**receipt, "required": invalid})
                        self.assertTrue(store.status()["blocked"])
                    store.resolve_recovery("fixture", operation, receipt)
                    self.assertFalse(store.status()["blocked"])

    @unittest.skipUnless(ANSIBLE.is_file() and shutil.which("docker"), "Ansible and Compose are required")
    def test_ansible_standalone_preview_supports_pins_and_local_bundles_without_contacting_backend(self):
        inventory = self.root / "inventory.yml"
        variables = {
            "ansible_connection": "ssh", "ansible_host": "invalid.example",
            "russound_gateway": "standalone", "russound_data_dir": "/srv/gateway",
            "russound_publish_host": "192.0.2.10", "russound_publish_port": 6666,
            "russound_serial_gid": 20,
        }
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"backend": variables}}}))
        command = [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev.yml")]
        selection = resolve("standalone", "linux/arm64", FixtureRegistry())
        candidate = self.root / "candidate.json"
        candidate.write_bytes(encoded(selection))
        candidate.chmod(0o600)
        bundle, checksum, _ = fixture_bundle(self.root, "standalone")
        for overrides in (
            {"russound_ser2net_image": selection["images"]["ser2net"]["reference"]},
            {"russound_image_selection_file": str(candidate)},
            {"russound_image_source": "local-build", "russound_local_bundle": str(bundle),
             "russound_local_manifest_sha256": checksum},
        ):
            with self.subTest(overrides=overrides):
                result = subprocess.run(command + ["-e", json.dumps(overrides)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("changed=0", result.stdout)
                self.assertIn("russound-ser2net", result.stdout)
        for overrides in (
            {"russound_publish_host": "0.0.0.0"},
            {"russound_web_image": "itcornernet/mpdrussoundpi-web@sha256:" + "a" * 64},
        ):
            result = subprocess.run(command + ["-e", json.dumps({
                "russound_ser2net_image": selection["images"]["ser2net"]["reference"], **overrides,
            })], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(ANSIBLE.is_file(), "Ansible is required")
    def test_tooling_plays_accept_arbitrary_single_host_and_reject_multi_host_runs_before_connecting(self):
        inventory = self.root / "tooling-inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {
            host: {"ansible_host": "invalid.example", "ansible_connection": "ssh"}
            for host in ("backend", "web")
        }}}))
        for name in ("bootstrap-deployment.yml", "upgrade-deployment.yml", "restore-deployment-tools.yml"):
            command = [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks" / name),
                       "--tags", "split-host-scope-check"]
            with self.subTest(playbook=name):
                result = subprocess.run(command + ["--limit", "backend"],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("changed=0", result.stdout)
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Select exactly one tooling target", result.stdout)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is required")
    def test_backend_compose_extends_gateway_without_web_or_ambient_inputs(self):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("COMPOSE_", "RUSSOUND_"))}
        environment.update({
            "COMPOSE_DISABLE_ENV_FILE": "1", "RUSSOUND_PLATFORM": "linux/arm64",
            "RUSSOUND_SER2NET_IMAGE": "itcornernet/mpdrussoundpi-ser2net@sha256:" + "b" * 64,
            "RUSSOUND_SERIAL_GID": "20", "RUSSOUND_GATEWAY_PUBLISH_HOST": "192.0.2.10",
            "RUSSOUND_GATEWAY_PUBLISH_PORT": "6666",
        })
        command = ["docker", "compose", "--env-file", "/dev/null", "-p", "split-test",
                   "-f", str(ROOT / "compose.backend.yml"), "config", "--format", "json"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(set(config["services"]), {"russound-ser2net"})
        gateway = config["services"]["russound-ser2net"]
        self.assertEqual(gateway["ports"][0]["host_ip"], "192.0.2.10")
        self.assertNotIn("volumes", gateway)
        self.assertNotIn("build", gateway)
        self.assertEqual(gateway["logging"]["options"], {"max-size": "10m", "max-file": "3"})
        environment["RUSSOUND_GATEWAY_PUBLISH_HOST"] = "2001:db8::10"
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["services"]["russound-ser2net"]["ports"][0]["host_ip"],
                         "2001:db8::10")
        del environment["RUSSOUND_GATEWAY_PUBLISH_HOST"]
        self.assertNotEqual(subprocess.run(command, env=environment, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
