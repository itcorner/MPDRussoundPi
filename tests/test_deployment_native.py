import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import yaml

from tool import deployment_native as native
from tool import deployment_native_source as source
from tool.deployment_backup import FileBackup
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import ComposeDeployment, ComposeError, encoded, paths, private_write
from tool.deployment_files import checked_directory
from tool.deployment_operator import controller, serve
from tool.deployment_state import PHASES, StateError, StateStore, candidate


ROOT = Path(__file__).resolve().parents[1]
INTENT = "sha256:" + "d" * 64


def selection_fixture():
    return {"schema_version": 1, "kind": source.NATIVE_CANDIDATE, "release": str(uuid4()),
            "revision": "a" * 40, "sha256": "b" * 64,
            "runtime": {"python": "c" * 64, "ser2net": "d" * 64,
                        "version": "3.13.5", "machine": "aarch64"}}


def spec_fixture(root):
    return {"schema_version": 1, "deployment": "fixture", "data_dir": str(root / "live"),
            "uid": max(os.getuid(), 1000), "gid": os.getgid(), "publish_host": "127.0.0.1",
            "publish_port": 18000, "serial_device": "/dev/ttyUSB0", "serial_gid": 20,
            "serial_options": "19200n81", "backend_port": 16666,
            "playback_agents": [{"agent_id": "musicpi", "url": "https://192.0.2.1:8443",
                                 "token_file": "/run/secrets/token", "tls_ca_file": "/run/secrets/ca.crt"}],
            "from_compose": None}


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.spec = spec_fixture(self.root)
        self.selection = selection_fixture()

    def test_native_candidate_has_source_identity_not_image_shaped_metadata(self):
        candidate(self.selection)
        for key, value in (("revision", "main"), ("sha256", "bad"), ("release", "../release"),
                           ("runtime", {}), ("schema_version", True)):
            with self.subTest(key=key), self.assertRaises((source.NativeError, StateError)):
                candidate({**self.selection, key: value})
        with self.assertRaises(StateError):
            candidate({**self.selection, "images": {}})

    def test_native_spec_rejects_ambiguous_identity_expansion_and_privileged_ports(self):
        native.specification(self.spec)
        for key, value in (("uid", 0), ("uid", True), ("gid", -1), ("serial_gid", None),
                           ("data_dir", "/"), ("data_dir", "/home/a/../b"), ("data_dir", "/home/a b"),
                           ("data_dir", "/home/a%u"), ("data_dir", "/home/a//b"),
                           ("serial_device", "/tmp/device"), ("publish_host", "localhost"),
                           ("publish_host", "::"), ("publish_host", "224.0.0.1"),
                           ("publish_port", 80), ("backend_port", True), ("serial_options", "19200n81\ninject"),
                           ("backend_port", self.spec["publish_port"]), ("playback_agents", [])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                native.specification({**self.spec, key: value})

    def test_units_use_fixed_unprivileged_scope_loopback_gateway_and_host_secrets(self):
        operation = str(uuid4())
        units = native.unit_files(self.spec, self.selection, operation)
        self.assertEqual(units, native.unit_files(json.loads(json.dumps(self.spec, sort_keys=True)),
                                                  self.selection, operation))
        web, gateway = units[native.WEB].decode(), units[native.GATEWAY].decode()
        self.assertIn(f"/{self.selection['release']}/venv/bin/python", web)
        self.assertIn(f"--state {self.spec['data_dir']}/data/russound_state.json", web)
        self.assertIn("--no-debug", web)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", web)
        self.assertIn("RUSSOUND_BACKEND_HOST=127.0.0.1", web)
        self.assertNotIn("/run/secrets/", web)
        self.assertIn(f"{self.spec['data_dir']}/secrets/token", web)
        self.assertIn(f"User={self.spec['uid']}", gateway)
        self.assertIn("SupplementaryGroups=20", gateway)
        self.assertIn(f"Requires={native.GATEWAY}", web)
        parsed = yaml.safe_load(native.gateway_config(self.spec))
        self.assertEqual(parsed["connection"]["accepter"], "tcp,127.0.0.1,16666")
        self.assertEqual(parsed["connection"]["connector"], "serialdev,/dev/ttyUSB0,19200n81,local")
        previous = yaml.safe_load((ROOT / "mpdrussoundpi/ser2net/ser2net.yaml").read_text())["connection"]
        self.assertEqual(parsed["connection"].get("options"), previous["options"])
        self.assertIs(parsed["connection"].get("enable"), True)
        for text in (web, gateway):
            self.assertNotIn("mpd_", text)
            self.assertNotIn("pipewire", text)
            self.assertNotIn("playback-agent.service", text)

    def test_secret_mapping_rejects_escape_and_does_not_change_requested_registry(self):
        before = copy.deepcopy(self.spec)
        native.environment(self.spec, native=True)
        self.assertEqual(self.spec, before)
        for path in ("/tmp/token", "/run/secrets/../token"):
            spec = copy.deepcopy(self.spec)
            spec["playback_agents"][0]["token_file"] = path
            with self.subTest(path=path), self.assertRaises(source.NativeError):
                native.environment(spec, native=True)

    def test_staging_play_is_single_host_revision_pinned_and_nonactivating(self):
        play = yaml.safe_load((ROOT / "ansible/playbooks/stage-native-deployment.yml").read_text())[0]
        checks = play["pre_tasks"][0]["ansible.builtin.assert"]["that"]
        self.assertIn("ansible_play_hosts_all | length == 1", checks)
        self.assertIn("'stage' in ansible_run_tags", checks)
        self.assertIn("native_application_revision is match('^[a-f0-9]{40}$')", checks)
        self.assertIn("not ansible_check_mode", checks)
        text = json.dumps(play)
        for forbidden in ("ansible.builtin.apt", "ansible.builtin.systemd", "state: restarted"):
            self.assertNotIn(forbidden, text)
        self.assertIn("tool.deployment_native_source", text)

    def test_source_seal_detects_changed_bytes_modes_membership_and_symlinks(self):
        release = self.root / "release"
        release.mkdir(mode=0o755)
        file = release / "source.py"
        file.write_text("original")
        with patch("tool.deployment_backup.os.listxattr", return_value=[], create=True):
            original = source.tree_digest(release, owner=os.getuid())
            file.write_text("changed")
            self.assertNotEqual(source.tree_digest(release, owner=os.getuid()), original)
            file.write_text("original")
            file.chmod(0o755)
            self.assertNotEqual(source.tree_digest(release, owner=os.getuid()), original)
            file.chmod(0o644)
            (release / "link").symlink_to("source.py")
            with self.assertRaises(ValueError):
                source.tree_digest(release, owner=os.getuid())

    def test_source_runtime_mismatch_is_not_treated_as_unchanged(self):
        with patch.object(source, "runtime_identity", return_value={}), \
                patch.object(source, "tree_digest") as tree:
            with self.assertRaisesRegex(source.NativeError, "changed"):
                source.verify_release(self.selection, releases=self.root)
        tree.assert_not_called()

    def test_public_release_permissions_work_with_private_umask_without_following_links(self):
        release = self.root / "release"
        release.mkdir(mode=0o700)
        code = release / "code.py"
        code.write_text("public code")
        code.chmod(0o600)
        executable = release / "python"
        executable.write_text("synthetic executable")
        executable.chmod(0o700)
        source.publish_release(release, owner=os.getuid())
        self.assertEqual(release.stat().st_mode & 0o777, 0o755)
        self.assertEqual(code.stat().st_mode & 0o777, 0o644)
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        (release / "link").symlink_to(code)
        with self.assertRaises(source.NativeError):
            source.publish_release(release, owner=os.getuid())

    def test_native_state_uses_shared_host_exclusion_and_sequential_completion(self):
        store = StateStore(self.root / "state", owner_uid=os.getuid())
        store.initialize()
        operation = str(uuid4())
        store.begin("fixture", operation, self.selection, INTENT, list(native.CHECKS), expected_committed=None)
        with self.assertRaises(StateError):
            store.begin("other", str(uuid4()), self.selection, INTENT, list(native.CHECKS))
        for phase in PHASES[1:]:
            store.advance("fixture", operation, phase, **({"backup": operation} if phase == "backup-complete" else {}))
        store.finalize("fixture", operation, {
            "operation": operation, "intent_digest": INTENT, "invocation": "verified-complete",
            "required": dict.fromkeys(native.CHECKS, "passed")})
        self.assertFalse(store.status()["blocked"])
        with self.assertRaisesRegex(StateError, "changed after review"):
            store.begin("fixture", str(uuid4()), self.selection, INTENT, list(native.CHECKS),
                        expected_committed=None)
        self.assertTrue(store.status()["blocked"])

    def test_native_recovery_cannot_use_container_absence_receipt(self):
        store = StateStore(self.root / "state", owner_uid=os.getuid())
        store.initialize()
        operation = str(uuid4())
        store.begin("fixture", operation, self.selection, INTENT, list(native.CHECKS))
        with self.assertRaises(StateError):
            store.resolve_recovery("fixture", operation, {
                "operation": operation, "review_digest": INTENT,
                "required": {"containers-absent": "passed", "prepared-data-restored": "passed"}})
        self.assertTrue(store.status()["blocked"])

    def test_compose_entrypoint_explicitly_rejects_native_sources(self):
        from test_deployment_operator import spec_fixture as compose_spec
        storage = paths(self.root)
        with self.assertRaisesRegex(ComposeError, "native deployment workflow"):
            ComposeDeployment(compose_spec(self.root), self.selection, storage)

    def test_compose_exports_can_be_bound_to_a_separate_native_recovery_operation(self):
        from test_deployment_build import image_archive
        for separate in (False, True):
            operation = str(uuid4())
            recovery = str(uuid4()) if separate else operation
            tag = f"mpdrussoundpi-recovery/web:{recovery}"
            archive = self.root / (recovery + ".tar")
            reference = image_archive(archive, tag, "a" * 40)
            target = Mock()
            target.operation = operation
            target.asset = self.root / operation
            target.asset.mkdir(mode=0o700)
            target.spec = {"platform": "linux/arm64"}
            target.services = {"russound-web": "web"}
            target.compose.return_value = ["compose"]
            target.baseline = [{"image": reference,
                                "config": {"Labels": {"com.docker.compose.service": "russound-web"}}}]
            def export(argv, *, output=None, **kwargs):
                if argv[:2] == ["image", "save"]:
                    output.write(archive.read_bytes())
                return b""
            target.command.side_effect = export
            ComposeDeployment.stop(target, **({"recovery_operation": recovery} if separate else {}))
            stored = json.loads((target.asset / "images.json").read_bytes())
            self.assertEqual(stored["web"]["tag"], tag)

    def test_controller_rejects_native_scope_substitution_before_approval(self):
        operation = str(uuid4())
        request = {"action": "native-deploy", "spec": self.spec, "selection": self.selection,
                   "operation": operation, "update": True}
        event = {"event": "review", "digest": INTENT, "plan": {
            "kind": "native-deployment-review", "operation": operation, "deployment": "fixture",
            "selection": self.selection, "scope": {
                "listener": {"host": "0.0.0.0", "port": self.spec["publish_port"]}}}}
        source_stream = io.BytesIO()
        Channel(io.BytesIO(), source_stream).send(event)
        source_stream.seek(0)
        approve = Mock()
        with self.assertRaises(ChannelError):
            controller(Channel(source_stream, io.BytesIO()), request, self.root, approve)
        approve.assert_not_called()

    def test_serve_routes_native_requests_to_reviewed_runner_only(self):
        request = {"action": "native-deploy", "spec": self.spec, "selection": self.selection,
                   "operation": str(uuid4()), "update": True}
        channel = Mock()
        channel.receive.return_value = request
        with patch.object(native, "NativeDeployment") as target:
            target.return_value.execute.return_value = {"event": "unchanged"}
            serve(channel, {})
        target.assert_called_once_with(self.spec, self.selection, {}, operation=request["operation"], update=True)
        channel.send.assert_called_once_with({"event": "unchanged"})


class NativeTransactionTests(unittest.TestCase):
    """Real state/backup/restore with controlled service and privilege boundaries."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-transaction-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.storage = paths(self.root)
        self.store = StateStore(self.storage["state"], owner_uid=os.getuid())
        self.store.initialize()
        self.spec = spec_fixture(self.root)
        self.spec["uid"] = os.getuid() or 1000
        self.live = Path(self.spec["data_dir"])
        self.live.mkdir(mode=0o700)
        for name in ("config", "data", "secrets", "logs"):
            (self.live / name).mkdir(mode=0o700)
        self.state_file = self.live / "data/russound_state.json"
        self.state_file.write_text('{"saved":"original"}')
        self.state_file.chmod(0o600)
        for name in ("releases", "native-runtime", "units", "offhost"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "native-runtime").chmod(0o755)
        self.running = False
        self.events = []
        self.fail_runtime = False
        patches = [
            patch.object(native, "RELEASES", self.root / "releases"),
            patch.object(native, "RUNTIME", self.root / "native-runtime"),
            patch.object(native, "UNITS", self.root / "units"),
            patch.object(native, "command", side_effect=self.command),
            patch.object(native, "runtime_checks", side_effect=self.checks),
            patch.object(native, "verify_release", side_effect=lambda selection: self.root / "releases" / selection["release"]),
            patch.object(native, "write_unit", side_effect=self.write_unit),
            patch.object(native, "check_port"),
            patch.object(native, "check_no_writers", side_effect=lambda spec: self.quiet()),
            patch.object(native, "check_host_writers"),
            patch.object(native, "system_units", side_effect=self.units),
            patch.object(native, "safe_path", side_effect=self.safe_path),
            patch.object(native, "checked_directory", side_effect=lambda path, owner, **kw: checked_directory(path, os.getuid(), **kw)),
            patch("tool.deployment_backup.os.listxattr", return_value=[], create=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def select(self):
        selection = selection_fixture()
        release = self.root / "releases" / selection["release"]
        release.mkdir(mode=0o755)
        (release / "code").write_text(selection["release"])
        selection["sha256"] = source.tree_digest(release, owner=os.getuid())
        return selection

    def prepare(self, selection=None):
        target = native.NativeDeployment(self.spec, selection or self.select(), self.storage,
                                         operation=str(uuid4()), update=True)
        def preflight():
            self.events.append("preflight")
            target.backup_estimate = 1000
            baseline = {"origin": "compose" if target.compose else "native" if target.previous_native else "prepared",
                        "units": self.units(), "previous_native": target.previous_native,
                        "committed": target.previous,
                        "compose_asset": str(target.compose.asset) if target.compose else None}
            if target.baseline is not None and target.baseline != baseline:
                raise source.NativeError("baseline changed")
            target.baseline = baseline
        target.preflight = preflight
        target.quiescent = self.quiet
        return target

    def command(self, argv, **kwargs):
        self.events.append(" ".join(argv[:3]))
        if argv[:2] == ["systemctl", "stop"]:
            self.running = False
        if argv[:3] == ["systemctl", "enable", "--now"] or argv[:2] == ["systemctl", "start"]:
            self.running = True
        return b""

    def quiet(self):
        if self.running:
            raise source.NativeError("writers active")

    def write_unit(self, name, content, operation):
        (self.root / "units" / name).write_bytes(content)

    def units(self):
        return {name: {"LoadState": "loaded" if (self.root / "units" / name).exists() else "not-found",
                       "ActiveState": "active" if self.running else "inactive", "MainPID": "10" if self.running else "0"}
                for name in (native.WEB, native.GATEWAY)}

    def safe_path(self, path):
        info = list(path.lstat())
        info[4] = 0
        return os.stat_result(info)

    def checks(self, spec, selection, operation):
        self.events.append("checks")
        self.assertTrue(self.running)
        for name, content in native.unit_files(spec, selection, operation).items():
            self.assertEqual((self.root / "units" / name).read_bytes(), content)
        if self.fail_runtime:
            self.state_file.write_text('{"saved":"post-backup edit"}')
            raise source.NativeError("runtime failure")
        return dict.fromkeys(native.CHECKS, "passed")

    def offhost(self, backup):
        self.events.append("offhost")
        self.quiet()
        destination = self.root / "offhost" / backup.operation
        shutil.copytree(backup.path, destination)
        self.backup = destination, backup.manifest_sha256
        native.recovery_assets(destination, backup.operation, backup.manifest_sha256)

    def execute(self, target):
        return target.execute(lambda review: review["digest"], self.offhost)

    def recover(self, target, approve=None):
        path, checksum = self.backup
        return native.recover(self.storage, "fixture", target.operation, path, checksum,
                              approve or (lambda review: review["digest"]))

    def test_declined_approval_never_reserves_or_stops(self):
        target = self.prepare()
        with self.assertRaises(source.NativeError):
            target.execute(lambda review: None, self.offhost)
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.events, ["preflight"])
        self.assertFalse(self.running)

    def test_input_or_runtime_drift_after_review_does_not_stop_writers(self):
        target = self.prepare()
        def drift(review):
            self.running = True
            return review["digest"]
        with self.assertRaises(source.NativeError):
            target.execute(drift, self.offhost)
        self.assertFalse(self.store.status()["blocked"])
        self.assertTrue(self.running)
        self.assertFalse(any("stop" in event for event in self.events))

    def test_successful_transaction_orders_backup_before_apply_and_finalizes(self):
        target = self.prepare()
        result = self.execute(target)
        self.assertEqual(result["event"], "complete")
        self.assertLess(self.events.index("offhost"), self.events.index("systemctl daemon-reload"))
        self.assertLess(self.events.index("systemctl enable --now"), self.events.index("checks"))
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.state_file.read_text(), '{"saved":"original"}')
        committed = self.store.status()["deployments"]["fixture"]["committed"]
        self.assertEqual(committed["selection"]["candidate"], target.selection)
        self.assertFalse((self.storage["state"] / "reservation").exists())
        with self.assertRaises(source.NativeError):
            self.execute(target)

    def test_offhost_failure_keeps_pending_and_never_installs_or_starts_candidate(self):
        target = self.prepare()
        def failed_backup(backup):
            raise source.NativeError("controller storage unavailable")
        with self.assertRaises(source.NativeError):
            target.execute(lambda review: review["digest"], failed_backup)
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse(self.running)
        self.assertEqual(list((self.root / "units").iterdir()), [])
        self.assertFalse((self.storage["evidence"] / (target.operation + ".apply-attempt.json")).exists())

    def test_backup_failure_can_resume_original_without_discarding_data(self):
        target = self.prepare()
        with self.assertRaises(source.NativeError):
            target.execute(lambda review: review["digest"], Mock(side_effect=source.NativeError("offline")))
        self.state_file.write_text("operator edit before retry")
        result = native.resume_original(self.storage, "fixture", target.operation,
                                        lambda review: review["digest"])
        self.assertEqual(result["event"], "recovered")
        self.assertEqual(self.state_file.read_text(), "operator edit before retry")
        self.assertFalse(self.store.status()["blocked"])

    def test_runtime_failure_requires_explicit_recovery_and_preserves_post_backup_data(self):
        target = self.prepare()
        self.fail_runtime = True
        with self.assertRaises(source.NativeError):
            self.execute(target)
        self.assertTrue(self.store.status()["blocked"])
        with self.assertRaises(source.NativeError):
            native.resume_original(self.storage, "fixture", target.operation, lambda review: review["digest"])
        with self.assertRaises(source.NativeError):
            self.recover(target, lambda review: None)
        self.assertTrue(self.running)
        result = self.recover(target)
        self.assertEqual(result["event"], "recovered")
        self.assertFalse(self.running)
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.state_file.read_text(), '{"saved":"original"}')
        retained = list(self.root.glob("live.failed-*"))
        self.assertEqual(len(retained), 1)
        self.assertIn("post-backup edit", (retained[0] / "data/russound_state.json").read_text())

    def test_successful_native_trial_can_be_explicitly_rolled_back(self):
        target = self.prepare()
        self.execute(target)
        self.state_file.write_text("new state after native commit")
        result = self.recover(target)
        self.assertEqual(result["event"], "recovered")
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"], {})
        self.assertEqual(self.state_file.read_text(), '{"saved":"original"}')
        with self.assertRaises(source.NativeError):
            self.recover(target)

    def test_same_candidate_rerun_checks_without_backup_restart_or_new_commit(self):
        target = self.prepare()
        self.execute(target)
        committed = self.store.status()
        self.events.clear()
        rerun = self.prepare(target.selection)
        result = self.execute(rerun)
        self.assertEqual(result["event"], "unchanged")
        self.assertEqual(self.store.status(), committed)
        self.assertNotIn("offhost", self.events)
        self.assertFalse(any("systemctl" in event for event in self.events))

    def test_partial_unit_install_is_removed_even_before_systemd_loaded_it(self):
        target = self.prepare()
        def partial_write(name, content, operation):
            if name == native.GATEWAY:
                raise source.NativeError("injected partial unit install")
            self.write_unit(name, content, operation)
        with patch.object(native, "write_unit", side_effect=partial_write), self.assertRaises(source.NativeError):
            self.execute(target)
        self.assertTrue((self.root / "units" / native.WEB).exists())
        not_loaded = {name: {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}
                      for name in (native.WEB, native.GATEWAY)}
        with patch.object(native, "system_units", return_value=not_loaded):
            self.recover(target)
        self.assertFalse((self.root / "units" / native.WEB).exists())
        self.assertFalse(self.store.status()["blocked"])

    def test_native_update_backup_contains_prior_environment_and_recovery_restores_it(self):
        original = self.prepare()
        self.execute(original)
        first_commit = copy.deepcopy(self.store.status()["deployments"]["fixture"]["committed"])
        self.events.clear()
        self.fail_runtime = True
        update = self.prepare()
        with self.assertRaises(source.NativeError):
            self.execute(update)
        self.assertLess(self.events.index("systemctl stop " + native.WEB), self.events.index("offhost"))
        self.fail_runtime = False
        self.recover(update)
        self.assertTrue(self.running)
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], first_commit)
        self.assertTrue(list((self.root / "releases").glob("*.failed-*")))

    def test_corrupted_backup_is_rejected_before_stopping_or_creating_recovery_attempt(self):
        target = self.prepare()
        self.execute(target)
        path, _ = self.backup
        blob = next(path.glob("*.blob"))
        blob.chmod(0o600)
        blob.write_text("corrupt")
        blob.chmod(0o400)
        self.events.clear()
        with self.assertRaises(ValueError):
            self.recover(target)
        self.assertTrue(self.running)
        self.assertEqual(self.events, [])
        self.assertFalse((self.storage["evidence"] / (target.operation + ".recovery-attempt.json")).exists())

    def test_cutover_image_backup_uses_native_operation_and_rollback_restores_docker_record(self):
        from test_deployment_build import image_archive
        from test_deployment_images import FixtureRegistry
        from tool.deployment_images import resolve
        from tool import deployment_compose, deployment_recovery
        case = self
        self.docker_running = True
        images = {name: image_archive(self.root / (name + "-fixture.tar"), "fixture/" + name + ":old", "a" * 40)
                  for name in ("web", "ser2net")}
        baseline = [{"id": name, "image": image,
                     "config": {"Labels": {"com.docker.compose.service": "russound-" + name}},
                     "host": {"RestartPolicy": {"Name": "unless-stopped"}}} for name, image in images.items()]
        config = {"name": "fixture", "services": {
            "russound-web": {"image": images["web"]},
            "russound-ser2net": {"image": images["ser2net"], "environment": {"RUSSOUND_SERIAL_OPTIONS": "19200n81"}}}}
        compose_file = self.root / "compose-input.json"
        private_write(compose_file, config)
        self.spec["from_compose"] = {
            "schema_version": 1, "deployment": "fixture", "project": "fixture", "compose": str(compose_file),
            **{key: self.spec[key] for key in ("data_dir", "uid", "gid", "publish_host", "publish_port",
                                               "serial_device", "serial_gid")},
            "gateway": "managed", "platform": "linux/arm64",
            "require_controller": True, "require_playback": True, "adopt": True}
        old_selection = resolve("managed", "linux/arm64", FixtureRegistry())
        old_operation = str(uuid4())
        required = ["containers", "http", "backend", "playback"]
        self.store.begin("fixture", old_operation, old_selection, INTENT, required)
        for phase in PHASES[1:]:
            self.store.advance("fixture", old_operation, phase,
                               **({"backup": old_operation} if phase == "backup-complete" else {}))
        self.store.finalize("fixture", old_operation, {
            "operation": old_operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(required, "passed")})
        previous = copy.deepcopy(self.store.status()["deployments"]["fixture"]["committed"])

        class FakeCompose:
            def __init__(self, spec, selection, storage, operation=None, **kwargs):
                self.spec = copy.deepcopy(spec)
                self.operation = operation or str(uuid4())
                self.asset = storage["assets"] / self.operation
                self.asset.mkdir(mode=0o700)
                self.baseline = copy.deepcopy(baseline)
                self.services = {"russound-web": "web", "russound-ser2net": "ser2net"}
                private_write(self.asset / "spec.json", spec)
                private_write(self.asset / "compose.json", config)

            def stop(self, *, recovery_operation=None):
                case.docker_running = False
                archives = {}
                for name, image in images.items():
                    tag = f"mpdrussoundpi-recovery/{name}:{recovery_operation or self.operation}"
                    path = self.asset / (name + ".tar")
                    image_archive(path, tag, "a" * 40)
                    archives[name] = {"tag": tag, "reference": image, "config": image,
                                      "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "bytes": path.stat().st_size, "archive": path.name}
                private_write(self.asset / "images.json", archives)

            def inspect_all(self):
                return [{"Id": item["id"], "Image": item["image"], "Config": item["config"]} for item in baseline]

            def own(self, containers):
                return containers

            def current_signature(self, containers):
                return baseline

            def compose(self):
                return ["compose"]

            def command(self, argv, **kwargs):
                case.docker_running = False
                return b""

            def quiescent(self):
                case.quiet()
                if case.docker_running:
                    raise source.NativeError("Docker writer active")

        def apply(path, loaded):
            self.assertFalse(self.running)
            self.assertFalse(self.docker_running)
            self.assertEqual(loaded, images)
            self.docker_running = True

        def check_original(target, loaded):
            self.assertTrue(self.docker_running)
            self.assertFalse(self.running)
            self.assertEqual(self.state_file.read_text(), '{"saved":"original"}')
            return dict.fromkeys(required, "passed")

        with patch.object(native, "ComposeDeployment", FakeCompose), \
                patch.object(deployment_recovery, "load_recovery_images", return_value=images), \
                patch.object(deployment_compose, "apply", side_effect=apply), \
                patch.object(deployment_compose, "runtime_checks", side_effect=check_original):
            target = self.prepare()
            self.execute(target)
            self.assertFalse(self.docker_running)
            self.assertTrue(self.running)
            self.recover(target)
        self.assertTrue(self.docker_running)
        self.assertFalse(self.running)
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], previous)


class NativeHTTPChecks(unittest.TestCase):
    def test_native_acceptance_uses_host_http_route_and_requires_controller_and_playback(self):
        responses = {"/api/rooms": {"config_required": False, "backend_status": {"connected": True}},
                     "/api/playback/health": {"configured": True, "status": "ok"}}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = json.dumps(responses[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            spec = {**spec_fixture(Path("/prepared")), "publish_port": server.server_port}
            native.web_checks(spec)
            responses["/api/rooms"]["backend_status"]["connected"] = False
            with self.assertRaises(source.NativeError):
                native.web_checks(spec)
            responses["/api/rooms"]["backend_status"]["connected"] = True
            responses["/api/playback/health"]["status"] = "degraded"
            with self.assertRaises(source.NativeError):
                native.web_checks(spec)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
