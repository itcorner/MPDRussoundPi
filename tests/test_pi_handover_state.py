import copy
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import Mock, call, patch
from uuid import uuid4

from test_deployment_build import fixture_bundle
from test_deployment_native import selection_fixture, spec_fixture
from tool import deployment_native as native
from tool import deployment_native_source as source
from tool.deployment_backup import FileBackup
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import ComposeDeployment, ComposeError, encoded, private_write, runtime_checks
from tool.deployment_load import select_bundle
from tool.deployment_handover_transaction import HandoverHooks, execute as execute_handover
from tool.deployment_handover_target import HandoverTarget
from tool.deployment_operator import controller, interactive, serve
from tool.deployment_pi_handover import (
    capture_backup, check_docker_conflicts, fence_candidate_containers, fence_native, inspect_uncertain_load,
    receive_handover_backup,
    prepare, prepare_backup_restore, recover_before_activation, resume_before_backup, send_handover_backup,
    verify_backup, wait_fenced_port,
)
from tool.deployment_preflight import PreflightError
from tool.deployment_state import PHASES, StateError, StateStore


INTENT = "sha256:" + "d" * 64


class FencedPortTests(unittest.TestCase):
    def test_waits_until_exclusive_bind_succeeds(self):
        with patch("tool.deployment_pi_handover.check_port",
                   side_effect=[PreflightError("occupied"), PreflightError("occupied"), None]) as port, \
                patch("tool.deployment_pi_handover.time.monotonic", side_effect=[0, 1, 2]), \
                patch("tool.deployment_pi_handover.time.sleep") as sleep:
            wait_fenced_port("127.0.0.1", 8000, timeout=3)
        self.assertEqual(port.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_occupied_port_fails_closed_at_deadline(self):
        with patch("tool.deployment_pi_handover.check_port", side_effect=PreflightError("occupied")), \
                patch("tool.deployment_pi_handover.time.monotonic", side_effect=[0, 1, 3]), \
                patch("tool.deployment_pi_handover.time.sleep") as sleep:
            with self.assertRaisesRegex(source.NativeError, "remains unavailable"):
                wait_fenced_port("127.0.0.1", 8000, timeout=3)
        sleep.assert_called_once_with(1)


class PiHandoverStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-handover-state-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        root.chmod(0o700)
        self.store = StateStore(root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.native = selection_fixture()
        self.native_operation = str(uuid4())
        required = ["units", "http", "backend", "playback"]
        self.store.begin("fixture", self.native_operation, self.native, INTENT, required)
        for phase in PHASES[1:]:
            self.store.advance("fixture", self.native_operation, phase)
        self.store.finalize("fixture", self.native_operation, {
            "operation": self.native_operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(required, "passed"),
        })
        self.previous = self.store.status()["deployments"]["fixture"]["committed"]
        bundle, checksum, _ = fixture_bundle(root, "managed")
        self.images = select_bundle(bundle, checksum, "managed", "linux/arm64")
        self.operation = str(uuid4())
        self.required = ["apply", "containers", "http", "backend", "playback"]

    def test_native_record_remains_committed_until_image_transaction_finalizes(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        self.store.advance("fixture", self.operation, "inputs-verified")
        with self.assertRaises(StateError):
            self.store.advance("fixture", self.operation, "backup-complete")
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.previous)
        self.store.advance("fixture", self.operation, "backup-complete", backup=self.operation)
        with self.assertRaises(StateError):
            self.store.advance("fixture", self.operation, "apply-complete")
        self.store.start_image_load("fixture", self.operation, self.images, INTENT)
        loaded = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
        self.store.complete_image_load("fixture", self.operation, self.images, INTENT, loaded)
        self.store.advance("fixture", self.operation, "apply-complete")
        self.store.advance("fixture", self.operation, "checks-complete")
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.previous)
        self.store.finalize("fixture", self.operation, {
            "operation": self.operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(self.required, "passed"),
        })
        state = self.store.status()
        self.assertFalse(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"]["selection"]["candidate"], self.images)
        self.assertEqual(state["deployments"]["fixture"]["committed"]["selection"]["loaded_images"], loaded)

    def test_existing_recovery_refuses_native_restore_for_pending_image_candidate(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": dict.fromkeys(("containers", "http", "backend", "playback"), "passed")}
        with self.assertRaisesRegex(StateError, "explicit handover recovery"):
            self.store.resolve_recovery("fixture", self.operation, receipt,
                                        restore_native=True, native_previous=self.previous)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.previous)
        self.assertEqual(state["deployments"]["fixture"]["pending"]["operation"], self.operation)

    def test_generic_image_recovery_cannot_clear_a_native_baseline(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        for required in (dict.fromkeys(("containers", "http", "backend", "playback"), "passed"),
                         {"containers-absent": "passed", "prepared-data-restored": "passed"}):
            receipt = {"operation": self.operation, "review_digest": INTENT, "required": required}
            with self.subTest(required=required), self.assertRaisesRegex(StateError, "explicit handover recovery"):
                self.store.resolve_recovery("fixture", self.operation, receipt)
            self.assertTrue(self.store.status()["blocked"])
            self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.previous)

    def test_explicit_handover_resolution_requires_matching_backup_and_native_checks(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": {"original-runtime": "passed", "matching-data": "passed"}}
        with self.assertRaisesRegex(StateError, "baseline and backup"):
            self.store.resolve_recovery("fixture", self.operation, receipt, restore_native=True,
                                        native_previous=self.previous, handover_native=True)
        self.store.advance("fixture", self.operation, "inputs-verified")
        self.store.advance("fixture", self.operation, "backup-complete", backup=self.operation)
        wrong_previous = copy.deepcopy(self.previous)
        wrong_previous["selection"]["operation"] = str(uuid4())
        wrong_receipt = {**receipt, "required": {"original-runtime": "passed", "matching-data": "failed"}}
        for previous, result in ((wrong_previous, receipt), (self.previous, wrong_receipt)):
            with self.subTest(previous=previous, result=result), self.assertRaises(StateError):
                self.store.resolve_recovery("fixture", self.operation, result, restore_native=True,
                                            native_previous=previous, handover_native=True)
            self.assertTrue(self.store.status()["blocked"])
        self.store.resolve_recovery("fixture", self.operation, receipt, restore_native=True,
                                    native_previous=self.previous, handover_native=True)
        state = self.store.status()
        self.assertFalse(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.previous)
        self.assertIsNone(state["deployments"]["fixture"]["pending"])

    def reject_candidate_drift(self, field, value):
        images = copy.deepcopy(self.images)
        images["manifest"][field] = value
        self.store.begin("fixture", self.operation, images, INTENT, self.required,
                         expected_committed=self.previous)
        self.store.advance("fixture", self.operation, "inputs-verified")
        self.store.advance("fixture", self.operation, "backup-complete", backup=self.operation)
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": {"original-runtime": "passed", "matching-data": "passed"}}
        with self.assertRaisesRegex(StateError, "candidate identity"):
            self.store.resolve_recovery("fixture", self.operation, receipt, restore_native=True,
                                        native_previous=self.previous, handover_native=True)
        self.assertTrue(self.store.status()["blocked"])

    def test_handover_resolution_rejects_revision_drift(self):
        self.reject_candidate_drift("revision", "b" * 40)

    def test_handover_resolution_rejects_platform_drift(self):
        self.reject_candidate_drift("platform", "linux/amd64")

    def test_prebackup_resume_state_rejects_backup_and_wrong_receipt(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        self.store.advance("fixture", self.operation, "inputs-verified")
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": {"original-runtime": "passed", "data-preserved": "passed"}}
        with self.assertRaises(StateError):
            self.store.resolve_recovery("fixture", self.operation, {**receipt, "required": {}},
                                        restore_native=True, native_previous=self.previous, handover_resume=True)
        self.store.advance("fixture", self.operation, "backup-complete", backup=self.operation)
        with self.assertRaisesRegex(StateError, "pre-backup"):
            self.store.resolve_recovery("fixture", self.operation, receipt, restore_native=True,
                                        native_previous=self.previous, handover_resume=True)
        self.assertTrue(self.store.status()["blocked"])


class PiHandoverBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-handover-backup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.releases = self.root / "releases"
        self.runtime = self.root / "runtime"
        self.assets = self.root / "assets"
        self.backups = self.root / "backups"
        self.evidence = self.root / "evidence"
        self.offhost = self.root / "offhost"
        for directory in (self.releases, self.runtime, self.assets, self.backups, self.evidence, self.offhost):
            directory.mkdir(mode=0o700)
        self.patchers = [
            patch.object(native, "RELEASES", self.releases),
            patch.object(native, "RUNTIME", self.runtime),
            patch("tool.deployment_backup.os.listxattr", return_value=[], create=True),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spec = spec_fixture(self.root)
        self.spec["uid"] = os.getuid() or 1000
        self.operation = str(uuid4())
        self.native_operation = str(uuid4())
        self.selection = selection_fixture()
        release = self.releases / self.selection["release"]
        release.mkdir(mode=0o700)
        (release / "code").write_text("sealed source")
        self.selection["sha256"] = source.tree_digest(release, owner=os.getuid())
        runtime = self.runtime / self.native_operation
        runtime.mkdir(mode=0o700)
        (runtime / "ser2net.yaml").write_bytes(native.gateway_config(self.spec))
        self.asset = self.assets / self.native_operation
        self.asset.mkdir(mode=0o700)
        (self.asset / "spec.json").write_text(json.dumps(self.spec))
        (self.asset / "selection.json").write_text(json.dumps(self.selection))
        for name, value in native.unit_files(self.spec, self.selection, self.native_operation).items():
            (self.asset / name).write_bytes(value)
        data = Path(self.spec["data_dir"])
        data.mkdir(mode=0o700)
        (data / "state.json").write_text("current state")
        store = StateStore(self.root / "state", owner_uid=os.getuid())
        self.store = store
        store.initialize()
        required = list(native.CHECKS)
        store.begin("fixture", self.native_operation, self.selection, INTENT, required)
        for phase in PHASES[1:]:
            store.advance("fixture", self.native_operation, phase)
        store.finalize("fixture", self.native_operation, {
            "operation": self.native_operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(required, "passed"),
        })
        self.committed = store.status()["deployments"]["fixture"]["committed"]
        bundle, checksum, _ = fixture_bundle(self.root, "managed")
        self.images = select_bundle(bundle, checksum, "managed", "linux/arm64")
        self.backup = FileBackup.capture(self.backups, self.operation, {
            "data": data, "native-asset": self.asset, "previous-release": release, "previous-runtime": runtime,
        }, verify_quiescent=lambda: None, source_owner_uid=os.getuid())

    def verify(self, **overrides):
        arguments = {"committed": self.committed, "native_spec": self.spec,
                     "image_selection": self.images, "native_asset": self.asset, **overrides}
        return verify_backup(self.backup.path, self.operation, self.backup.manifest_sha256, **arguments)

    def test_verified_backup_binds_fresh_data_and_native_assets(self):
        manifest = self.verify()
        self.assertEqual(manifest["sources"]["data"], self.spec["data_dir"])
        self.assertTrue(any(entry["path"] == "data/state.json" for entry in manifest["entries"]))

    def test_wrong_native_identity_or_bundle_is_rejected(self):
        changed = copy.deepcopy(self.spec)
        changed["serial_options"] = "9600n81"
        with self.assertRaisesRegex(source.NativeError, "native inputs"):
            self.verify(native_spec=changed)
        images = copy.deepcopy(self.images)
        images["manifest"]["revision"] = "b" * 40
        with self.assertRaisesRegex(source.NativeError, "source revision"):
            self.verify(image_selection=images)
        with self.assertRaisesRegex(source.NativeError, "asset path"):
            self.verify(native_asset=self.assets / str(uuid4()))

    def test_corrupted_backup_is_rejected(self):
        blob = next(self.backup.path.glob("*.blob"))
        blob.chmod(0o600)
        blob.write_bytes(b"corrupted")
        blob.chmod(0o400)
        with self.assertRaises(ValueError):
            self.verify()

    def test_validly_captured_but_changed_native_assets_are_rejected(self):
        for path, replacement, message in (
            (self.asset / native.WEB, b"unexpected unit", "native unit"),
            (self.runtime / self.native_operation / "ser2net.yaml", b"unexpected route", "gateway configuration"),
            (self.releases / self.selection["release"] / "code", b"unexpected source", "native release"),
        ):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(replacement)
                operation = str(uuid4())
                backup = FileBackup.capture(self.backups, operation, {
                    "data": Path(self.spec["data_dir"]), "native-asset": self.asset,
                    "previous-release": self.releases / self.selection["release"],
                    "previous-runtime": self.runtime / self.native_operation,
                }, verify_quiescent=lambda: None, source_owner_uid=os.getuid())
                with self.assertRaisesRegex(source.NativeError, message):
                    verify_backup(backup.path, operation, backup.manifest_sha256, committed=self.committed,
                                  native_spec=self.spec, image_selection=self.images, native_asset=self.asset)
                path.write_bytes(original)

    def test_capture_requires_independent_verification_before_advancing(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        observed = []
        def verify_offhost(backup):
            self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"]["phase"],
                             "inputs-verified")
            copy = self.offhost / operation
            shutil.copytree(backup.path, copy)
            verify_backup(copy, operation, backup.manifest_sha256, committed=self.committed,
                          native_spec=self.spec, image_selection=self.images, native_asset=self.asset)
            observed.append("verified")
        result = capture_backup(
            self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
            "fixture", operation, committed=self.committed, native_spec=self.spec,
            image_selection=self.images, native_asset=self.asset,
            verify_quiescent=lambda: None, verify_offhost=verify_offhost,
        )
        self.assertEqual(observed, ["verified"])
        pending = self.store.status()["deployments"]["fixture"]["pending"]
        self.assertEqual((pending["phase"], pending["backup"]), ("backup-complete", operation))
        evidence = json.loads((self.evidence / (operation + ".backup.json")).read_bytes())
        self.assertEqual(evidence["manifest_sha256"], result.manifest_sha256)
        self.assertTrue(self.store.status()["blocked"])

    def transaction_fixture(self, *, failure=None):
        self.operation = str(uuid4())
        runtime = self.root / "transaction-runtime"
        runtime.mkdir(mode=0o700)
        storage = {"assets": self.assets, "backups": self.backups, "evidence": self.evidence,
                   "runtime": runtime}
        calls = []

        def step(name):
            calls.append(name)
            if name == failure:
                raise source.NativeError("injected " + name)

        def backup():
            step("backup")
            capture_backup(self.store, storage, "fixture", self.operation, committed=self.committed,
                           native_spec=self.spec, image_selection=self.images, native_asset=self.asset,
                           verify_quiescent=lambda: None, verify_offhost=lambda backup: None)

        def load(digest):
            step("load")
            self.store.start_image_load("fixture", self.operation, self.images, digest)
            images = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
            self.store.complete_image_load("fixture", self.operation, self.images, digest, images)
            return images

        def apply(images):
            step("apply")
            directory = runtime / self.operation
            config = json.loads((directory / "compose.json").read_bytes())
            config["services"]["russound-web"]["image"] = images["web"]
            config["services"]["russound-ser2net"]["image"] = images["ser2net"]
            private_write(directory / "active.json", config)

        hooks = HandoverHooks(preflight=lambda: step("preflight"),
                              receive_images=lambda digest: step("transfer"),
                              stop_native=lambda: step("fence"), capture_backup=backup,
                              load_images=load, apply=apply,
                              checks=lambda images: (step("checks") or
                                                     dict.fromkeys(("containers", "http", "backend", "playback"),
                                                                   "passed")))
        arguments = {"committed": self.committed, "native_spec": self.spec, "selection": self.images,
                     "engine": "engine", "hooks": hooks, "approve": lambda review: review["digest"]}
        return storage, arguments, calls

    def target_fixture(self):
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        data = Path(self.spec["data_dir"])
        (data / "config").mkdir()
        (data / "config/russound_config.json").write_text('{"zones": [{"controller": 1, "zone": 1}]}')
        storage = {"assets": self.assets, "backups": self.backups, "evidence": self.evidence,
                   "state": self.root / "state", "runtime": self.root / "target-runtime",
                   "incoming": self.root / "incoming", "staging": self.root / "staging",
                   "restores": self.root / "restores"}
        for name in ("runtime", "incoming", "staging", "restores"):
            storage[name].mkdir(mode=0o700)
        return HandoverTarget(self.store, storage, "fixture", str(uuid4()), self.images)

    def target_preflight_patches(self):
        stack = ExitStack()
        stack.enter_context(patch("tool.deployment_handover_target.sys.platform", "linux"))
        stack.enter_context(patch("tool.deployment_handover_target.os.geteuid", return_value=0))
        stack.enter_context(patch("tool.deployment_handover_target.pwd.getpwuid",
                                  return_value=Mock(pw_gid=self.spec["gid"])))
        stack.enter_context(patch("tool.deployment_handover_target.check_data"))
        stack.enter_context(patch("tool.deployment_handover_target.validate_runtime_files"))
        stack.enter_context(patch("web.config_types.validate_russound_config"))
        stack.enter_context(patch("tool.deployment_handover_target.safe_path",
                                  return_value=Mock(st_mode=stat.S_IFCHR | 0o660,
                                                    st_gid=self.spec["serial_gid"])))
        stack.enter_context(patch("tool.deployment_handover_target.mode_access", return_value=True))
        stack.enter_context(patch.object(native, "unit_status",
                                        return_value={"ActiveState": "inactive", "UnitFileState": "disabled"}))
        stack.enter_context(patch.object(native, "runtime_checks"))
        stack.enter_context(patch.object(native, "check_host_writers"))
        stack.enter_context(patch("tool.deployment_handover_target.check_docker_conflicts", return_value="engine"))
        stack.enter_context(patch("tool.deployment_handover_target.local_engine", return_value="default"))

        def command(argv):
            if "info" in argv:
                return b'{"ID":"engine","OSType":"linux","Architecture":"arm64","SecurityOptions":[]}'
            if "version" in argv:
                return b'{"version":"2.0"}'
            raise AssertionError(argv)

        stack.enter_context(patch.object(native, "command", side_effect=command))
        return stack

    def test_target_preflight_checks_native_assets_and_strict_docker_scope(self):
        target = self.target_fixture()
        with self.target_preflight_patches(), \
                patch("tool.deployment_handover_target.check_docker_conflicts", return_value="engine") as conflicts:
            target.preflight()
            target.preflight()
        self.assertEqual(target.engine, "engine")
        self.assertGreater(target.backup_estimate, 0)
        self.assertEqual(conflicts.call_count, 4)
        self.assertTrue(all(call.kwargs["handover_project"] == target.project for call in conflicts.call_args_list))
        self.assertFalse(self.store.status()["blocked"])

    def test_target_preflight_rejects_changed_native_assets_without_mutation(self):
        target = self.target_fixture()
        (self.asset / "spec.json").write_text("{}")
        with self.target_preflight_patches(), patch.object(native, "command") as command:
            with self.assertRaisesRegex(source.NativeError, "assets changed"):
                target.preflight()
            command.assert_not_called()
        self.assertFalse(self.store.status()["blocked"])

    def test_target_preflight_rejects_conflicting_container_before_review(self):
        target = self.target_fixture()
        with self.target_preflight_patches(), \
                patch("tool.deployment_handover_target.check_docker_conflicts",
                      side_effect=source.NativeError("container references live data")):
            with self.assertRaisesRegex(source.NativeError, "container references"):
                target.preflight()
        self.assertIsNone(target.engine)
        self.assertFalse(self.store.status()["blocked"])

    def test_target_preflight_rejects_docker_identity_drift(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
            with patch("tool.deployment_handover_target.check_docker_conflicts",
                       side_effect=source.NativeError("Docker daemon changed")):
                with self.assertRaisesRegex(source.NativeError, "daemon changed"):
                    target.preflight()
        self.assertFalse(self.store.status()["blocked"])

    def test_target_receives_verified_bundle_only_for_matching_reservation(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        self.store.begin("fixture", target.operation, self.images, INTENT, ["apply"],
                         expected_committed=self.committed)
        self.store.advance("fixture", target.operation, "inputs-verified")
        self.store.start_image_transfer("fixture", target.operation, self.images, INTENT)
        source = self.root / self.images["manifest"]["operation"]
        wire = io.BytesIO()
        sender = Channel(io.BytesIO(), wire)
        sender.send_file(source / "manifest.json")
        for component in sorted(self.images["manifest"]["images"]):
            sender.send_file(source / self.images["manifest"]["images"][component]["archive"])
        requests = io.BytesIO()
        target.receive_images(Channel(io.BytesIO(wire.getvalue()), requests), INTENT)
        self.assertEqual(Channel(io.BytesIO(requests.getvalue()), io.BytesIO()).receive(),
                         {"event": "images", "operation": target.operation, "intent_digest": INTENT})
        incoming = target.storage["incoming"] / target.operation
        self.assertEqual(select_bundle(incoming, self.images["manifest_sha256"], "managed", "linux/arm64"),
                         self.images)
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse(self.store.status()["image_transfer_verified"])

    def test_target_rejects_unreserved_image_transfer_without_receiving_files(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        channel = Mock()
        with self.assertRaisesRegex(source.NativeError, "reserved handover"):
            target.receive_images(channel, INTENT)
        channel.send.assert_not_called()
        self.assertFalse((target.storage["incoming"] / target.operation).exists())
        self.assertFalse(self.store.status()["blocked"])

    def test_interrupted_target_transfer_retains_reservation_and_never_acknowledges(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        self.store.begin("fixture", target.operation, self.images, INTENT, ["apply"],
                         expected_committed=self.committed)
        self.store.advance("fixture", target.operation, "inputs-verified")
        self.store.start_image_transfer("fixture", target.operation, self.images, INTENT)
        source = self.root / self.images["manifest"]["operation"]
        wire = io.BytesIO()
        sender = Channel(io.BytesIO(), wire)
        sender.send_file(source / "manifest.json")
        channel = Channel(io.BytesIO(wire.getvalue()), io.BytesIO())
        with self.assertRaisesRegex(ChannelError, "ended unexpectedly"):
            target.receive_images(channel, INTENT)
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_transfer_started"])
        self.assertFalse(self.store.status()["image_transfer_verified"])

    def test_target_preflight_allows_bounded_growth_but_not_unreviewed_capacity(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
            growth = Path(self.spec["data_dir"]) / "state.json"
            growth.write_text("changed live data")
            target.preflight()
            target.backup_estimate = 1
            with self.assertRaisesRegex(source.NativeError, "outgrew"):
                target.preflight()
        self.assertFalse(self.store.status()["blocked"])

    def test_target_binds_fence_backup_and_load_to_reviewed_engine(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        self.store.begin("fixture", target.operation, self.images, INTENT, ["apply"],
                         expected_committed=self.committed)
        self.store.advance("fixture", target.operation, "inputs-verified")
        self.store.start_image_transfer("fixture", target.operation, self.images, INTENT)
        self.store.complete_image_transfer("fixture", target.operation, self.images, INTENT)
        channel = Mock()
        with patch("tool.deployment_handover_target.check_docker_conflicts") as conflicts, \
                patch("tool.deployment_handover_target.fence_native") as fence, \
                patch("tool.deployment_handover_target.capture_backup") as backup, \
                patch("tool.deployment_handover_target.load_reserved_bundle", return_value={"web": "image"}) as load, \
                patch.object(native, "check_no_writers") as quiet, \
                patch.object(native, "verify_units"), \
                patch.object(native, "system_units", return_value={
                    native.WEB: {"UnitFileState": "disabled"},
                    native.GATEWAY: {"UnitFileState": "disabled"}}):
            target.stop_native()
            target.capture_backup(channel)
            self.assertEqual(target.load_images(INTENT), {"web": "image"})
        self.assertEqual(conflicts.call_count, 3)
        self.assertEqual(quiet.call_count, 2)
        self.assertEqual(fence.call_args.kwargs["committed"], self.committed)
        self.assertEqual(backup.call_args.kwargs["verify_quiescent"], target.quiescent)
        self.assertEqual(load.call_args.kwargs["expected_engine"], "engine")

    def test_target_does_not_fence_before_verified_transfer(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        self.store.begin("fixture", target.operation, self.images, INTENT, ["apply"],
                         expected_committed=self.committed)
        self.store.advance("fixture", target.operation, "inputs-verified")
        with patch("tool.deployment_handover_target.fence_native") as fence:
            with self.assertRaisesRegex(source.NativeError, "verified image transfer"):
                target.stop_native()
        fence.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def test_quiescence_rejects_reenabled_native_units(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        with patch.object(native, "verify_units"), \
                patch.object(native, "system_units", return_value={
                    native.WEB: {"UnitFileState": "enabled"},
                    native.GATEWAY: {"UnitFileState": "disabled"}}), \
                patch.object(native, "check_no_writers") as quiet:
            with self.assertRaisesRegex(source.NativeError, "no longer disabled"):
                target.quiescent()
        quiet.assert_not_called()

    def test_target_activation_requires_matching_markers_and_images(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        self.store.begin("fixture", target.operation, self.images, INTENT, ["apply"],
                         expected_committed=self.committed)
        self.store.advance("fixture", target.operation, "inputs-verified")
        self.store.advance("fixture", target.operation, "backup-complete", backup=target.operation)
        self.store.start_image_load("fixture", target.operation, self.images, INTENT)
        images = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
        self.store.complete_image_load("fixture", target.operation, self.images, INTENT, images)
        runtime = target.storage["runtime"] / target.operation
        runtime.mkdir(mode=0o700)
        private_write(runtime / "compose.json", target.config)
        private_write(self.evidence / (target.operation + ".apply-attempt.json"),
                      {"operation": target.operation, "intent_digest": INTENT})
        private_write(self.evidence / (target.operation + ".handover-activation.json"),
                      {"operation": target.operation, "intent_digest": INTENT, "project": target.project,
                       "engine": "engine", "images": images,
                       "config_digest": "sha256:" + hashlib.sha256(encoded(target.config)).hexdigest()})
        with patch.object(target, "quiescent"), patch.object(target, "command"), \
                patch("tool.deployment_handover_target.check_port"), \
                patch("tool.deployment_handover_target.apply") as apply:
            with self.assertRaisesRegex(source.NativeError, "fully loaded"):
                target.activate({**images, "web": "sha256:" + "f" * 64})
            apply.assert_not_called()
            target.activate(images)
            apply.assert_called_once_with(runtime / "compose.json", images)
        self.assertTrue(self.store.status()["blocked"])

    def test_runtime_acceptance_rejects_foreign_data_container(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        images = {"web": "web-image", "ser2net": "gateway-image"}
        candidates = [{"id": "web", "running": True, "restarting": False},
                      {"id": "gateway", "running": True, "restarting": False}]
        foreign = {"Id": "foreign", "Mounts": [{"Type": "bind", "Source": self.spec["data_dir"]}],
                   "HostConfig": {"Devices": []}}
        units = {native.WEB: {"UnitFileState": "disabled", "ActiveState": "inactive", "MainPID": "0"},
                 native.GATEWAY: {"UnitFileState": "disabled", "ActiveState": "inactive", "MainPID": "0"}}
        with patch.object(native, "verify_units"), patch.object(native, "system_units", return_value=units), \
                patch("tool.deployment_handover_target.inspect_candidate_containers", return_value=candidates), \
                patch.object(target, "inspect_all", return_value=[foreign]), \
                patch("tool.deployment_handover_target.runtime_checks") as checks:
            with self.assertRaisesRegex(source.NativeError, "foreign container"):
                target.checks(images)
            checks.assert_not_called()

    def test_runtime_acceptance_requires_configured_playback_when_requested(self):
        target = Mock()
        target.services = {"russound-web": "web"}
        target.spec = {"gateway": "managed", "require_controller": True, "require_playback": True,
                       "publish_host": "127.0.0.1", "publish_port": 8000}
        container: dict[str, Any] = {"Id": "web-id", "Image": "web-image", "RestartCount": 0,
                                     "Config": {"Labels": {"com.docker.compose.service": "russound-web"}},
                                     "State": {"Running": True, "Health": {"Status": "healthy"}}}
        target.own.return_value = [container]
        target.command.return_value = json.dumps({"configured": True, "backend": True,
                                                  "playback": True, "playback_configured": False}).encode()
        with patch("tool.deployment_compose.time.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(ComposeError, "health/routing deadline"):
                runtime_checks(target, {"web": "web-image"})
        target.command.return_value = json.dumps({"configured": True, "backend": True,
                                                  "playback": True, "playback_configured": True}).encode()
        response = Mock(status=200)
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(runtime_checks(target, {"web": "web-image"}),
                             dict.fromkeys(("containers", "http", "backend", "playback"), "passed"))
        target.command.side_effect = source.NativeError("Docker daemon changed")
        with self.assertRaisesRegex(source.NativeError, "Docker daemon changed"):
            runtime_checks(target, {"web": "web-image"})

    def test_candidate_acceptance_validates_compose_hashes(self):
        target = self.target_fixture()
        with self.target_preflight_patches():
            target.preflight()
        images = {"web": "web-image", "ser2net": "gateway-image"}
        candidates: list[dict[str, Any]] = [{"id": name, "running": True, "restarting": False}
                                            for name in ("web", "gateway")]
        containers: list[dict[str, Any]] = [
            {"Id": identifier, "Config": {"Labels": {
                "com.docker.compose.project": target.project,
                "com.docker.compose.service": service,
                "com.docker.compose.config-hash": checksum,
            }}} for identifier, service, checksum in (
                ("web", "russound-web", "web-hash"),
                ("gateway", "russound-ser2net", "gateway-hash"))
        ]
        units = {name: {"UnitFileState": "disabled", "ActiveState": "inactive", "MainPID": "0"}
                 for name in (native.WEB, native.GATEWAY)}
        result = dict.fromkeys(("containers", "http", "backend", "playback"), "passed")
        with patch.object(native, "verify_units"), patch.object(native, "system_units", return_value=units), \
                patch("tool.deployment_handover_target.inspect_candidate_containers", return_value=candidates), \
                patch.object(target, "inspect_all", return_value=containers), \
                patch.object(target, "command", return_value=b"russound-web web-hash\nrussound-ser2net gateway-hash\n"), \
                patch("tool.deployment_handover_target.runtime_checks", return_value=result) as checks:
            self.assertEqual(target.checks(images), result)
            containers[0]["Config"]["Labels"]["com.docker.compose.config-hash"] = "different"
            with self.assertRaisesRegex(source.NativeError, "configuration differs"):
                target.checks(images)
            self.assertEqual(checks.call_count, 1)

    def test_internal_transaction_orders_backup_load_activation_and_checks(self):
        storage, arguments, calls = self.transaction_fixture()
        result = execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        self.assertEqual(calls, ["preflight", "preflight", "transfer", "fence", "backup", "load", "apply", "checks"])
        self.assertEqual(result["event"], "complete")
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"]["selection"]["candidate"],
                         self.images)
        self.assertTrue((self.evidence / (self.operation + ".backup.json")).exists())
        self.assertTrue((self.evidence / (self.operation + ".handover-activation.json")).exists())

    def test_internal_transaction_keeps_reservation_after_apply_failure(self):
        storage, arguments, calls = self.transaction_fixture(failure="apply")
        with self.assertRaisesRegex(source.NativeError, "injected apply"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.committed)
        self.assertEqual(state["deployments"]["fixture"]["pending"]["phase"], "backup-complete")
        self.assertTrue((self.evidence / (self.operation + ".apply-attempt.json")).exists())
        self.assertNotIn("checks", calls)

    def test_internal_transaction_decline_does_not_reserve_or_interrupt(self):
        storage, arguments, calls = self.transaction_fixture()
        arguments["approve"] = lambda review: None
        with self.assertRaisesRegex(source.NativeError, "declined"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        self.assertEqual(calls, ["preflight"])
        self.assertFalse(self.store.status()["blocked"])

    def test_internal_transaction_backup_failure_never_loads_images(self):
        storage, arguments, calls = self.transaction_fixture(failure="backup")
        with self.assertRaisesRegex(source.NativeError, "injected backup"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["pending"]["phase"], "inputs-verified")
        self.assertFalse(state["image_load_started"])
        self.assertNotIn("load", calls)

    def test_internal_transaction_failed_checks_never_commit_candidate(self):
        storage, arguments, calls = self.transaction_fixture(failure="checks")
        with self.assertRaisesRegex(source.NativeError, "injected checks"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.committed)
        self.assertEqual(state["deployments"]["fixture"]["pending"]["phase"], "apply-complete")
        self.assertEqual(calls[-1], "checks")

    def test_internal_transaction_apply_failure_can_recover_native(self):
        storage, arguments, calls = self.transaction_fixture(failure="apply")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        with self.assertRaisesRegex(source.NativeError, "injected apply"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        backup_evidence = json.loads((self.evidence / (self.operation + ".backup.json")).read_bytes())
        restores = self.root / "transaction-restores"
        restores.mkdir(mode=0o700)
        storage.update({"state": self.root / "state", "restores": restores})
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks"), \
                patch.object(native, "command"), \
                patch("tool.deployment_pi_handover.inspect_candidate_containers", return_value=[]), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"):
            result = recover_before_activation(storage, "fixture", self.operation,
                                               backup_path=self.backups / self.operation,
                                               checksum=backup_evidence["manifest_sha256"],
                                               approve=lambda review: review["digest"], activated=True)
        self.assertEqual(result["event"], "recovered")
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.committed)

    def test_isolated_restore_preserves_live_data_and_keeps_reservation(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("post-backup change")
        guard = Mock(return_value=None)
        destination = prepare_backup_restore(
            self.store, {"assets": self.assets, "evidence": self.evidence, "restores": restores},
            "fixture", operation, backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
            committed=self.committed, native_spec=self.spec, image_selection=self.images,
            native_asset=self.asset, recovery=str(uuid4()), verify_quiescent=guard)
        self.assertEqual((destination / "data/state.json").read_text(), "current state")
        self.assertEqual(live.read_text(), "post-backup change")
        self.assertGreater(guard.call_count, 1)
        self.assertTrue(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.committed)

    def test_isolated_restore_rejects_mismatched_custody_and_prebackup_state(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": "0" * 64})
        arguments = {
            "backup_path": self.backup.path, "checksum": self.backup.manifest_sha256,
            "committed": self.committed, "native_spec": self.spec, "image_selection": self.images,
            "native_asset": self.asset, "recovery": str(uuid4()), "verify_quiescent": lambda: None,
        }
        storage = {"assets": self.assets, "evidence": self.evidence, "restores": restores}
        with self.assertRaisesRegex(source.NativeError, "reserved handover"):
            prepare_backup_restore(self.store, storage, "fixture", operation, **arguments)
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        with self.assertRaisesRegex(source.NativeError, "captured evidence"):
            prepare_backup_restore(self.store, storage, "fixture", operation, **arguments)
        self.assertEqual(list(restores.iterdir()), [])
        self.assertTrue(self.store.status()["blocked"])

    def test_isolated_restore_keeps_partial_tree_when_quiescence_fails(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        recovery = str(uuid4())
        guard = Mock(side_effect=[None, source.NativeError("writer appeared")])
        with self.assertRaisesRegex(source.NativeError, "writer appeared"):
            prepare_backup_restore(
                self.store, {"assets": self.assets, "evidence": self.evidence, "restores": restores},
                "fixture", operation, backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
                committed=self.committed, native_spec=self.spec, image_selection=self.images,
                native_asset=self.asset, recovery=recovery, verify_quiescent=guard)
        self.assertTrue((restores / recovery).is_dir())
        self.assertEqual((Path(self.spec["data_dir"]) / "state.json").read_text(), "current state")
        self.assertTrue(self.store.status()["blocked"])

    def test_isolated_restore_stops_if_image_load_starts_concurrently(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        recovery = str(uuid4())

        def change_state():
            self.store.start_image_load("fixture", operation, self.images, INTENT)

        with self.assertRaisesRegex(source.NativeError, "reserved handover"):
            prepare_backup_restore(
                self.store, {"assets": self.assets, "evidence": self.evidence, "restores": restores},
                "fixture", operation, backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
                committed=self.committed, native_spec=self.spec, image_selection=self.images,
                native_asset=self.asset, recovery=recovery, verify_quiescent=change_state)
        self.assertTrue((restores / recovery).is_dir())
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertEqual((Path(self.spec["data_dir"]) / "state.json").read_text(), "current state")

    def test_backup_recovery_restores_data_and_retains_later_tree(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks") as checks, \
                patch.object(native, "command") as command, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            result = recover_before_activation(storage, "fixture", operation, backup_path=self.backup.path,
                                               checksum=self.backup.manifest_sha256,
                                               approve=lambda review: review["digest"])
        self.assertEqual(live.read_text(), "current state")
        preserved = live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
        self.assertEqual((preserved / "state.json").read_text(), "later edit")
        checks.assert_called_once_with(self.spec, self.selection, self.native_operation)
        command.assert_called_once_with(["systemctl", "enable", "--now", native.GATEWAY, native.WEB], timeout=90)
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.committed)

    def test_backup_recovery_rejects_started_image_load_without_service_mutation(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        self.store.start_image_load("fixture", operation, self.images, INTENT)
        with patch.object(native, "stop_native") as stop:
            with self.assertRaisesRegex(source.NativeError, "completed image load"):
                recover_before_activation(
                    {"state": self.root / "state", "evidence": self.evidence}, "fixture", operation,
                    backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
                    approve=lambda review: review["digest"])
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def uncertain_load_fixture(self):
        operation = self.operation
        intent = {"kind": "pi-handover-transaction", "deployment": "fixture", "operation": operation,
                  "committed": self.committed, "selection": self.images, "native_spec": self.spec,
                  "engine": "engine"}
        digest = "sha256:" + hashlib.sha256(encoded(intent)).hexdigest()
        self.store.begin("fixture", operation, self.images, digest, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.start_image_transfer("fixture", operation, self.images, digest)
        self.store.complete_image_transfer("fixture", operation, self.images, digest)
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        self.store.start_image_load("fixture", operation, self.images, digest)
        private_write(self.evidence / (operation + ".intent.json"), intent)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        log = self.evidence / (operation + ".image-load.log")
        log.write_text("partial load")
        log.chmod(0o600)
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        return {"assets": self.assets, "evidence": self.evidence,
                "state": self.root / "state", "restores": restores}

    def test_uncertain_load_recovery_preserves_partial_images_and_recovers_native(self):
        storage = self.uncertain_load_fixture()
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        snapshot = {"web": [], "ser2net": [self.images["configs"]["ser2net"]]}
        reviews = []

        def approve(review):
            reviews.append(review["plan"])
            return review["digest"]

        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks"), \
                patch.object(native, "command"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"), \
                patch("tool.deployment_pi_handover.inspect_uncertain_load", return_value=snapshot):
            result = recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                               checksum=self.backup.manifest_sha256, approve=approve,
                                               uncertain_load=True)
        self.assertEqual(reviews[0]["operation_image_ids"], snapshot)
        self.assertTrue(reviews[0]["operator_confirmed_daemon_load_ended"])
        self.assertFalse(reviews[0]["image_load_retry"])
        self.assertEqual(live.read_text(), "current state")
        retained = live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
        self.assertEqual((retained / "state.json").read_text(), "later edit")
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.committed)

    def test_uncertain_load_decline_or_evidence_drift_never_mutates_services(self):
        storage = self.uncertain_load_fixture()
        snapshot = {"web": [], "ser2net": []}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"), \
                patch("tool.deployment_pi_handover.inspect_uncertain_load", return_value=snapshot):
            with self.assertRaisesRegex(source.NativeError, "declined"):
                recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                          checksum=self.backup.manifest_sha256, approve=lambda _: None,
                                          uncertain_load=True)

            def drift(review):
                (self.evidence / (self.operation + ".image-load.log")).write_text("changed")
                return review["digest"]

            with self.assertRaisesRegex(source.NativeError, "evidence changed"):
                recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                          checksum=self.backup.manifest_sha256, approve=drift,
                                          uncertain_load=True)
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def test_uncertain_load_requires_untouched_activation_evidence(self):
        storage = self.uncertain_load_fixture()
        private_write(self.evidence / (self.operation + ".handover-activation.json"), {})
        with patch.object(native, "stop_native") as stop:
            with self.assertRaises(source.NativeError):
                recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                          checksum=self.backup.manifest_sha256, approve=lambda review: review["digest"],
                                          uncertain_load=True)
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def test_uncertain_load_inspection_rejects_foreign_images_and_daemon_drift(self):
        image_id = self.images["configs"]["web"]
        manifest = self.images["manifest"]
        metadata = {"Id": image_id, "Os": "linux", "Architecture": "arm64", "Config": {"Labels": {
            "org.opencontainers.image.revision": manifest["revision"],
            "org.opencontainers.image.source": "https://github.com/itcorner/MPDRussoundPi"}}}
        names = list(manifest["images"])
        existing_name = names[0]
        image_id = self.images["configs"][existing_name]
        metadata["Id"] = image_id
        sequence = [b'{"ID":"engine"}', (image_id + "\n").encode(),
                    json.dumps([metadata]).encode(), b"", b'{"ID":"engine"}']
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=sequence) as command:
            result = inspect_uncertain_load(self.images, "engine")
            self.assertEqual(result[existing_name], [image_id])
            self.assertTrue(all(not call.args[0][-2:] == ["image", "load"] for call in command.call_args_list))
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=[b'{"ID":"engine"}', b"sha256:foreign\n"]):
            with self.assertRaisesRegex(source.NativeError, "unexpected image"):
                inspect_uncertain_load(self.images, "engine")
        sequence[-1] = b'{"ID":"other"}'
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=sequence):
            with self.assertRaisesRegex(source.NativeError, "daemon changed"):
                inspect_uncertain_load(self.images, "engine")

    def test_uncertain_load_cannot_be_resolved_as_ordinary_recovery(self):
        self.uncertain_load_fixture()
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": {"original-runtime": "passed", "matching-data": "passed"}}
        with self.assertRaisesRegex(StateError, "dedicated reviewed recovery"):
            self.store.resolve_recovery("fixture", self.operation, receipt, restore_native=True,
                                        native_previous=self.committed, handover_native=True)
        self.assertTrue(self.store.status()["blocked"])

    def test_operator_routes_uncertain_load_recovery(self):
        request = {"action": "handover-recover-uncertain-load", "deployment": "fixture",
                   "operation": self.operation}
        channel = Mock()
        channel.receive.side_effect = [request, {"event": "backup", "operation": self.operation}]
        incoming = self.root / "incoming"
        incoming.mkdir(mode=0o700)
        with patch("tool.deployment_operator.receive_backup",
                   return_value=(self.backup.path, self.backup.manifest_sha256)), \
                patch("tool.deployment_pi_handover.recover_before_activation") as recover:
            recover.return_value = {"event": "recovered", "operation": self.operation}
            serve(channel, {"incoming": incoming, "restores": self.root})
        self.assertTrue(recover.call_args.kwargs["uncertain_load"])
        self.assertFalse(recover.call_args.kwargs["activated"])

    def test_controller_rejects_uncertain_load_review_without_end_confirmation(self):
        request = {"action": "handover-recover-uncertain-load", "deployment": "fixture",
                   "operation": self.operation}
        plan = {"kind": "pi-handover-backup-recovery", "operation": self.operation,
                "deployment": "fixture", "backup_manifest_sha256": self.backup.manifest_sha256,
                "restore_matching_data": True, "preserve_post_backup_tree": True,
                "operator_confirmed_original_orchestration_ended": True, "activated": False,
                "uncertain_image_load": True, "operator_confirmed_daemon_load_ended": False,
                "partial_images_and_tags": "retained", "image_load_retry": False}
        review = {"event": "review", "plan": plan,
                  "digest": "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()}
        stream = io.BytesIO()
        Channel(io.BytesIO(), stream).send(review)
        approve = Mock()
        with patch("tool.deployment_operator.portable_backup"), patch("tool.deployment_operator.send_backup"):
            with self.assertRaisesRegex(ChannelError, "different uncertain image-load resolution"):
                controller(Channel(io.BytesIO(stream.getvalue()), io.BytesIO()), request, self.evidence,
                           approve, recovery=self.backup.path, recovery_checksum=self.backup.manifest_sha256)
        approve.assert_not_called()

    def test_interactive_uncertain_recovery_requires_separate_load_end_attestation(self):
        review = {"digest": INTENT,
                  "plan": {"operation": self.operation, "uncertain_image_load": True}}
        for entered, expected in (
                (f"approve {self.operation}\n", None),
                (f"confirm load-ended {self.operation}\napprove {self.operation}\n", INTENT)):
            with self.subTest(entered=entered), \
                    patch("builtins.open", side_effect=[io.StringIO(entered), io.StringIO()]):
                self.assertEqual(interactive(review), expected)

    def test_backup_recovery_after_completed_image_load_preserves_live_tree(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        self.store.start_image_load("fixture", operation, self.images, INTENT)
        loaded = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
        self.store.complete_image_load("fixture", operation, self.images, INTENT, loaded)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        reviews = []
        def approve(review):
            reviews.append(review["plan"])
            return review["digest"]
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks"), \
                patch.object(native, "command"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            result = recover_before_activation(storage, "fixture", operation, backup_path=self.backup.path,
                                               checksum=self.backup.manifest_sha256, approve=approve)
        self.assertEqual(reviews[0]["loaded_images"], loaded)
        self.assertEqual(live.read_text(), "current state")
        retained = live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
        self.assertEqual((retained / "state.json").read_text(), "later edit")
        self.assertFalse(self.store.status()["blocked"])

    def test_backup_recovery_rejects_apply_attempt_before_any_service_change(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".apply-attempt.json"), {"operation": operation})
        with patch.object(native, "stop_native") as stop:
            with self.assertRaisesRegex(source.NativeError, "completed image load"):
                recover_before_activation(
                    {"state": self.root / "state", "evidence": self.evidence}, "fixture", operation,
                    backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
                    approve=lambda review: review["digest"])
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def test_backup_recovery_decline_does_not_stop_services_or_create_attempt(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            with self.assertRaisesRegex(source.NativeError, "declined"):
                recover_before_activation(storage, "fixture", operation, backup_path=self.backup.path,
                                           checksum=self.backup.manifest_sha256, approve=lambda review: None)
            stop.assert_not_called()
        self.assertFalse((self.evidence / (operation + ".recovery-attempt.json")).exists())
        self.assertEqual(list(restores.iterdir()), [])
        self.assertTrue(self.store.status()["blocked"])

    def candidate_fencing_fixture(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        self.store.start_image_load("fixture", operation, self.images, INTENT)
        loaded = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
        self.store.complete_image_load("fixture", operation, self.images, INTENT, loaded)
        private_write(self.evidence / (operation + ".apply-attempt.json"),
                      {"operation": operation, "intent_digest": INTENT})
        project = "pi-handover-" + operation.replace("-", "")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        _, config = prepare(self.spec, self.selection, self.images, project=project,
                            compose_path="/handover-compose.json")
        private_write(self.evidence / (operation + ".handover-activation.json"),
                      {"operation": operation, "intent_digest": INTENT, "project": project,
                       "engine": "engine", "images": loaded,
                       "config_digest": "sha256:" + hashlib.sha256(encoded(config)).hexdigest()})
        containers = []
        for service, component in (("russound-web", "web"), ("russound-ser2net", "ser2net")):
            containers.append({"Id": component + "-id", "Image": loaded[component],
                               "Config": {"Labels": {"com.docker.compose.project": project,
                                                     "com.docker.compose.service": service}},
                               "State": {"Running": True, "Restarting": False, "Paused": False},
                               "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}})
        return containers

    def test_candidate_fencing_stops_only_matching_operation_containers(self):
        containers = self.candidate_fencing_fixture()
        unrelated = {"Id": "unrelated-id", "Image": "other", "Config": {"Labels": {}},
                     "State": {}, "HostConfig": {}}
        containers.append(unrelated)
        mutations = []
        def command(argv, **kwargs):
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            mutations.append(args)
            item = next(item for item in containers if item["Id"] == args[-1])
            if args[0] == "update":
                item["HostConfig"]["RestartPolicy"]["Name"] = "no"
            elif args[0] == "stop":
                item["State"]["Running"] = False
            else:
                self.fail(f"Unexpected Docker command: {args}")
            return b""
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command), \
                patch.object(native, "check_host_writers"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts") as conflicts:
            fence_candidate_containers(
                self.store, {"evidence": self.evidence, "assets": self.assets}, "fixture", self.operation,
                native_spec=self.spec, expected_engine="engine", approve=lambda review: review["digest"])
        self.assertEqual(len(mutations), 4)
        self.assertTrue(all(args[-1] != "unrelated-id" for args in mutations))
        conflicts.assert_called_once_with(self.spec, expected_engine="engine")
        self.assertTrue((self.evidence / (self.operation + ".candidate-fence-complete.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_candidate_fencing_refuses_unapproved_image_before_mutation(self):
        containers = self.candidate_fencing_fixture()
        containers[0]["Image"] = "unexpected"
        def command(argv, **kwargs):
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            self.fail(f"Unexpected Docker mutation: {args}")
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command):
            with self.assertRaisesRegex(source.NativeError, "Unexpected candidate"):
                fence_candidate_containers(
                    self.store, {"evidence": self.evidence, "assets": self.assets}, "fixture", self.operation,
                    native_spec=self.spec, expected_engine="engine", approve=lambda review: review["digest"])
        self.assertFalse((self.evidence / (self.operation + ".candidate-fence-attempt.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_candidate_fencing_refuses_container_drift_after_review(self):
        containers = self.candidate_fencing_fixture()
        def command(argv, **kwargs):
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            self.fail(f"Unexpected Docker mutation: {args}")
        def approve(review):
            containers[0]["State"]["Running"] = False
            return review["digest"]
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command):
            with self.assertRaisesRegex(source.NativeError, "changed after fencing review"):
                fence_candidate_containers(
                    self.store, {"evidence": self.evidence, "assets": self.assets}, "fixture",
                    self.operation, native_spec=self.spec, expected_engine="engine", approve=approve)
        self.assertFalse((self.evidence / (self.operation + ".candidate-fence-attempt.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_candidate_fencing_failure_retains_one_shot_evidence(self):
        containers = self.candidate_fencing_fixture()
        updates = []
        def command(argv, **kwargs):
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            updates.append(args)
            raise source.NativeError("injected Docker failure")
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command), \
                patch.object(native, "check_host_writers"):
            arguments = {"native_spec": self.spec, "expected_engine": "engine",
                         "approve": lambda review: review["digest"]}
            with self.assertRaisesRegex(source.NativeError, "injected Docker failure"):
                fence_candidate_containers(self.store, {"evidence": self.evidence, "assets": self.assets},
                                           "fixture", self.operation, **arguments)
            with self.assertRaises(FileExistsError):
                fence_candidate_containers(self.store, {"evidence": self.evidence, "assets": self.assets},
                                           "fixture", self.operation, **arguments)
        self.assertEqual(len(updates), 1)
        self.assertTrue((self.evidence / (self.operation + ".candidate-fence-attempt.json")).exists())
        self.assertFalse((self.evidence / (self.operation + ".candidate-fence-complete.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_activated_recovery_fences_containers_and_preserves_later_data(self):
        containers = self.candidate_fencing_fixture()
        private_write(self.evidence / (self.operation + ".backup.json"),
                      {"operation": self.operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        calls = []
        def command(argv, **kwargs):
            if argv[0] == "systemctl":
                calls.append("native-start")
                return b""
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            item = next(item for item in containers if item["Id"] == args[-1])
            if args[0] == "update":
                item["HostConfig"]["RestartPolicy"]["Name"] = "no"
            elif args[0] == "stop":
                item["State"]["Running"] = False
            else:
                self.fail(f"Unexpected Docker command: {args}")
            calls.append(args[0])
            return b""
        reviews = []
        def approve(review):
            reviews.append(review["plan"])
            return review["digest"]
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command), \
                patch.object(native, "check_host_writers"), patch.object(native, "verify_units"), \
                patch.object(native, "verify_release"), patch.object(native, "stop_native"), \
                patch.object(native, "check_no_writers"), patch.object(native, "runtime_checks"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"):
            result = recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                               checksum=self.backup.manifest_sha256, approve=approve, activated=True)
        self.assertEqual(len(reviews), 1)
        self.assertTrue(reviews[0]["activated"])
        self.assertEqual(len(reviews[0]["candidate_containers"]), 2)
        self.assertEqual(calls, ["update", "stop", "update", "stop", "native-start"])
        self.assertEqual(live.read_text(), "current state")
        self.assertEqual((live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
                          / "state.json").read_text(), "later edit")
        self.assertFalse(self.store.status()["blocked"])

    def test_activated_recovery_refuses_restarted_candidate_before_restore(self):
        containers = self.candidate_fencing_fixture()
        private_write(self.evidence / (self.operation + ".backup.json"),
                      {"operation": self.operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        def command(argv, **kwargs):
            args = argv[3:]
            if args[:1] == ["info"]:
                return b'{"ID":"engine"}'
            if args[:1] == ["ps"]:
                return ("\n".join(item["Id"] for item in containers) + "\n").encode()
            if args[:1] == ["inspect"]:
                return json.dumps(containers).encode()
            item = next(item for item in containers if item["Id"] == args[-1])
            if args[0] == "update":
                item["HostConfig"]["RestartPolicy"]["Name"] = "no"
            elif args[0] == "stop":
                item["State"]["Running"] = False
            else:
                self.fail(f"Unexpected Docker command: {args}")
            return b""
        def restart(*args):
            containers[0]["State"]["Running"] = True
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command", side_effect=command), \
                patch.object(native, "check_host_writers"), patch.object(native, "verify_units"), \
                patch.object(native, "verify_release"), patch.object(native, "stop_native"), \
                patch.object(native, "check_no_writers", side_effect=restart), \
                patch.object(native, "runtime_checks") as checks, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"):
            with self.assertRaisesRegex(source.NativeError, "Fenced candidate containers changed"):
                recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                           checksum=self.backup.manifest_sha256,
                                           approve=lambda review: review["digest"], activated=True)
            checks.assert_not_called()
        self.assertEqual(live.read_text(), "later edit")
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse((self.evidence / (self.operation + ".recovery-complete.json")).exists())

    def test_activated_recovery_decline_never_fences_containers(self):
        containers = self.candidate_fencing_fixture()
        private_write(self.evidence / (self.operation + ".backup.json"),
                      {"operation": self.operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, \
                patch("tool.deployment_pi_handover.inspect_candidate_containers", return_value=containers), \
                patch("tool.deployment_pi_handover.fence_candidate_containers") as fence, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)):
            with self.assertRaisesRegex(source.NativeError, "declined"):
                recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                           checksum=self.backup.manifest_sha256, approve=lambda review: None,
                                           activated=True)
            fence.assert_not_called()
            stop.assert_not_called()
        self.assertFalse((self.evidence / (self.operation + ".recovery-attempt.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    @unittest.skipUnless(os.environ.get("MPDRUSSOUNDPI_TEST_DOCKER_FENCING_IMAGE"),
                         "requires an explicitly selected local Docker test image")
    def test_candidate_fencing_against_real_docker(self):
        self.candidate_fencing_fixture()
        from tool.deployment_build import local_engine
        docker = ["docker", "--context", local_engine()]
        image = os.environ["MPDRUSSOUNDPI_TEST_DOCKER_FENCING_IMAGE"]
        image_id = subprocess.check_output(docker + ["image", "inspect", "--format", "{{.Id}}", image],
                                           text=True).strip()
        info = json.loads(subprocess.check_output(docker + ["info", "--format", "{{json .}}"]))
        state = self.store.status()
        loaded = {"web": image_id, "ser2net": image_id}
        state["deployments"]["fixture"]["pending"]["loaded_images"] = loaded
        store = Mock(spec=StateStore)
        store.status.return_value = state
        project = "pi-handover-" + self.operation.replace("-", "")
        _, config = prepare(self.spec, self.selection, self.images, project=project,
                            compose_path="/handover-compose.json")
        activation = self.evidence / (self.operation + ".handover-activation.json")
        activation.chmod(0o600)
        activation.write_bytes(encoded({"operation": self.operation, "intent_digest": INTENT,
                                        "project": project, "engine": info["ID"], "images": loaded,
                                        "config_digest": "sha256:" + hashlib.sha256(encoded(config)).hexdigest()}))
        activation.chmod(0o400)
        created = []
        for service in ("russound-web", "russound-ser2net"):
            container = subprocess.check_output(docker + [
                "run", "--detach", "--network", "none", "--restart", "unless-stopped",
                "--label", "com.docker.compose.project=" + project,
                "--label", "com.docker.compose.service=" + service,
                "--entrypoint", "python", image, "-c",
                "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *args: sys.exit(0)); time.sleep(300)",
            ], text=True).strip()
            created.append(container)
            self.addCleanup(subprocess.run, docker + ["rm", "--force", container],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with patch.object(native, "check_host_writers"):
            fence_candidate_containers(store, {"evidence": self.evidence, "assets": self.assets},
                                       "fixture", self.operation, native_spec=self.spec,
                                       expected_engine=info["ID"], approve=lambda review: review["digest"])
        inspected = json.loads(subprocess.check_output(docker + ["inspect", *created]))
        for item in inspected:
            self.assertFalse(item["State"]["Running"])
            self.assertEqual(item["HostConfig"]["RestartPolicy"]["Name"], "no")

    @unittest.skipUnless(os.environ.get("MPDRUSSOUNDPI_TEST_DOCKER_FENCING_IMAGE"),
                         "requires an explicitly selected local Docker test image")
    def test_activated_recovery_against_real_docker(self):
        self.candidate_fencing_fixture()
        from tool.deployment_build import local_engine
        docker = ["docker", "--context", local_engine()]
        image = os.environ["MPDRUSSOUNDPI_TEST_DOCKER_FENCING_IMAGE"]
        image_id = subprocess.check_output(docker + ["image", "inspect", "--format", "{{.Id}}", image],
                                           text=True).strip()
        info = json.loads(subprocess.check_output(docker + ["info", "--format", "{{json .}}"]))
        state = self.store.status()
        loaded = {"web": image_id, "ser2net": image_id}
        state["deployments"]["fixture"]["pending"]["loaded_images"] = loaded
        store = Mock(spec=StateStore)
        store.status.return_value = state
        project = "pi-handover-" + self.operation.replace("-", "")
        _, config = prepare(self.spec, self.selection, self.images, project=project,
                            compose_path="/handover-compose.json")
        activation = self.evidence / (self.operation + ".handover-activation.json")
        activation.chmod(0o600)
        activation.write_bytes(encoded({"operation": self.operation, "intent_digest": INTENT,
                                        "project": project, "engine": info["ID"], "images": loaded,
                                        "config_digest": "sha256:" + hashlib.sha256(encoded(config)).hexdigest()}))
        activation.chmod(0o400)
        private_write(self.evidence / (self.operation + ".backup.json"),
                      {"operation": self.operation, "manifest_sha256": self.backup.manifest_sha256})
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        created = []
        for service in ("russound-web", "russound-ser2net"):
            container = subprocess.check_output(docker + [
                "run", "--detach", "--network", "none", "--restart", "unless-stopped",
                "--label", "com.docker.compose.project=" + project,
                "--label", "com.docker.compose.service=" + service,
                "--entrypoint", "python", image, "-c",
                "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *args: sys.exit(0)); time.sleep(300)",
            ], text=True).strip()
            created.append(container)
            self.addCleanup(subprocess.run, docker + ["rm", "--force", container],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        original_command = native.command
        def command(argv, **kwargs):
            return b"" if argv[0] == "systemctl" else original_command(argv, **kwargs)
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        with patch("tool.deployment_pi_handover.StateStore", return_value=store), \
                patch.object(native, "command", side_effect=command), \
                patch.object(native, "check_host_writers"), patch.object(native, "verify_units"), \
                patch.object(native, "verify_release"), patch.object(native, "stop_native"), \
                patch.object(native, "check_no_writers"), patch.object(native, "runtime_checks"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"):
            result = recover_before_activation(storage, "fixture", self.operation, backup_path=self.backup.path,
                                               checksum=self.backup.manifest_sha256,
                                               approve=lambda review: review["digest"], activated=True)
        inspected = json.loads(subprocess.check_output(docker + ["inspect", *created]))
        self.assertTrue(all(not item["State"]["Running"] and item["HostConfig"]["RestartPolicy"]["Name"] == "no"
                            for item in inspected))
        self.assertEqual(live.read_text(), "current state")
        self.assertEqual((live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
                          / "state.json").read_text(), "later edit")
        store.resolve_recovery.assert_called_once()

    def test_backup_recovery_failure_keeps_evidence_and_does_not_retry(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("later edit")
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "command"), \
                patch.object(native, "runtime_checks", side_effect=source.NativeError("injected health failure")), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            arguments = {"backup_path": self.backup.path, "checksum": self.backup.manifest_sha256,
                         "approve": lambda review: review["digest"]}
            with self.assertRaisesRegex(source.NativeError, "injected health failure"):
                recover_before_activation(storage, "fixture", operation, **arguments)
            with self.assertRaises(FileExistsError):
                recover_before_activation(storage, "fixture", operation, **arguments)
            stop.assert_called_once()
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue((self.evidence / (operation + ".recovery-attempt.json")).exists())
        self.assertFalse((self.evidence / (operation + ".recovery-complete.json")).exists())
        retained = list(live.parent.parent.glob(live.parent.name + ".failed-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / "state.json").read_text(), "later edit")

    def test_backup_recovery_does_not_clear_changed_state_after_runtime_check(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": self.backup.manifest_sha256})
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        restores = self.root / "restores"
        restores.mkdir(mode=0o700)
        storage = {"assets": self.assets, "evidence": self.evidence,
                   "state": self.root / "state", "restores": restores}

        def change_state(*args):
            self.store.start_image_load("fixture", operation, self.images, INTENT)

        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "command"), \
                patch.object(native, "runtime_checks", side_effect=change_state), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            with self.assertRaisesRegex(source.NativeError, "state changed"):
                recover_before_activation(storage, "fixture", operation, backup_path=self.backup.path,
                                           checksum=self.backup.manifest_sha256,
                                           approve=lambda review: review["digest"])
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertFalse((self.evidence / (operation + ".recovery-complete.json")).exists())

    def test_controller_rejects_handover_backup_review_substitution(self):
        operation = self.operation
        request = {"action": "handover-recover-backup", "deployment": "fixture", "operation": operation}
        review = {"event": "review", "digest": INTENT,
                  "plan": {"kind": "pi-handover-backup-recovery", "operation": operation,
                           "deployment": "fixture", "backup_manifest_sha256": "0" * 64,
                           "restore_matching_data": True, "preserve_post_backup_tree": True}}
        source_stream = io.BytesIO()
        Channel(io.BytesIO(), source_stream).send(review)
        approve = Mock()
        with patch("tool.deployment_operator.send_backup"), patch("tool.deployment_operator.portable_backup"):
            with self.assertRaisesRegex(ChannelError, "different handover backup recovery"):
                controller(Channel(io.BytesIO(source_stream.getvalue()), io.BytesIO()),
                           request, self.evidence, approve, recovery=self.backup.path,
                           recovery_checksum=self.backup.manifest_sha256)
        approve.assert_not_called()

    def test_controller_rejects_activated_recovery_without_reviewed_scope(self):
        request = {"action": "handover-recover-activated", "deployment": "fixture",
                   "operation": self.operation}
        review = {"event": "review", "digest": INTENT,
                  "plan": {"kind": "pi-handover-backup-recovery", "operation": self.operation,
                           "deployment": "fixture", "backup_manifest_sha256": self.backup.manifest_sha256,
                           "restore_matching_data": True, "preserve_post_backup_tree": True,
                           "operator_confirmed_original_orchestration_ended": True, "activated": False}}
        source_stream = io.BytesIO()
        Channel(io.BytesIO(), source_stream).send(review)
        approve = Mock()
        with patch("tool.deployment_operator.send_backup"), patch("tool.deployment_operator.portable_backup"):
            with self.assertRaisesRegex(ChannelError, "different handover backup recovery"):
                controller(Channel(io.BytesIO(source_stream.getvalue()), io.BytesIO()),
                           request, self.evidence, approve, recovery=self.backup.path,
                           recovery_checksum=self.backup.manifest_sha256)
        approve.assert_not_called()

    def test_native_fencing_is_reserved_one_shot_and_disables_before_quiescence(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        units = {name: {"UnitFileState": "disabled"} for name in (native.WEB, native.GATEWAY)}
        with patch.object(native, "runtime_checks") as runtime_checks, \
                patch.object(native, "check_host_writers"), \
                patch.object(native, "command") as command, \
                patch.object(native, "verify_units"), \
                patch.object(native, "system_units", return_value=units), \
                patch.object(native, "check_no_writers") as quiet, \
                patch("tool.deployment_pi_handover.check_port") as ports:
            fence_native(self.store, {"assets": self.assets, "evidence": self.evidence},
                         "fixture", operation, committed=self.committed, native_spec=self.spec,
                         image_selection=self.images)
            runtime_checks.assert_called_once_with(self.spec, self.selection, self.native_operation)
            command.assert_called_once_with(
                ["systemctl", "disable", "--now", native.WEB, native.GATEWAY], timeout=90)
            quiet.assert_called_once_with(self.spec)
            self.assertEqual(ports.call_args_list,
                             [call(self.spec["publish_host"], self.spec["publish_port"]),
                              call("127.0.0.1", self.spec["backend_port"])])
            with self.assertRaises(FileExistsError):
                fence_native(self.store, {"assets": self.assets, "evidence": self.evidence},
                             "fixture", operation, committed=self.committed, native_spec=self.spec,
                             image_selection=self.images)
            self.assertEqual(command.call_count, 1)
        self.assertTrue((self.evidence / (operation + ".fence-attempt.json")).is_file())
        self.assertTrue(self.store.status()["blocked"])

    def test_native_fencing_rejects_unreserved_or_changed_assets_before_stop(self):
        operation = str(uuid4())
        storage = {"assets": self.assets, "evidence": self.evidence}
        with patch.object(native, "command") as command:
            with self.assertRaisesRegex(source.NativeError, "matching reserved"):
                fence_native(self.store, storage, "fixture", operation, committed=self.committed,
                             native_spec=self.spec, image_selection=self.images)
            self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                             expected_committed=self.committed)
            self.store.advance("fixture", operation, "inputs-verified")
            for name in ("spec.json", "selection.json"):
                (self.asset / name).chmod(0o600)
            changed = copy.deepcopy(self.spec)
            changed["serial_options"] = "9600n81"
            with self.assertRaisesRegex(source.NativeError, "committed assets"):
                fence_native(self.store, storage, "fixture", operation, committed=self.committed,
                             native_spec=changed, image_selection=self.images)
            command.assert_not_called()
        self.assertFalse((self.evidence / (operation + ".fence-attempt.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_native_fencing_failure_retains_attempt_and_reservation(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        with patch.object(native, "runtime_checks"), patch.object(native, "check_host_writers"), \
                patch.object(native, "command", side_effect=source.NativeError("injected stop failure")):
            with self.assertRaisesRegex(source.NativeError, "injected stop failure"):
                fence_native(self.store, {"assets": self.assets, "evidence": self.evidence},
                             "fixture", operation, committed=self.committed, native_spec=self.spec,
                             image_selection=self.images)
        self.assertTrue((self.evidence / (operation + ".fence-attempt.json")).exists())
        self.assertTrue(self.store.status()["blocked"])

    def test_native_fencing_rejects_units_that_remain_enabled(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        units = {native.WEB: {"UnitFileState": "disabled"},
                 native.GATEWAY: {"UnitFileState": "enabled"}}
        with patch.object(native, "runtime_checks"), patch.object(native, "check_host_writers"), \
                patch.object(native, "command"), patch.object(native, "verify_units"), \
                patch.object(native, "system_units", return_value=units), \
                patch.object(native, "check_no_writers") as quiet:
            with self.assertRaisesRegex(source.NativeError, "not disabled"):
                fence_native(self.store, {"assets": self.assets, "evidence": self.evidence},
                             "fixture", operation, committed=self.committed, native_spec=self.spec,
                             image_selection=self.images)
            quiet.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue((self.evidence / (operation + ".fence-attempt.json")).is_file())

    def test_prebackup_resume_preserves_data_and_resolves_only_after_checks(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        storage = {"assets": self.assets, "evidence": self.evidence, "state": self.root / "state"}
        file = Path(self.spec["data_dir"]) / "state.json"
        file.write_text("operator edit after stop")
        file.chmod(0o600)
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks") as checks, \
                patch.object(native, "command") as command, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            result = resume_before_backup(storage, "fixture", operation, lambda review: review["digest"])
        self.assertEqual(result["event"], "recovered")
        self.assertEqual(file.read_text(), "operator edit after stop")
        stop.assert_called_once()
        command.assert_called_once_with(["systemctl", "enable", "--now", native.GATEWAY, native.WEB], timeout=90)
        checks.assert_called_once_with(self.spec, self.selection, self.native_operation)
        state = self.store.status()
        self.assertFalse(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.committed)

    def test_declined_prebackup_resume_never_changes_services(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        storage = {"assets": self.assets, "evidence": self.evidence, "state": self.root / "state"}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            with self.assertRaisesRegex(source.NativeError, "declined"):
                resume_before_backup(storage, "fixture", operation, lambda review: None)
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse((self.evidence / (operation + ".recovery-attempt.json")).exists())

    def test_prebackup_resume_failure_keeps_reservation(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        storage = {"assets": self.assets, "evidence": self.evidence, "state": self.root / "state"}
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "command"), \
                patch.object(native, "runtime_checks", side_effect=source.NativeError("injected check failure")), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            with self.assertRaisesRegex(source.NativeError, "injected check failure"):
                resume_before_backup(storage, "fixture", operation, lambda review: review["digest"])
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue((self.evidence / (operation + ".recovery-attempt.json")).exists())
        self.assertFalse((self.evidence / (operation + ".recovery-complete.json")).exists())

    def test_prebackup_resume_rejects_asset_drift_after_review(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        storage = {"assets": self.assets, "evidence": self.evidence, "state": self.root / "state"}

        def approve(review):
            (self.asset / "spec.json").write_text("{}")
            return review["digest"]

        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native") as stop, \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"), \
                patch("tool.deployment_pi_handover.check_docker_conflicts"):
            with self.assertRaisesRegex(source.NativeError, "assets changed"):
                resume_before_backup(storage, "fixture", operation, approve)
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse((self.evidence / (operation + ".recovery-attempt.json")).exists())

    def test_running_or_always_restart_related_container_blocks_resume(self):
        item = {"Id": "container-id", "Mounts": [{"Type": "bind", "Source": self.spec["data_dir"]}],
                "HostConfig": {"Devices": [], "RestartPolicy": {"Name": "unless-stopped"}},
                "State": {"Running": False, "Restarting": False, "Paused": False}}
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command") as command:
            for field, value in (("Running", True), ("Restarting", True), ("Paused", True)):
                altered = copy.deepcopy(item)
                altered["State"][field] = value
                command.side_effect = [b'{"ID":"engine"}', b"container-id\n", json.dumps([altered]).encode()]
                with self.subTest(field=field), self.assertRaisesRegex(source.NativeError, "Docker container"):
                    check_docker_conflicts(self.spec)
            item["HostConfig"]["RestartPolicy"]["Name"] = "always"
            command.side_effect = [b'{"ID":"engine"}', b"container-id\n", json.dumps([item]).encode()]
            with self.assertRaisesRegex(source.NativeError, "Docker container"):
                check_docker_conflicts(self.spec)

    def test_docker_conflict_check_rejects_identity_drift_and_allows_unrelated_container(self):
        item = {"Id": "other-id", "Mounts": [],
                "HostConfig": {"Devices": [], "RestartPolicy": {"Name": "always"}},
                "State": {"Running": True, "Restarting": False, "Paused": False}}
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command") as command:
            command.side_effect = [b'{"ID":"engine"}', b"container-id\n", json.dumps([item]).encode()]
            with self.assertRaisesRegex(source.NativeError, "metadata"):
                check_docker_conflicts(self.spec)
            item["Id"] = "container-id"
            command.side_effect = [b'{"ID":"engine"}', b"container-id\n",
                                   json.dumps([item]).encode(), b'{"ID":"engine"}']
            self.assertEqual(check_docker_conflicts(self.spec), "engine")

    def test_docker_conflict_check_rejects_daemon_drift(self):
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command") as command:
            command.side_effect = [b'{"ID":"engine-a"}', b"", b'{"ID":"engine-b"}']
            with self.assertRaisesRegex(source.NativeError, "daemon changed"):
                check_docker_conflicts(self.spec)
            command.side_effect = [b'{"ID":"engine-b"}']
            with self.assertRaisesRegex(source.NativeError, "daemon changed"):
                check_docker_conflicts(self.spec, expected_engine="engine-a")

    def test_handover_conflicts_include_stopped_containers_and_project_collision(self):
        item = {"Id": "container-id", "Mounts": [],
                "Config": {"Labels": {"com.docker.compose.project": "old-project"}},
                "HostConfig": {"Devices": [], "RestartPolicy": {"Name": "unless-stopped"}},
                "State": {"Running": False, "Restarting": False, "Paused": False}}
        with patch("tool.deployment_pi_handover.local_engine", return_value="default"), \
                patch.object(native, "command") as command:
            for change in ("data", "device", "project", "malformed"):
                altered = copy.deepcopy(item)
                if change == "data":
                    altered["Mounts"] = [{"Type": "bind", "Source": self.spec["data_dir"]}]
                elif change == "device":
                    altered["HostConfig"]["Devices"] = [{"PathOnHost": self.spec["serial_device"]}]
                elif change == "project":
                    altered["Config"]["Labels"]["com.docker.compose.project"] = "new-project"
                else:
                    altered["Config"]["Labels"] = []
                command.side_effect = [b'{"ID":"engine"}', b"container-id\n", json.dumps([altered]).encode()]
                with self.subTest(change=change), self.assertRaises(source.NativeError):
                    check_docker_conflicts(self.spec, handover_project="new-project")
            command.side_effect = [b'{"ID":"engine"}', b"container-id\n",
                                   json.dumps([item]).encode(), b'{"ID":"engine"}']
            self.assertEqual(check_docker_conflicts(self.spec, handover_project="new-project"), "engine")

    def test_operator_routes_prebackup_resume_and_rejects_malformed_request(self):
        operation = str(uuid4())
        request = {"action": "handover-resume-original", "deployment": "fixture", "operation": operation}
        channel = Mock()
        channel.receive.return_value = request
        storage = {"state": self.root / "state"}
        with patch("tool.deployment_pi_handover.resume_before_backup") as resume:
            resume.return_value = {"event": "recovered", "operation": operation}
            serve(channel, storage)
        self.assertEqual(resume.call_args.args[:3], (storage, "fixture", operation))
        channel.send.assert_called_once_with({"event": "recovered", "operation": operation})
        channel.receive.return_value = {**request, "extra": True}
        with self.assertRaises(ChannelError):
            serve(channel, storage)

    def test_operator_routes_handover_backup_recovery(self):
        operation = self.operation
        request = {"action": "handover-recover-backup", "deployment": "fixture", "operation": operation}
        channel = Mock()
        channel.receive.side_effect = [request, {"event": "backup", "operation": operation}]
        incoming = self.root / "incoming"
        incoming.mkdir(mode=0o700)
        storage = {"incoming": incoming, "restores": self.root}
        with patch("tool.deployment_operator.receive_backup",
                   return_value=(self.backup.path, self.backup.manifest_sha256)), \
                patch("tool.deployment_pi_handover.recover_before_activation") as recover:
            recover.return_value = {"event": "recovered", "operation": operation}
            serve(channel, storage)
        self.assertEqual(recover.call_args.args, (storage, "fixture", operation))
        self.assertEqual(recover.call_args.kwargs["checksum"], self.backup.manifest_sha256)
        self.assertFalse(recover.call_args.kwargs["activated"])
        channel.send.assert_called_once_with({"event": "recovered", "operation": operation})

    def test_operator_rejects_mismatched_backup_before_creating_receive_directory(self):
        request = {"action": "handover-recover-backup", "deployment": "fixture",
                   "operation": self.operation}
        channel = Mock()
        channel.receive.side_effect = [request, {"event": "backup", "operation": str(uuid4())}]
        incoming = self.root / "incoming"
        incoming.mkdir(mode=0o700)
        with patch("tool.deployment_operator.receive_backup") as receive, \
                self.assertRaisesRegex(ChannelError, "operation and backup differ"):
            serve(channel, {"incoming": incoming, "restores": self.root})
        receive.assert_not_called()
        self.assertEqual(list(incoming.iterdir()), [])

    def test_operator_routes_activated_recovery(self):
        operation = self.operation
        request = {"action": "handover-recover-activated", "deployment": "fixture", "operation": operation}
        channel = Mock()
        channel.receive.side_effect = [request, {"event": "backup", "operation": operation}]
        incoming = self.root / "incoming"
        incoming.mkdir(mode=0o700)
        storage = {"incoming": incoming, "restores": self.root}
        with patch("tool.deployment_operator.receive_backup",
                   return_value=(self.backup.path, self.backup.manifest_sha256)), \
                patch("tool.deployment_pi_handover.recover_before_activation") as recover:
            recover.return_value = {"event": "recovered", "operation": operation}
            serve(channel, storage)
        self.assertTrue(recover.call_args.kwargs["activated"])
        channel.send.assert_called_once_with({"event": "recovered", "operation": operation})

    def test_controller_rejects_resume_scope_that_replaces_data(self):
        operation = str(uuid4())
        request = {"action": "handover-resume-original", "deployment": "fixture", "operation": operation}
        review = {"event": "review", "digest": INTENT,
                  "plan": {"kind": "pi-handover-resume-original", "operation": operation,
                           "deployment": "fixture", "restore_data": True, "preserve_operator_edits": True}}
        source_stream = io.BytesIO()
        Channel(io.BytesIO(), source_stream).send(review)
        approve = Mock()
        with self.assertRaisesRegex(ChannelError, "different handover recovery"):
            controller(Channel(io.BytesIO(source_stream.getvalue()), io.BytesIO()),
                       request, self.evidence, approve)
        approve.assert_not_called()

    def test_nonvoid_offhost_verification_never_advances(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        with self.assertRaisesRegex(source.NativeError, "Off-host verification"):
            capture_backup(
                self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
                "fixture", operation, committed=self.committed, native_spec=self.spec,
                image_selection=self.images, native_asset=self.asset,
                verify_quiescent=lambda: None, verify_offhost=lambda backup: False,
            )
        pending = self.store.status()["deployments"]["fixture"]["pending"]
        self.assertEqual(pending["phase"], "inputs-verified")
        self.assertTrue(self.store.status()["blocked"])
        self.assertFalse((self.evidence / (operation + ".backup.json")).exists())

    def test_interrupted_offhost_transfer_keeps_reservation_and_backup(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        def interrupted(backup):
            raise OSError("transfer interrupted")
        with self.assertRaisesRegex(OSError, "transfer interrupted"):
            capture_backup(
                self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
                "fixture", operation, committed=self.committed, native_spec=self.spec,
                image_selection=self.images, native_asset=self.asset,
                verify_quiescent=lambda: None, verify_offhost=interrupted,
            )
        self.assertTrue((self.backups / operation).exists())
        self.assertTrue(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"]["phase"], "inputs-verified")

    def test_channel_custody_is_verified_and_persisted_before_acknowledgment(self):
        outbound = io.BytesIO()
        expected = {"event": "backup-verified", "operation": self.operation,
                    "manifest_sha256": self.backup.manifest_sha256}
        acknowledgment = io.BytesIO()
        Channel(io.BytesIO(), acknowledgment).send(expected)
        send_handover_backup(Channel(io.BytesIO(acknowledgment.getvalue()), outbound), self.backup)
        controller_ack = io.BytesIO()
        controller = Channel(io.BytesIO(outbound.getvalue()), controller_ack)
        header = controller.receive()
        received, checksum = receive_handover_backup(
            controller, self.offhost, header, operation=self.operation, committed=self.committed,
            native_spec=self.spec, image_selection=self.images, native_asset=self.asset,
        )
        self.assertEqual(checksum, self.backup.manifest_sha256)
        self.assertTrue(received.is_dir())
        identity = json.loads((self.offhost / (self.operation + ".backup-identity.json")).read_bytes())
        self.assertEqual(identity, {"operation": self.operation, "manifest_sha256": checksum})
        self.assertEqual(Channel(io.BytesIO(controller_ack.getvalue()), io.BytesIO()).receive(), expected)

    def test_controller_rejects_mismatched_review_and_truncated_stream_without_ack(self):
        outgoing = io.BytesIO()
        acknowledgment = io.BytesIO()
        Channel(io.BytesIO(), acknowledgment).send(
            {"event": "backup-verified", "operation": self.operation,
             "manifest_sha256": self.backup.manifest_sha256})
        send_handover_backup(Channel(io.BytesIO(acknowledgment.getvalue()), outgoing), self.backup)
        wire = outgoing.getvalue()
        changed = copy.deepcopy(self.spec)
        changed["serial_options"] = "9600n81"
        for data, spec, operation in ((wire, changed, self.operation),
                                      (wire[:-1], self.spec, self.operation),
                                      (wire, self.spec, str(uuid4()))):
            with self.subTest(operation=operation, truncated=len(data) != len(wire)):
                parent = self.root / str(uuid4())
                parent.mkdir(mode=0o700)
                reply = io.BytesIO()
                controller = Channel(io.BytesIO(data), reply)
                header = controller.receive()
                with self.assertRaises((ValueError, EOFError)):
                    receive_handover_backup(controller, parent, header, operation=operation,
                                            committed=self.committed, native_spec=spec,
                                            image_selection=self.images, native_asset=self.asset)
                self.assertEqual(reply.getvalue(), b"")
                self.assertFalse((parent / (operation + ".backup-identity.json")).exists())

    def test_target_rejects_mismatched_or_missing_acknowledgment(self):
        for response in (None, {"event": "backup-verified", "operation": str(uuid4()),
                                "manifest_sha256": self.backup.manifest_sha256},
                         {"event": "backup-verified", "operation": self.operation,
                          "manifest_sha256": "0" * 64}):
            reply = io.BytesIO()
            if response is not None:
                Channel(io.BytesIO(), reply).send(response)
            with self.subTest(response=response), self.assertRaises(ChannelError):
                send_handover_backup(Channel(io.BytesIO(reply.getvalue()), io.BytesIO()), self.backup)

    def test_reserved_capture_transfers_and_verifies_over_duplex_channel(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        target_socket, controller_socket = socket.socketpair()
        target_socket.settimeout(10)
        controller_socket.settimeout(10)
        failures = []
        def controller():
            try:
                with controller_socket, controller_socket.makefile("rb") as source_stream, \
                        controller_socket.makefile("wb") as target_stream:
                    channel = Channel(source_stream, target_stream)
                    receive_handover_backup(
                        channel, self.offhost, channel.receive(), operation=operation,
                        committed=self.committed, native_spec=self.spec,
                        image_selection=self.images, native_asset=self.asset,
                    )
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=controller)
        worker.start()
        try:
            with target_socket, target_socket.makefile("rb") as source_stream, \
                    target_socket.makefile("wb") as target_stream:
                channel = Channel(source_stream, target_stream)
                capture_backup(
                    self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
                    "fixture", operation, committed=self.committed, native_spec=self.spec,
                    image_selection=self.images, native_asset=self.asset,
                    verify_quiescent=lambda: None,
                    verify_offhost=lambda backup: send_handover_backup(channel, backup),
                )
        finally:
            worker.join(timeout=12)
        self.assertFalse(worker.is_alive(), "Controller transfer did not finish")
        self.assertEqual(failures, [])
        self.assertTrue((self.offhost / operation / "manifest.json").is_file())
        self.assertTrue((self.offhost / (operation + ".backup-identity.json")).is_file())
        self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"]["phase"], "backup-complete")

    def test_apply_failure_recovers_from_independently_received_backup(self):
        storage, arguments, _ = self.transaction_fixture(failure="apply")
        for name in ("spec.json", "selection.json"):
            (self.asset / name).chmod(0o600)
        live = Path(self.spec["data_dir"]) / "state.json"
        live.write_text("fresh before backup")
        target_socket, controller_socket = socket.socketpair()
        target_socket.settimeout(10)
        controller_socket.settimeout(10)
        received = []
        failures = []

        def controller_backup():
            try:
                with controller_socket, controller_socket.makefile("rb") as source_stream, \
                        controller_socket.makefile("wb") as target_stream:
                    channel = Channel(source_stream, target_stream)
                    received.append(receive_handover_backup(
                        channel, self.offhost, channel.receive(), operation=self.operation,
                        committed=self.committed, native_spec=self.spec,
                        image_selection=self.images, native_asset=self.asset))
            except Exception as error:
                failures.append(error)

        def capture():
            worker = threading.Thread(target=controller_backup)
            worker.start()
            try:
                with target_socket, target_socket.makefile("rb") as source_stream, \
                        target_socket.makefile("wb") as target_stream:
                    channel = Channel(source_stream, target_stream)
                    capture_backup(
                        self.store, storage, "fixture", self.operation, committed=self.committed,
                        native_spec=self.spec, image_selection=self.images, native_asset=self.asset,
                        verify_quiescent=lambda: None,
                        verify_offhost=lambda backup: send_handover_backup(channel, backup))
            finally:
                worker.join(timeout=12)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

        hooks = arguments["hooks"]
        arguments["hooks"] = HandoverHooks(
            preflight=hooks.preflight, receive_images=hooks.receive_images, stop_native=hooks.stop_native,
            capture_backup=capture, load_images=hooks.load_images, apply=hooks.apply, checks=hooks.checks)
        with self.assertRaisesRegex(source.NativeError, "injected apply"):
            execute_handover(self.store, storage, "fixture", self.operation, **arguments)
        self.assertEqual(len(received), 1)
        backup_path, checksum = received[0]
        self.assertEqual(backup_path, self.offhost / self.operation)
        self.assertEqual(json.loads((self.offhost / (self.operation + ".backup-identity.json")).read_bytes()),
                         {"operation": self.operation, "manifest_sha256": checksum})
        live.write_text("post-backup change")
        restores = self.root / "offhost-recovery-restores"
        restores.mkdir(mode=0o700)
        storage.update({"state": self.root / "state", "restores": restores})
        with patch.object(native, "verify_units"), patch.object(native, "verify_release"), \
                patch.object(native, "stop_native"), patch.object(native, "check_no_writers"), \
                patch.object(native, "check_host_writers"), patch.object(native, "runtime_checks"), \
                patch.object(native, "command"), \
                patch("tool.deployment_pi_handover.inspect_candidate_containers", return_value=[]), \
                patch("tool.deployment_pi_handover.check_docker_conflicts", return_value="engine"), \
                patch("tool.deployment_pi_handover.safe_path",
                      return_value=Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0)), \
                patch("tool.deployment_pi_handover.check_data"):
            result = recover_before_activation(
                storage, "fixture", self.operation, backup_path=backup_path, checksum=checksum,
                approve=lambda review: review["digest"], activated=True)
        self.assertEqual(live.read_text(), "fresh before backup")
        preserved = live.parent.with_name(live.parent.name + ".failed-" + result["recovery"])
        self.assertEqual((preserved / "state.json").read_text(), "post-backup change")
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.committed)

    def test_operator_handover_protocol_transfers_backup_and_commits(self):
        target = self.target_fixture()
        storage = target.storage
        storage["runtime"] = self.runtime
        operation = target.operation
        request = {"action": "handover-deploy", "deployment": "fixture", "operation": operation,
                   "native_spec": self.spec, "committed": self.committed, "selection": self.images}
        bundle = self.root / self.images["manifest"]["operation"]
        target_socket, controller_socket = socket.socketpair()
        target_socket.settimeout(15)
        controller_socket.settimeout(15)
        failures = []

        def preflight(instance):
            instance.engine = "engine"
            instance.backup_estimate = 1024

        def load(instance, digest):
            instance.store.start_image_load("fixture", operation, self.images, digest)
            images = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
            instance.store.complete_image_load("fixture", operation, self.images, digest, images)
            return images

        def activate(instance, images):
            active = copy.deepcopy(instance.config)
            for name, component in instance.services.items():
                active["services"][name]["image"] = images[component]
            private_write(instance.storage["runtime"] / operation / "active.json", active)

        def target_worker():
            try:
                with target_socket, target_socket.makefile("rb") as source_stream, \
                        target_socket.makefile("wb") as target_stream:
                    serve(Channel(source_stream, target_stream), storage)
            except Exception as error:
                failures.append(error)

        with patch("tool.deployment_operator.STORAGE", self.root), \
                patch.object(HandoverTarget, "preflight", preflight), \
                patch.object(HandoverTarget, "stop_native", return_value=None), \
                patch.object(HandoverTarget, "quiescent", return_value=None), \
                patch.object(HandoverTarget, "load_images", load), \
                patch.object(HandoverTarget, "activate", activate), \
                patch.object(HandoverTarget, "checks",
                             return_value=dict.fromkeys(("containers", "http", "backend", "playback"), "passed")):
            worker = threading.Thread(target=target_worker)
            worker.start()
            try:
                with controller_socket, controller_socket.makefile("rb") as source_stream, \
                        controller_socket.makefile("wb") as target_stream:
                    result = controller(Channel(source_stream, target_stream), request, self.offhost,
                                        lambda review: review["digest"], bundle=bundle)
            finally:
                worker.join(timeout=17)
                if failures:
                    raise failures[0]
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result["event"], "complete")
        self.assertFalse(self.store.status()["blocked"])
        self.assertTrue((self.offhost / operation / "manifest.json").is_file())
        self.assertTrue((self.offhost / (operation + ".backup-identity.json")).is_file())
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"]["selection"]["candidate"],
                         self.images)
        next_spec, _ = prepare(self.spec, self.selection, self.images,
                               project="pi-handover-" + operation.replace("-", ""),
                               compose_path=str(self.runtime / operation / "compose.json"))
        with patch("tool.deployment_compose.local_engine", return_value="default"):
            next_deployment = ComposeDeployment(next_spec, self.images, storage, operation=str(uuid4()))
        self.assertEqual(next_deployment.config,
                         json.loads((self.runtime / operation / "active.json").read_bytes()))

    def test_controller_rejects_altered_handover_review_and_digest(self):
        operation = str(uuid4())
        request = {"action": "handover-deploy", "deployment": "fixture", "operation": operation,
                   "native_spec": self.spec, "committed": self.committed, "selection": self.images}
        project = "pi-handover-" + operation.replace("-", "")
        spec, config = prepare(self.spec, self.selection, self.images, project=project,
                               compose_path=str(self.root / "runtime" / operation / "compose.json"))
        plan = {"kind": "pi-handover-transaction", "deployment": "fixture", "operation": operation,
                "committed": self.committed, "selection": self.images, "native_spec": self.spec,
                "compose_spec": spec, "compose_config": config, "engine": "engine",
                "interrupt_native": True, "preserve_data_in_place": True, "offhost_backup_required": True,
                "automatic_rollback": False, "offhost_free_bytes_required": 1024}
        for change in ("native", "config", "digest"):
            altered = copy.deepcopy(plan)
            if change == "native":
                altered["native_spec"]["serial_options"] = "9600n81"
            elif change == "config":
                altered["compose_config"]["name"] = "different"
            digest = "sha256:" + hashlib.sha256(encoded(altered)).hexdigest()
            if change == "digest":
                digest = "sha256:" + "f" * 64
            wire = io.BytesIO()
            Channel(io.BytesIO(), wire).send({"event": "review", "plan": altered, "digest": digest})
            approve = Mock()
            with self.subTest(change=change), patch("tool.deployment_operator.STORAGE", self.root):
                with self.assertRaisesRegex(ChannelError, "different handover deployment scope"):
                    controller(Channel(io.BytesIO(wire.getvalue()), io.BytesIO()),
                               request, self.offhost, approve)
            approve.assert_not_called()

    def test_lost_custody_ack_retains_both_copies_and_blocks_activation(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        target_socket, controller_socket = socket.socketpair()
        target_socket.settimeout(10)
        controller_socket.settimeout(10)
        failures = []

        def controller_backup():
            try:
                with controller_socket, controller_socket.makefile("rb") as source_stream, \
                        controller_socket.makefile("wb") as target_stream:
                    channel = Channel(source_stream, target_stream)
                    with patch.object(channel, "send", side_effect=ChannelError("acknowledgment lost")):
                        receive_handover_backup(
                            channel, self.offhost, channel.receive(), operation=operation,
                            committed=self.committed, native_spec=self.spec,
                            image_selection=self.images, native_asset=self.asset)
            except ChannelError as error:
                failures.append(str(error))

        worker = threading.Thread(target=controller_backup)
        worker.start()
        try:
            with target_socket, target_socket.makefile("rb") as source_stream, \
                    target_socket.makefile("wb") as target_stream:
                channel = Channel(source_stream, target_stream)
                with self.assertRaises(ChannelError):
                    capture_backup(
                        self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
                        "fixture", operation, committed=self.committed, native_spec=self.spec,
                        image_selection=self.images, native_asset=self.asset,
                        verify_quiescent=lambda: None,
                        verify_offhost=lambda backup: send_handover_backup(channel, backup))
        finally:
            worker.join(timeout=12)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ["acknowledgment lost"])
        self.assertTrue((self.backups / operation / "manifest.json").is_file())
        self.assertTrue((self.offhost / operation / "manifest.json").is_file())
        self.assertTrue((self.offhost / (operation + ".backup-identity.json")).is_file())
        self.assertFalse((self.evidence / (operation + ".backup.json")).exists())
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertFalse(state["image_load_started"])
        self.assertEqual(state["deployments"]["fixture"]["pending"]["phase"], "inputs-verified")

    def test_capture_rejects_nonquiescent_writer_before_creating_backup(self):
        operation = str(uuid4())
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        def nonquiescent():
            raise source.NativeError("writer active")
        with self.assertRaisesRegex(source.NativeError, "writer active"):
            capture_backup(
                self.store, {"assets": self.assets, "backups": self.backups, "evidence": self.evidence},
                "fixture", operation, committed=self.committed, native_spec=self.spec,
                image_selection=self.images, native_asset=self.asset,
                verify_quiescent=nonquiescent, verify_offhost=lambda backup: None,
            )
        self.assertFalse((self.backups / operation).exists())
        self.assertTrue(self.store.status()["blocked"])


if __name__ == "__main__":
    unittest.main()
