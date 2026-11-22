import copy
import argparse
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tool.deployment_backup import FileBackup
from tool.deployment_channel import BACKUP_HEADROOM, Channel, ChannelError, portable_backup, receive_backup, send_backup
from tool.deployment_compose import (
    ComposeDeployment, ComposeError, check_host_writers, encoded, paths, specification, validate_runtime_files,
)
from tool.deployment_load import select_bundle
from tool.deployment_operator import controller, interactive, main, operator_lock, remote
from tool.deployment_recovery import recovery_assets
from test_deployment_build import fixture_bundle


ROOT = Path(__file__).resolve().parents[1]


def spec_fixture(root):
    return {
        "schema_version": 1, "deployment": "fixture", "project": "fixture",
        "compose": str(root / "compose.json"), "data_dir": str(root / "live"),
        "uid": 10001, "gid": 10001, "gateway": "external", "platform": "linux/arm64",
        "publish_host": "127.0.0.1", "publish_port": 18000,
        "serial_device": "/dev/ttyUSB0", "serial_gid": None,
        "require_controller": False, "require_playback": False, "adopt": True,
    }


def compose_fixture(spec, image):
    return {
        "name": spec["project"], "services": {"russound-web": {
            "image": image, "platform": spec["platform"], "user": f'{spec["uid"]}:{spec["gid"]}',
            "ports": [{"host_ip": spec["publish_host"], "target": 8000, "published": str(spec["publish_port"])}],
            "volumes": [
                {"type": "bind", "source": str(Path(spec["data_dir"]) / name), "target": target,
                 "read_only": name == "secrets", "bind": {"create_host_path": False}}
                for name, target in (("config", "/config"), ("data", "/data"), ("logs", "/logs"),
                                     ("secrets", "/run/secrets"))
            ],
        }},
    }


class OperatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-operator-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.storage = paths(self.root)
        self.spec = spec_fixture(self.root)
        self.bundle, self.checksum, _ = fixture_bundle(self.root)
        self.selection = select_bundle(self.bundle, self.checksum, "external", "linux/arm64")
        self.config = compose_fixture(self.spec, self.selection["manifest"]["images"]["web"]["reference"])
        self.compose = Path(self.spec["compose"])
        self.compose.write_bytes(encoded(self.config))
        self.compose.chmod(0o600)

    def target(self):
        with patch("tool.deployment_compose.local_engine", return_value="default"):
            return ComposeDeployment(self.spec, self.selection, self.storage)

    def test_operator_lock_excludes_other_operations_and_releases_on_exit(self):
        with operator_lock(self.storage["evidence"]):
            with self.assertRaisesRegex(ChannelError, "Another target operator"):
                with operator_lock(self.storage["evidence"]):
                    self.fail("Concurrent operator acquired the lock")
        with operator_lock(self.storage["evidence"]):
            pass

    def test_operator_lock_rejects_unsafe_existing_lock(self):
        path = self.storage["evidence"] / "operator.lock"
        path.write_text("")
        path.chmod(0o644)
        with self.assertRaisesRegex(ChannelError, "Unsafe target operator lock"):
            with operator_lock(self.storage["evidence"]):
                self.fail("Unsafe lock was accepted")

    def test_operator_lock_excludes_a_separate_process(self):
        script = ("from pathlib import Path\n"
                  "from tool.deployment_operator import operator_lock\n"
                  "with operator_lock(Path(__import__('sys').argv[1])): print('acquired')\n")
        with operator_lock(self.storage["evidence"]):
            result = subprocess.run([sys.executable, "-c", script, str(self.storage["evidence"])],
                                    cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another target operator is active", result.stderr)

    def test_controller_timeout_cli_passes_default_and_explicit_limit(self):
        argv = ["deployment_operator", "resume-original", "--host", "127.0.0.1",
                "--user", "fixture", "--known-hosts", "known_hosts", "--evidence", "evidence",
                "--deployment", "fixture", "--operation", str(uuid4())]
        for options, expected in (([], 3600), (["--controller-timeout-seconds", "18000"], 18000)):
            with self.subTest(expected=expected), patch.object(sys, "argv", argv + options), \
                    patch("tool.deployment_operator.remote", return_value={}) as run, patch("builtins.print"):
                self.assertEqual(main(), 0)
                self.assertEqual(run.call_args.kwargs["timeout_seconds"], expected)
        for invalid in ("0", "18001"):
            with self.subTest(invalid=invalid), \
                    patch.object(sys, "argv", argv + ["--controller-timeout-seconds", invalid]), \
                    patch("tool.deployment_operator.remote") as run, patch.object(sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_remote_uses_and_cancels_the_configured_timer(self):
        trust = self.root / "known_hosts"
        trust.write_bytes(b"fixture trust snapshot\n")
        trust.chmod(0o600)
        args = argparse.Namespace(evidence=self.storage["evidence"], known_hosts=trust,
                                  host="127.0.0.1", user="fixture", port=22, identity=None,
                                  bundle=None, backup=None, backup_sha256=None)
        request = {"action": "resume-original", "operation": str(uuid4())}
        with patch("tool.deployment_operator.subprocess.Popen") as popen, \
                patch("tool.deployment_operator.controller", return_value={"event": "recovered"}), \
                patch("tool.deployment_operator.threading.Timer") as timer:
            process = popen.return_value.__enter__.return_value
            process.wait.return_value = 0
            process.poll.return_value = 0
            self.assertEqual(remote(args, request, timeout_seconds=18000), {"event": "recovered"})
        timer.assert_called_once_with(18000, process.kill)
        timer.return_value.start.assert_called_once_with()
        timer.return_value.cancel.assert_called_once_with()
        process.kill.assert_not_called()

    def test_prepared_scope_preserves_exact_services_and_rejects_privilege_or_mount_changes(self):
        target = self.target()
        with self.assertRaises(ComposeError):
            target.scope()
        target.baseline = []
        target.backup_estimate = 1024
        self.assertEqual(target.scope()["native_services"], "untouched")
        for key, value in (("privileged", True), ("build", "."), ("network_mode", "host"),
                           ("user", "0:0"), ("platform", "linux/amd64"),
                           ("ports", [{"target": 8000, "published": "19000"}]),
                           ("volumes", [{"type": "bind", "source": "/etc", "target": "/config"}])):
            config = copy.deepcopy(self.config)
            config["services"]["russound-web"][key] = value
            self.compose.write_bytes(encoded(config))
            with self.subTest(key=key), self.assertRaises(ComposeError):
                self.target()
        self.assertFalse(any(self.storage["state"].glob("reservation")))

    def test_compose_apply_rejects_committed_native_layout_without_mutation(self):
        from tool.deployment_native_source import NATIVE_CANDIDATE

        committed = {"selection": {"candidate": {"kind": NATIVE_CANDIDATE}}}
        status = {"deployments": {self.spec["deployment"]: {"committed": committed}}}
        with patch("tool.deployment_compose.local_engine", return_value="default"), \
                patch("tool.deployment_compose.StateStore.status", return_value=status):
            with self.assertRaisesRegex(ComposeError, "Native-to-Docker migration"):
                ComposeDeployment(self.spec, self.selection, self.storage)
        self.assertFalse(any(self.storage["assets"].iterdir()))
        self.assertFalse((self.storage["state"] / "reservation").exists())

    def committed_target(self, recorded, supplied):
        """A target whose last committed layout is ``recorded`` and whose supplied input is ``supplied``."""
        operation = str(uuid4())
        runtime = self.storage["runtime"] / operation
        runtime.mkdir(parents=True)
        (runtime / "active.json").write_bytes(encoded(recorded))
        (runtime / "active.json").chmod(0o600)
        self.compose.write_bytes(encoded(supplied))
        committed = {"selection": {"candidate": {"kind": "local-development-image-candidate"}, "operation": operation}}
        status = {"deployments": {self.spec["deployment"]: {"committed": committed}}}
        with patch("tool.deployment_compose.local_engine", return_value="default"), \
                patch("tool.deployment_compose.StateStore.status", return_value=status):
            return ComposeDeployment(self.spec, self.selection, self.storage)

    def updater_mount(self, **changes):
        from tool.deployment_compose import UPDATER_SOCKET_DIR, UPDATER_SOCKET_TARGET
        mount = {"type": "bind", "source": UPDATER_SOCKET_DIR, "target": UPDATER_SOCKET_TARGET,
                 "read_only": True, "bind": {"create_host_path": False}}
        mount.update(changes)
        return mount

    def test_update_may_add_exactly_the_updater_socket_mount(self):
        from tool.deployment_compose import UPDATER_LAYOUT_CHANGE
        recorded = copy.deepcopy(self.config)
        recorded["services"]["russound-web"]["image"] = "sha256:" + "b" * 64
        supplied = copy.deepcopy(self.config)
        supplied["services"]["russound-web"]["volumes"].append(self.updater_mount())
        target = self.committed_target(recorded, supplied)
        self.assertEqual(target.layout_change, UPDATER_LAYOUT_CHANGE)
        applied = json.loads((target.asset / "compose.json").read_text())
        self.assertIn(self.updater_mount(), applied["services"]["russound-web"]["volumes"])
        self.assertEqual(applied["services"]["russound-web"]["image"], "sha256:" + "b" * 64,
                         "the recorded layout is the base; images are replaced only at apply")
        running = json.loads((target.asset / "running.json").read_text())
        self.assertNotIn(self.updater_mount(), running["services"]["russound-web"]["volumes"])
        target.baseline, target.backup_estimate = [], 0
        self.assertEqual(target.scope()["layout_change"], UPDATER_LAYOUT_CHANGE)

    def test_an_unchanged_layout_reports_no_layout_change(self):
        target = self.committed_target(self.config, self.config)
        self.assertIsNone(target.layout_change)
        self.assertFalse((target.asset / "running.json").exists())
        target.baseline, target.backup_estimate = [], 0
        self.assertNotIn("layout_change", target.scope())

    def test_any_other_layout_change_is_still_refused(self):
        variants = {
            "writable": self.updater_mount(read_only=False),
            "other source": self.updater_mount(source="/var/lib/elsewhere"),
            "other target": self.updater_mount(target="/run/elsewhere"),
            "creates host path": self.updater_mount(bind={"create_host_path": True}),
            "extra field": {**self.updater_mount(), "consistency": "cached"},
        }
        for name, mount in variants.items():
            supplied = copy.deepcopy(self.config)
            supplied["services"]["russound-web"]["volumes"].append(mount)
            with self.subTest(name=name), self.assertRaisesRegex(ComposeError, "Recorded layout changed"):
                self.committed_target(self.config, supplied)
        both = copy.deepcopy(self.config)
        both["services"]["russound-web"]["volumes"].append(self.updater_mount())
        both["services"]["russound-web"]["environment"] = {"RUSSOUND_DEBUG": "true"}
        with self.assertRaisesRegex(ComposeError, "Recorded layout changed"):
            self.committed_target(self.config, both)
        recorded = copy.deepcopy(self.config)
        recorded["services"]["russound-web"]["volumes"].append(self.updater_mount())
        with self.assertRaisesRegex(ComposeError, "Recorded layout changed"):
            self.committed_target(recorded, self.config)  # removing it is not the accepted addition

    def test_preflight_requires_the_installed_updater_socket_directory(self):
        from tool.deployment_compose import UPDATER_SOCKET_DIR
        if Path(UPDATER_SOCKET_DIR).exists():
            self.skipTest("an updater is installed on this machine")
        config = copy.deepcopy(self.config)
        config["services"]["russound-web"]["volumes"].append(self.updater_mount())
        self.compose.write_bytes(encoded(config))
        target = self.target()
        info = {"Architecture": "aarch64", "OSType": "linux", "SecurityOptions": [], "ID": "fixture"}
        with patch("tool.deployment_compose.sys.platform", "linux"), \
                patch("tool.deployment_compose.os.geteuid", return_value=0), \
                patch("tool.deployment_compose.check_data"), \
                patch.object(target, "command", return_value=encoded(info)):
            with self.assertRaisesRegex(ComposeError, "install the updater service first"):
                target.preflight()

    def test_a_first_install_may_include_the_updater_mount(self):
        config = copy.deepcopy(self.config)
        config["services"]["russound-web"]["volumes"].append(self.updater_mount())
        self.compose.write_bytes(encoded(config))
        self.assertIsNone(self.target().layout_change)

    # ---------------------------------------------------------------- HTTPS layout

    def tls_mount(self, **changes):
        from tool.deployment_compose import WEB_TLS_PUBLIC_DIR, WEB_TLS_TARGET
        mount = {"type": "bind", "source": WEB_TLS_PUBLIC_DIR, "target": WEB_TLS_TARGET,
                 "read_only": True, "bind": {"create_host_path": False}}
        mount.update(changes)
        return mount

    def lan_layout(self):
        """The recorded layout before HTTPS: the listener on all addresses."""
        recorded = copy.deepcopy(self.config)
        recorded["services"]["russound-web"]["ports"][0]["host_ip"] = "0.0.0.0"
        return recorded

    def https_layout(self, base=None, mount=None, environment=None, host="127.0.0.1"):
        supplied = copy.deepcopy(base or self.config)
        web = supplied["services"]["russound-web"]
        web["volumes"].append(mount or self.tls_mount())
        web["environment"] = {**web.get("environment", {}),
                              **(environment if environment is not None else
                                 {"RUSSOUND_TRUSTED_PROXY_COUNT": "1", "RUSSOUND_REQUIRE_HTTPS_LOGIN": "true"})}
        web["ports"][0]["host_ip"] = host
        return supplied

    def test_update_may_switch_to_the_https_layout_as_one_reviewed_change(self):
        from tool.deployment_compose import HTTPS_LAYOUT_CHANGE
        recorded = self.lan_layout()
        recorded["services"]["russound-web"]["image"] = "sha256:" + "b" * 64
        target = self.committed_target(recorded, self.https_layout())
        self.assertEqual(target.layout_change, HTTPS_LAYOUT_CHANGE)
        web = json.loads((target.asset / "compose.json").read_text())["services"]["russound-web"]
        self.assertIn(self.tls_mount(), web["volumes"])
        self.assertEqual(web["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual((web["environment"]["RUSSOUND_TRUSTED_PROXY_COUNT"],
                          web["environment"]["RUSSOUND_REQUIRE_HTTPS_LOGIN"]), ("1", "true"))
        self.assertEqual(web["image"], "sha256:" + "b" * 64, "images are replaced only at apply")
        running = json.loads((target.asset / "running.json").read_text())["services"]["russound-web"]
        self.assertEqual(running["ports"][0]["host_ip"], "0.0.0.0", "running containers keep the recorded layout")
        target.baseline, target.backup_estimate = [], 0
        self.assertEqual(target.scope()["layout_change"], HTTPS_LAYOUT_CHANGE)

    def test_the_updater_mount_and_the_https_layout_may_arrive_together(self):
        from tool.deployment_compose import HTTPS_LAYOUT_CHANGE, UPDATER_LAYOUT_CHANGE
        supplied = self.https_layout()
        supplied["services"]["russound-web"]["volumes"].insert(0, self.updater_mount())
        target = self.committed_target(self.lan_layout(), supplied)
        self.assertEqual(target.layout_change, UPDATER_LAYOUT_CHANGE + "; " + HTTPS_LAYOUT_CHANGE)

    def test_partial_or_altered_https_layouts_are_refused(self):
        variants = {
            "listener left on the LAN": self.https_layout(host="0.0.0.0"),
            "no proxy settings": self.https_layout(environment={}),
            "only one setting": self.https_layout(environment={"RUSSOUND_TRUSTED_PROXY_COUNT": "1"}),
            "two proxies": self.https_layout(environment={"RUSSOUND_TRUSTED_PROXY_COUNT": "2",
                                                          "RUSSOUND_REQUIRE_HTTPS_LOGIN": "true"}),
            "extra setting": self.https_layout(environment={"RUSSOUND_TRUSTED_PROXY_COUNT": "1",
                                                            "RUSSOUND_REQUIRE_HTTPS_LOGIN": "true",
                                                            "RUSSOUND_DEBUG": "true"}),
            "writable mount": self.https_layout(mount=self.tls_mount(read_only=False)),
            "other source": self.https_layout(mount=self.tls_mount(source="/etc/mpdrussoundpi-web-tls")),
            "creates host path": self.https_layout(mount=self.tls_mount(bind={"create_host_path": True})),
        }
        for name, supplied in variants.items():
            with self.subTest(name=name), self.assertRaises(ComposeError):
                self.committed_target(self.lan_layout(), supplied)
        settings_only = copy.deepcopy(self.config)
        settings_only["services"]["russound-web"]["environment"] = {
            "RUSSOUND_TRUSTED_PROXY_COUNT": "1", "RUSSOUND_REQUIRE_HTTPS_LOGIN": "true"}
        with self.assertRaisesRegex(ComposeError, "Recorded layout changed"):
            self.committed_target(self.lan_layout(), settings_only)
        with self.assertRaisesRegex(ComposeError, "Recorded layout changed"):
            self.committed_target(self.https_layout(), self.config)  # removing it is not an accepted addition

    def test_proxy_settings_never_appear_with_a_listener_the_network_can_reach(self):
        lan_spec = {**self.spec, "publish_host": "0.0.0.0"}
        config = self.https_layout(base=self.lan_layout(), host="0.0.0.0")
        self.compose.write_bytes(encoded(config))
        with patch("tool.deployment_compose.local_engine", return_value="default"), \
                self.assertRaisesRegex(ComposeError, "HTTPS layout needs all of"):
            ComposeDeployment(lan_spec, self.selection, self.storage)
        settings_only = self.lan_layout()
        settings_only["services"]["russound-web"]["environment"] = {"RUSSOUND_TRUSTED_PROXY_COUNT": "1"}
        self.compose.write_bytes(encoded(settings_only))
        with patch("tool.deployment_compose.local_engine", return_value="default"), \
                self.assertRaisesRegex(ComposeError, "HTTPS layout needs all of"):
            ComposeDeployment(lan_spec, self.selection, self.storage)

    def test_a_first_install_may_include_the_https_layout(self):
        self.compose.write_bytes(encoded(self.https_layout()))
        self.assertIsNone(self.target().layout_change)

    def test_preflight_requires_the_running_https_proxy(self):
        import tool.deployment_compose as compose
        self.compose.write_bytes(encoded(self.https_layout()))
        target = self.target()
        info = {"Architecture": "aarch64", "OSType": "linux", "SecurityOptions": [], "ID": "fixture"}
        status = self.root / "web-tls" / "status.json"

        def preflight():
            with patch("tool.deployment_compose.sys.platform", "linux"), \
                    patch("tool.deployment_compose.os.geteuid", return_value=0), \
                    patch("tool.deployment_compose.check_data"), \
                    patch.object(compose, "WEB_TLS_STATUS", str(status)), \
                    patch.object(target, "command", return_value=encoded(info)):
                target.preflight()

        with self.assertRaisesRegex(ComposeError, "run deploy-web-tls.yml first"):
            preflight()
        status.parent.mkdir()
        status.write_text("{}")
        with patch("tool.deployment_compose.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 3)) as run, \
                self.assertRaisesRegex(ComposeError, "HTTPS proxy is not running"):
            preflight()
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "is-active", "--quiet", "mpdrussoundpi-web-proxy.service"])

    def test_explicit_adoption_acceptance_and_no_hidden_dummy_build(self):
        for key, value in (("adopt", "yes"), ("require_playback", None),
                           ("data_dir", "/"), ("compose", "../compose.json")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                specification({**self.spec, key: value})
        with self.assertRaisesRegex(ValueError, "Dummy acceptance"):
            specification({**self.spec, "gateway": "dummy"})
        config = copy.deepcopy(self.config)
        config["services"]["russound-web"]["environment"] = {"RUSSOUND_PLAYBACK_AGENTS": "private"}
        self.compose.write_bytes(encoded(config))
        with self.assertRaisesRegex(ComposeError, "playback requires acceptance"):
            self.target()

    def test_dummy_requires_verified_local_image_and_isolated_headless_route(self):
        spec = {**self.spec, "gateway": "dummy", "require_controller": True}
        bundle, checksum, _ = fixture_bundle(self.root, "dummy")
        selection = select_bundle(bundle, checksum, "dummy", "linux/arm64")
        web_image = selection["manifest"]["images"]["web"]["reference"]
        dummy_image = selection["manifest"]["images"]["dummy"]["reference"]
        config = compose_fixture(spec, web_image)
        web = config["services"]["russound-web"]
        web["environment"] = {"RUSSOUND_BACKEND_HOST": "russound-dummy",
                              "RUSSOUND_BACKEND_PORT": "6666", "RUSSOUND_PLAYBACK_AGENTS": ""}
        web["depends_on"] = {"russound-dummy": {"condition": "service_healthy"}}
        web["volumes"] = [dict(mount, source=str(Path(spec["data_dir"]) / child))
                          for mount, child in zip(web["volumes"], ("config", "data", "logs", "secrets"))]
        config["services"]["russound-dummy"] = {
            "image": dummy_image, "platform": spec["platform"], "user": f'{spec["uid"]}:{spec["gid"]}',
            "volumes": [{"type": "bind", "source": str(Path(spec["data_dir"]) / "dummy-data"),
                         "target": "/data", "read_only": False,
                         "bind": {"create_host_path": False}}],
            "environment": {"RUSSOUND_DUMMY_TUI": "false"},
            "healthcheck": {"test": ["CMD", "python", "-c", "pass"]},
            "init": True, "tty": True,
        }
        self.compose.write_bytes(encoded(config))
        with patch("tool.deployment_compose.local_engine", return_value="default"):
            target = ComposeDeployment(spec, selection, self.storage)
        self.assertEqual(target.services, {"russound-web": "web", "russound-dummy": "dummy"})
        for service_name, mutation in (
            ("russound-dummy", {"environment": {"RUSSOUND_DUMMY_TUI": "true"}}),
            ("russound-dummy", {"ports": [{"target": 6666, "published": "6666"}]}),
            ("russound-dummy", {"devices": ["/dev/ttyUSB0:/dev/ttyUSB0"]}),
            ("russound-web", {"depends_on": {}}),
        ):
            invalid = copy.deepcopy(config)
            invalid["services"][service_name].update(mutation)
            self.compose.write_bytes(encoded(invalid))
            with self.subTest(service=service_name, mutation=mutation), self.assertRaises(ComposeError):
                with patch("tool.deployment_compose.local_engine", return_value="default"):
                    ComposeDeployment(spec, selection, self.storage)

    def test_compose_null_image_defaults_are_preserved_but_overrides_are_rejected(self):
        service = self.config["services"]["russound-web"]
        service.update(command=None, entrypoint=None)
        self.compose.write_bytes(encoded(self.config))
        target = self.target()
        self.assertEqual(target.config, self.config)
        for key in ("command", "entrypoint"):
            for value in ("", [], "custom-command", ["custom-command"]):
                config = copy.deepcopy(self.config)
                config["services"]["russound-web"][key] = value
                self.compose.write_bytes(encoded(config))
                with self.subTest(key=key, value=value), self.assertRaises(ComposeError):
                    self.target()

    def test_managed_preflight_accepts_foreign_containers_without_devices(self):
        target = self.target()
        target.spec.update(gateway="managed", serial_gid=20)
        config = Path(self.spec["data_dir"]) / "config/russound_config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}")
        info = {"Architecture": "aarch64", "OSType": "linux", "SecurityOptions": [], "ID": "fixture"}
        foreign = {"Mounts": [], "HostConfig": {"Devices": None}, "Config": {"Labels": {}}}
        with patch("tool.deployment_compose.sys.platform", "linux"), \
                patch("tool.deployment_compose.os.geteuid", return_value=0), \
                patch("tool.deployment_compose.check_data"), \
                patch("tool.deployment_compose.validate_runtime_files"), \
                patch("tool.deployment_compose.safe_path"), \
                patch("tool.deployment_compose.check_port"), \
                patch("tool.deployment_compose.check_serial"), \
                patch("web.config_types.validate_russound_config"), \
                patch.object(target, "command", return_value=encoded(info)), \
                patch.object(target, "inspect_all", return_value=[foreign]):
            target.preflight()
        self.assertEqual(target.baseline, [])

    def test_runtime_signature_ignores_mount_order_but_detects_mount_changes(self):
        target = self.target()
        item = {
            "Id": "fixture", "Image": "sha256:" + "a" * 64, "Config": {}, "HostConfig": {},
            "NetworkSettings": {"Networks": {"default": {"NetworkID": "fixture"}}},
            "Mounts": [
                {"Source": "/live/config", "Destination": "/config", "RW": True},
                {"Source": "/live/secrets", "Destination": "/run/secrets", "RW": False},
            ],
        }
        expected = target.current_signature([item])
        reordered = copy.deepcopy(item)
        reordered["Mounts"].reverse()
        self.assertEqual(target.current_signature([reordered]), expected)
        self.assertEqual(item["Mounts"][0]["Destination"], "/config")
        for field, value in (("Source", "/elsewhere"), ("Destination", "/elsewhere"), ("RW", True)):
            changed = copy.deepcopy(reordered)
            changed["Mounts"][0][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(target.current_signature([changed]), expected)

    def test_channel_rejects_truncation_oversize_and_nonobjects(self):
        import struct
        for value in (b"", b"\x00", struct.pack("!Q", 9 * 1024**2),
                      struct.pack("!Q", 2) + b"[]", struct.pack("!Q", 5) + b"{}"):
            with self.subTest(value=value), self.assertRaises((ValueError, EOFError)):
                Channel(io.BytesIO(value), io.BytesIO()).receive()

    def test_terminal_approval_uses_separate_nonseekable_read_and_write_streams(self):
        operation = str(uuid4())
        review = {"plan": {"operation": operation}, "digest": "sha256:" + "a" * 64}
        for answer, expected in (("approve " + operation, review["digest"]), ("yes", None)):
            streams = []
            def terminal(path, mode, **kwargs):
                self.assertEqual(path, "/dev/tty")
                if "+" in mode:
                    raise io.UnsupportedOperation("File or stream is not seekable")
                self.assertIn(mode, ("r", "w"))
                streams.append(mode)
                return io.StringIO(answer + "\n" if mode == "r" else "")
            with self.subTest(answer=answer), patch("builtins.open", side_effect=terminal), \
                    patch("builtins.print"):
                self.assertEqual(interactive(review), expected)
            self.assertEqual(sorted(streams), ["r", "w"])

    def test_channel_exact_private_transfer_and_corrupt_or_duplicate_payload_retention(self):
        source = self.storage["assets"] / "payload"
        source.write_bytes(b"synthetic-private-bytes")
        source.chmod(0o400)
        wire = io.BytesIO()
        Channel(io.BytesIO(), wire).send_file(source)
        receiver = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
        receiver.receive_file(self.storage["incoming"], "payload")
        result = self.storage["incoming"] / "payload"
        self.assertEqual(result.read_bytes(), source.read_bytes())
        self.assertEqual(result.stat().st_mode & 0o777, 0o400)
        with self.assertRaises(FileExistsError):
            Channel(io.BytesIO(wire.getvalue()), io.BytesIO()).receive_file(self.storage["incoming"], "payload")
        result.unlink()
        bad = wire.getvalue()[:-1] + b"X"
        with self.assertRaisesRegex(ChannelError, "checksum"):
            Channel(io.BytesIO(bad), io.BytesIO()).receive_file(self.storage["incoming"], "payload")
        self.assertTrue(result.exists())
        result.unlink()
        for bounds in ({"maximum": 1}, {"size": 1}, {"checksum": "0" * 64}):
            with self.subTest(bounds=bounds), self.assertRaises(ChannelError):
                Channel(io.BytesIO(wire.getvalue()), io.BytesIO()).receive_file(
                    self.storage["incoming"], "payload", **bounds)
            self.assertFalse(result.exists())

    def test_portable_offhost_verifier_authenticates_all_payloads_without_linux_metadata(self):
        source = self.root / "source"
        source.mkdir(mode=0o700)
        (source / "config").write_text("private")
        (source / "config").chmod(0o600)
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            backup = FileBackup.capture(self.storage["backups"], str(uuid4()), {"data": source},
                                        verify_quiescent=lambda: None)
        wire = io.BytesIO()
        send_backup(Channel(io.BytesIO(), wire), backup.path, backup.operation, backup.manifest_sha256)
        channel = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
        received, checksum = receive_backup(channel, self.storage["incoming"], channel.receive())
        manifest = portable_backup(received, backup.operation, checksum)
        self.assertGreater(manifest["total_bytes"], 0)
        blob = next(received.glob("*.blob"))
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt")
        blob.chmod(0o400)
        with self.assertRaises(ChannelError):
            portable_backup(received, backup.operation, checksum)
        with self.assertRaises(ChannelError):
            portable_backup(backup.path, backup.operation, "0" * 64)

    def test_backup_receive_checks_space_before_metadata_and_blobs_and_during_writes(self):
        source = self.root / "space-source"
        source.mkdir(mode=0o700)
        (source / "config").write_bytes(b"fixture data")
        (source / "config").chmod(0o600)
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            backup = FileBackup.capture(self.storage["backups"], str(uuid4()), {"data": source},
                                        verify_quiescent=lambda: None)
        wire = io.BytesIO()
        send_backup(Channel(io.BytesIO(), wire), backup.path, backup.operation, backup.manifest_sha256)
        total = len(b"fixture data")
        for scenario, free_values, message, metadata, blob in (
            ("metadata", [0], "metadata", False, False),
            ("restore", [BACKUP_HEADROOM + 8 * 1024**2, BACKUP_HEADROOM + 2 * total - 1],
             "headroom", True, False),
            ("write", [BACKUP_HEADROOM + 8 * 1024**2, BACKUP_HEADROOM + 2 * total,
                       BACKUP_HEADROOM + 2 * total, BACKUP_HEADROOM + 2 * total - 1],
             "exhausted", True, True),
        ):
            parent = self.storage["incoming"] / scenario
            parent.mkdir(mode=0o700)
            receiver = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
            header = receiver.receive()
            with self.subTest(scenario=scenario), \
                    patch("tool.deployment_channel.shutil.disk_usage",
                          side_effect=[Mock(free=value) for value in free_values]), \
                    self.assertRaisesRegex(ChannelError, message):
                receive_backup(receiver, parent, header, restore_root=self.storage["restores"])
            received = parent / backup.operation
            self.assertEqual((received / "manifest.json").exists(), metadata)
            self.assertEqual(any(received.glob("*.blob")) if received.exists() else False, blob)
            if blob:
                self.assertEqual(next(received.glob("*.blob")).stat().st_size, 0)

    def test_backup_receive_accepts_exact_space_for_transfer_and_same_filesystem_restore(self):
        source = self.root / "exact-space-source"
        source.mkdir(mode=0o700)
        (source / "data").write_bytes(b"abc")
        (source / "data").chmod(0o600)
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            backup = FileBackup.capture(self.storage["backups"], str(uuid4()), {"data": source},
                                        verify_quiescent=lambda: None)
        wire = io.BytesIO()
        send_backup(Channel(io.BytesIO(), wire), backup.path, backup.operation, backup.manifest_sha256)
        receiver = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
        header = receiver.receive()
        values = [BACKUP_HEADROOM + 8 * 1024**2, *([BACKUP_HEADROOM + 6] * 3)]
        with patch("tool.deployment_channel.shutil.disk_usage",
                   side_effect=[Mock(free=value) for value in values]):
            received, checksum = receive_backup(receiver, self.storage["incoming"], header,
                                                restore_root=self.storage["restores"])
        self.assertEqual(checksum, backup.manifest_sha256)
        self.assertEqual(portable_backup(received, backup.operation, checksum)["total_bytes"], 3)

    def test_backup_receive_checks_a_separate_restore_filesystem(self):
        source = self.root / "separate-space-source"
        source.mkdir(mode=0o700)
        (source / "data").write_bytes(b"abc")
        (source / "data").chmod(0o600)
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            backup = FileBackup.capture(self.storage["backups"], str(uuid4()), {"data": source},
                                        verify_quiescent=lambda: None)
        wire = io.BytesIO()
        send_backup(Channel(io.BytesIO(), wire), backup.path, backup.operation, backup.manifest_sha256)
        receiver = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
        header = receiver.receive()
        original_stat = Path.stat

        def stat_on_separate_device(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == self.storage["restores"]:
                fields = list(info)
                fields[2] += 1
                return os.stat_result(fields)
            return info

        with patch.object(Path, "stat", stat_on_separate_device), \
                patch("tool.deployment_channel.shutil.disk_usage",
                      side_effect=[Mock(free=BACKUP_HEADROOM + 8 * 1024**2),
                                   Mock(free=BACKUP_HEADROOM + 3),
                                   Mock(free=BACKUP_HEADROOM + 2)]), \
                self.assertRaisesRegex(ChannelError, "isolated restore space"):
            receive_backup(receiver, self.storage["incoming"], header, restore_root=self.storage["restores"])
        received = self.storage["incoming"] / backup.operation
        self.assertTrue((received / "manifest.json").exists())
        self.assertFalse(any(received.glob("*.blob")))

    def test_controller_rejects_unreviewed_mutation_and_wrong_scope(self):
        operation = str(uuid4())
        request = {"action": "deploy", "spec": self.spec, "selection": self.selection,
                   "operation": operation, "update": False}
        for response in (
            {"event": "images", "operation": operation, "intent_digest": "sha256:" + "a" * 64},
            {"event": "complete", "operation": operation},
            {"event": "review", "digest": "sha256:" + "a" * 64,
             "plan": {"operation": str(uuid4())}},
        ):
            wire = io.BytesIO()
            Channel(io.BytesIO(), wire).send(response)
            with self.subTest(response=response), self.assertRaises(ChannelError):
                controller(Channel(io.BytesIO(wire.getvalue()), io.BytesIO()), request,
                           self.storage["evidence"], lambda review: review["digest"], bundle=self.bundle)

    def test_explicit_credential_parser_and_service_access_are_validated_without_environment_mutation(self):
        from web.playback_status import PlaybackAgentConfig
        spec = {**self.spec, "uid": os.getuid(), "gid": os.getgid(), "require_playback": True}
        live = Path(spec["data_dir"])
        live.mkdir(mode=0o700)
        for name in ("config", "data", "logs", "secrets"):
            (live / name).mkdir(mode=0o700)
        token = live / "secrets/token"
        token.write_text("synthetic-token-" + "x" * 32)
        token.chmod(0o600)
        value = json.dumps([{"agent_id": "fixture", "url": "http://127.0.0.1:8081",
                             "token_file": "/run/secrets/token"}])
        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": "not-json"}), \
                patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            self.assertEqual(PlaybackAgentConfig.from_json(value)[0].agent_id, "fixture")
            validate_runtime_files(spec, {"RUSSOUND_PLAYBACK_AGENTS": value})
            self.assertEqual(os.environ["RUSSOUND_PLAYBACK_AGENTS"], "not-json")
            for invalid in ("", "two\nlines", "x" * 4097):
                token.write_text(invalid)
                with self.assertRaises(ValueError):
                    validate_runtime_files(spec, {"RUSSOUND_PLAYBACK_AGENTS": value})
            token.write_text("synthetic-token-" + "x" * 32)
            token.chmod(0o000)
            with self.assertRaises(ComposeError):
                validate_runtime_files(spec, {"RUSSOUND_PLAYBACK_AGENTS": value})
            token.chmod(0o600)

    def test_native_writer_and_outstanding_ansible_work_block_recovery(self):
        proc = self.root / "proc"
        proc.mkdir()
        process = proc / "123456789"
        process.mkdir()
        descriptors = process / "fd"
        descriptors.mkdir()
        (process / "fdinfo").mkdir()
        (process / "cmdline").write_bytes(b"python\0native-writer.py\0")
        target = self.root / "live"
        target.mkdir()
        (descriptors / "3").symlink_to(target / "state.json")
        (process / "fdinfo/3").write_text("flags:\t0100001\n")
        original = Path.iterdir
        def iterdir(path):
            return iter([process]) if path == Path("/proc") else original(path)
        with patch.object(Path, "iterdir", iterdir):
            with self.assertRaisesRegex(ComposeError, "native process"):
                check_host_writers(target)
            check_host_writers(target, check_files=False)
            (process / "cmdline").write_bytes(b"/usr/bin/python\0/opt/venv/bin/ansible-playbook\0play.yml\0")
            with self.assertRaisesRegex(ComposeError, "Ansible work"):
                check_host_writers(target, check_files=False)


@unittest.skipUnless(os.environ.get("RUSSOUND_RUN_DEPLOYMENT_TESTS") == "1",
                     "Opt in to disposable Linux Docker/Ansible remote deployment and recovery")
class ActualDevelopmentWorkflowTests(unittest.TestCase):
    def test_actual_deploy_unchanged_failure_and_offhost_recovery(self):
        from tool import deployment_build as builds
        docker = [shutil.which("docker") or "docker", "--context", builds.local_docker()]
        fixture_image = os.environ["RUSSOUND_TEST_DEPLOYMENT_IMAGE"]
        runtime_image = os.environ["RUSSOUND_TEST_WEB_IMAGE"]
        cached = json.loads(builds.command(docker + ["image", "inspect", runtime_image]))[0]
        platform = cached["Os"] + "/" + cached["Architecture"]
        with tempfile.TemporaryFile() as archive:
            builds.command(docker + ["image", "save", runtime_image], log=archive, timeout=600)
            archive.seek(0)
            builds.archive_identity(
                archive, {"tag": runtime_image, "reference": cached["Id"]}, platform, None)
        unique = uuid4().hex[:12]
        project = "deploy-fixture-" + unique
        network, volume, peer = project + "-net", project + "-data", project + "-gateway"
        tags = set()
        builds.command(docker + ["volume", "create", volume])
        mountpoint = json.loads(builds.command(docker + ["volume", "inspect", volume]))[0]["Mountpoint"]
        linux = Path(mountpoint)
        helper = docker + ["run", "--rm", "-i", "--network", "host",
                           "--env", "PYTHONPATH=/workspace",
                           "--volume", f"{ROOT}:/workspace:ro",
                           "--volume", "/var/run/docker.sock:/var/run/docker.sock",
                           "--volume", f"{volume}:{mountpoint}", fixture_image]
        builds.command(docker + ["network", "create", network])
        with tempfile.TemporaryDirectory(prefix="russound-workflow-") as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            server = None
            try:
                # External gateway is deliberately outside the deployed project.
                builds.command(docker + [
                    "run", "--detach", "--name", peer, "--network", network,
                    "--network-alias", "fixture-gateway", "--volume", f"{ROOT}:/workspace:ro",
                    "--workdir", "/workspace", "python:3.12-slim", "python", "-m",
                    "tool.dummy_backend.dummy_backend", "--serve", "--host", "0.0.0.0",
                    "--port", "6666", "--state", "/tmp/gateway-state.json"])
                baseline_peer = json.loads(builds.command(docker + ["inspect", peer]))[0]["Id"]
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                spec = spec_fixture(linux)
                spec.update(project=project, deployment="fixture", publish_port=port,
                            platform=platform, require_controller=True)
                spec["compose"] = str(linux / "compose.json")
                config = compose_fixture(spec, cached["Id"])
                config["services"]["russound-web"].update(command=None, entrypoint=None)
                config["networks"] = {"default": {"external": True, "name": network}}
                web = config["services"]["russound-web"]
                web["environment"] = {
                    "RUSSOUND_CONFIG": "/config/russound_config.json",
                    "RUSSOUND_STATE": "/data/russound_state.json",
                    "RUSSOUND_LOG_FILE": "/logs/web.log",
                    "RUSSOUND_BACKEND_HOST": "fixture-gateway", "RUSSOUND_BACKEND_PORT": "6666",
                }
                web["healthcheck"] = {
                    "test": ["CMD", "python", "-c",
                             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/',timeout=3)"],
                    "interval": "1s", "start_period": "1s",
                }
                setup = """
import json,os,sys
from pathlib import Path
root=Path(sys.argv[1]); spec=json.loads(sys.argv[2]); config=json.loads(sys.argv[3])
root.chmod(0o755)
for name in ('live','control'):
 p=root/name;p.mkdir(mode=0o755 if name=='live' else 0o700)
for name in ('config','data','logs','secrets'):
 p=root/'live'/name;p.mkdir(mode=0o700);os.chown(p,10001,10001)
p=root/'live/config/russound_config.json'
p.write_text(json.dumps({'backend':{'host':'fixture-gateway','port':6666,'poll_interval_seconds':1},
 'controllers':[{'id':1,'zone_count':2}],
 'zones':[{'name':'Fixture','controller':1,'zone':1}],
 'inputs':[{'id':1,'name':'Fixture'}],'notes':'preserve unknown fields'}))
p.chmod(0o600);os.chown(p,10001,10001)
p=root/'live/secrets/token';p.write_text('synthetic-retained-token');p.chmod(0o600);os.chown(p,10001,10001)
p=root/'compose.json';p.write_text(json.dumps(config));p.chmod(0o600)
"""
                builds.command(helper + ["python", "-c", setup, mountpoint, json.dumps(spec), json.dumps(config)])
                # Start the old stack so adoption and old-image capture are real.
                builds.command(helper + ["docker", "compose", "--env-file", "/dev/null", "-p", project,
                                         "-f", spec["compose"], "up", "-d", "--pull", "never",
                                         "--wait", "--wait-timeout", "60"])
                source = root / "source"
                source.mkdir()
                for args in (["init", "--quiet"], ["config", "user.email", "fixture@example.invalid"],
                             ["config", "user.name", "Fixture"]):
                    builds.command(["git", "-C", str(source), *args])
                (source / "Dockerfile").write_text(f"FROM {runtime_image}\nCOPY marker /tmp/marker\n")
                (source / "marker").write_text(unique)
                builds.command(["git", "-C", str(source), "add", "."])
                builds.command(["git", "-C", str(source), "commit", "--quiet", "-m", "Fixture"])
                revision = builds.command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
                artifacts = root / "artifacts"
                artifacts.mkdir(mode=0o700)
                bundle, checksum = builds.build(source, revision, artifacts, "external", platform)
                selection = select_bundle(bundle, checksum, "external", platform)
                tags.update(image["tag"] for image in selection["manifest"]["images"].values())
                evidence = root / "evidence"
                evidence.mkdir(mode=0o700)
                target_command = helper + ["python", "tests/integration/deployment_target.py", str(linux / "control")]
                # The exact controller SSH client talks to a real isolated sshd,
                # whose forced command confines all target work to Linux fixtures.
                host_key, client_key = root / "hostkey", root / "clientkey"
                for key in (host_key, client_key):
                    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
                authorized = root / "authorized_keys"
                authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
                authorized.chmod(0o600)
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    ssh_port = listener.getsockname()[1]
                ssh_config = root / "sshd_config"
                ssh_config.write_text(
                    f"Port {ssh_port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
                    f"PidFile {root / 'sshd.pid'}\nAuthorizedKeysFile {authorized}\n"
                    "PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
                    "PubkeyAuthentication yes\nStrictModes yes\nAllowTcpForwarding no\n"
                    "PermitTunnel no\nX11Forwarding no\nPermitTTY no\n"
                    f"AllowUsers {getpass.getuser()}\nForceCommand {shlex.join(target_command)}\n")
                trust = root / "known_hosts"
                trust.write_text(f"[127.0.0.1]:{ssh_port} " + host_key.with_suffix(".pub").read_text())
                trust.chmod(0o600)
                server_log = (root / "sshd.log").open("wb")
                self.addCleanup(server_log.close)
                server = subprocess.Popen([shutil.which("sshd") or "/usr/sbin/sshd", "-D", "-e",
                                           "-f", str(ssh_config)], stdout=server_log, stderr=server_log)
                deadline = time.monotonic() + 10
                while True:
                    self.assertIsNone(server.poll())
                    try:
                        with socket.create_connection(("127.0.0.1", ssh_port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() > deadline:
                            self.fail("Isolated sshd readiness deadline exceeded")
                        time.sleep(0.1)
                def invoke(request, *, backup=None, backup_checksum=None, expect_failure=False):
                    args = argparse.Namespace(
                        evidence=evidence, known_hosts=trust, host="127.0.0.1", user=getpass.getuser(),
                        port=ssh_port, identity=client_key, bundle=bundle,
                        backup=backup, backup_sha256=backup_checksum)
                    try:
                        with patch("tool.deployment_operator.interactive", side_effect=lambda review: review["digest"]):
                            result = remote(args, request)
                        self.assertFalse(expect_failure, "Expected target failure did not occur")
                        return result
                    except (ChannelError, OSError):
                        if expect_failure:
                            return None
                        prefix = request["operation"] + (".recovery" if request["action"] == "recover" else "")
                        self.fail((evidence / (prefix + ".ssh.log")).read_text())
                operation = str(uuid4())
                request = {"action": "deploy", "spec": spec, "selection": selection,
                           "operation": operation, "update": False}
                trusted_hosts = trust
                wrong = root / "wrong_known_hosts"
                wrong.write_text(f"[127.0.0.1]:{ssh_port} " + client_key.with_suffix(".pub").read_text())
                wrong.chmod(0o600)
                trust = wrong
                self.assertIsNone(invoke({**request, "operation": str(uuid4())}, expect_failure=True))
                trust = trusted_hosts
                result = invoke(request)
                self.assertEqual(result["event"], "complete")
                self.assertEqual(set(result["required"].values()), {"passed"})
                offhost = evidence / operation
                identity_record = json.loads((evidence / (operation + ".backup-identity.json")).read_bytes())
                recovery_assets(offhost, operation, identity_record["manifest_sha256"])
                ids = builds.command(docker + ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project])
                rerun = invoke({**request, "selection": None, "operation": str(uuid4())})
                self.assertEqual(rerun["event"], "unchanged")
                self.assertEqual(ids, builds.command(docker + [
                    "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]))
                self.assertEqual(json.loads(builds.command(docker + ["inspect", peer]))[0]["Id"], baseline_peer)
                # Change only the selected source's command, not the approved layout.
                (source / "Dockerfile").write_text(
                    f"FROM {runtime_image}\nCOPY marker /tmp/marker\n"
                    'CMD ["python","-c","raise SystemExit(17)"]\n')
                builds.command(["git", "-C", str(source), "add", "."])
                builds.command(["git", "-C", str(source), "commit", "--quiet", "-m", "Unhealthy fixture"])
                revision = builds.command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
                bundle, checksum = builds.build(source, revision, artifacts, "external", platform)
                selection = select_bundle(bundle, checksum, "external", platform)
                tags.update(image["tag"] for image in selection["manifest"]["images"].values())
                failed_operation = str(uuid4())
                failed_request = {**request, "selection": selection, "operation": failed_operation, "update": True}
                self.assertIsNone(invoke(failed_request, expect_failure=True))
                read_state = [
                    "python", "-c", "import sys;from pathlib import Path;"
                    "print((Path(sys.argv[1])/'control/state/fixture.json').read_text())", mountpoint]
                state = json.loads(builds.command(helper + read_state))
                self.assertEqual(state["committed"]["selection"]["operation"], operation)
                self.assertEqual(state["pending"]["operation"], failed_operation)
                self.assertEqual(state["pending"]["phase"], "backup-complete")
                failed_identity = json.loads((evidence / (failed_operation + ".backup-identity.json")).read_bytes())
                # Preserve edits made after the stopped-writer backup.
                builds.command(helper + [
                    "python", "-c", "import sys,os;from pathlib import Path;"
                    "p=Path(sys.argv[1])/'live/data/post-failure';p.write_text('operator edit');"
                    "p.chmod(0o600);os.chown(p,10001,10001)", mountpoint])
                recovered = invoke(
                    {"action": "recover", "deployment": "fixture", "operation": failed_operation},
                    backup=evidence / failed_operation,
                    backup_checksum=failed_identity["manifest_sha256"])
                self.assertEqual(recovered["event"], "recovered")
                state = json.loads(builds.command(helper + read_state))
                self.assertIsNone(state["pending"])
                self.assertEqual(state["committed"]["selection"]["operation"], operation)
                preserved = builds.command(helper + [
                    "python", "-c", "import sys;from pathlib import Path;r=Path(sys.argv[1]);"
                    "p=list(r.glob('live.failed-*/data/post-failure'));assert len(p)==1;"
                    "assert p[0].read_text()=='operator edit';"
                    "assert not (r/'live/data/post-failure').exists();"
                    "assert (r/'live/secrets/token').read_text()=='synthetic-retained-token';"
                    "print('preserved')", mountpoint])
                self.assertIn(b"preserved", preserved)
                self.assertEqual(json.loads(builds.command(docker + ["inspect", peer]))[0]["Id"], baseline_peer)
                # Recovery itself is one-shot: no automatic replay can mutate data.
                recovery_replay = {"action": "recover", "deployment": "fixture", "operation": failed_operation}
                self.assertIsNone(invoke(recovery_replay, backup=evidence / failed_operation,
                                         backup_checksum=failed_identity["manifest_sha256"], expect_failure=True))
                # Lost off-host acknowledgement blocks apply; an explicitly
                # approved pre-apply resolution restarts only the original IDs.
                interrupted_operation = str(uuid4())
                interrupted = {**failed_request, "operation": interrupted_operation}
                with patch("tool.deployment_operator.recovery_assets",
                           side_effect=ChannelError("synthetic lost backup acknowledgement")):
                    self.assertIsNone(invoke(interrupted, expect_failure=True))
                state = json.loads(builds.command(helper + read_state))
                self.assertEqual(state["pending"]["phase"], "inputs-verified")
                resumed = invoke({"action": "resume-original", "deployment": "fixture",
                                  "operation": interrupted_operation})
                self.assertEqual(resumed["event"], "recovered")
                state = json.loads(builds.command(helper + read_state))
                self.assertIsNone(state["pending"])
                self.assertEqual(state["committed"]["selection"]["operation"], operation)
                # Separate prepared first installation: rollback must restore
                # absence, not manufacture passing HTTP/hardware acceptance.
                first_spec = {**spec, "deployment": "first", "project": project + "-first",
                              "data_dir": str(linux / "first-live"),
                              "compose": str(linux / "first-compose.json"), "adopt": False}
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    first_spec["publish_port"] = listener.getsockname()[1]
                first_config = compose_fixture(first_spec, cached["Id"])
                first_config["networks"] = config["networks"]
                first_web = first_config["services"]["russound-web"]
                first_web["environment"] = web["environment"]
                first_web["healthcheck"] = web["healthcheck"]
                prepare_first = (
                    "import sys,shutil,json,os;from pathlib import Path;r=Path(sys.argv[1]);"
                    "shutil.copytree(r/'live',r/'first-live',copy_function=shutil.copy2);"
                    "[(os.chown(p,10001,10001)) for p in (r/'first-live').rglob('*')];"
                    "p=r/'first-compose.json';p.write_text(sys.argv[2]);p.chmod(0o600)")
                builds.command(helper + ["python", "-c", prepare_first, mountpoint, json.dumps(first_config)])
                first_operation = str(uuid4())
                self.assertIsNone(invoke({"action": "deploy", "spec": first_spec, "selection": selection,
                                         "operation": first_operation, "update": False}, expect_failure=True))
                first_identity = json.loads((evidence / (first_operation + ".backup-identity.json")).read_bytes())
                absent = invoke({"action": "recover", "deployment": "first", "operation": first_operation},
                                backup=evidence / first_operation,
                                backup_checksum=first_identity["manifest_sha256"])
                self.assertEqual(absent["required"], {
                    "containers-absent": "passed", "prepared-data-restored": "passed"})
                self.assertFalse(builds.command(docker + [
                    "ps", "-aq", "--filter", "label=com.docker.compose.project=" + first_spec["project"]]).strip())
                status = json.loads(builds.command(helper + [
                    "python", "-c", "import sys,os,json;from pathlib import Path;"
                    "from tool.deployment_state import StateStore;"
                    "print(json.dumps(StateStore(Path(sys.argv[1])/'control/state').status()))", mountpoint]))
                self.assertFalse(status["blocked"])
                self.assertNotIn("first", status["deployments"])
                self.assertEqual(json.loads(builds.command(docker + ["inspect", peer]))[0]["Id"], baseline_peer)
            finally:
                if server is not None:
                    server.terminate()
                    server.wait(timeout=10)
                for owned_project in (project, project + "-first"):
                    ids = builds.command(docker + [
                        "ps", "-aq", "--filter", "label=com.docker.compose.project=" + owned_project])
                    for container in ids.decode().split():
                        builds.command(docker + ["rm", "-f", container])
                if builds.command(docker + ["ps", "-aq", "--filter", "name=^/" + peer + "$"]).strip():
                    builds.command(docker + ["rm", "-f", peer])
                builds.command(docker + ["network", "rm", network])
                builds.command(docker + ["volume", "rm", volume])
                present = builds.command(docker + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
                for tag in tags & set(present.decode().split()):
                    builds.command(docker + ["image", "rm", tag])
                for tag in present.decode().split():
                    if tag.startswith("mpdrussoundpi-recovery/web:") and tag.split(":")[1] in {
                            operation if "operation" in locals() else "",
                            failed_operation if "failed_operation" in locals() else "",
                            interrupted_operation if "interrupted_operation" in locals() else ""}:
                        builds.command(docker + ["image", "rm", tag])


if __name__ == "__main__":
    unittest.main()
