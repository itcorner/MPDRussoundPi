import copy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch
from uuid import uuid4

from test_deployment_build import fixture_bundle
from test_deployment_native import selection_fixture, spec_fixture
from tool import deployment_native as native
from tool import deployment_native_source as source
from tool.deployment_backup import FileBackup
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import private_write
from tool.deployment_load import select_bundle
from tool.deployment_operator import controller, serve
from tool.deployment_pi_handover import (
    capture_backup, check_docker_conflicts, fence_native, receive_handover_backup,
    prepare_backup_restore, recover_before_activation, resume_before_backup, send_handover_backup, verify_backup,
)
from tool.deployment_state import PHASES, StateError, StateStore


INTENT = "sha256:" + "d" * 64


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
            with self.assertRaisesRegex(source.NativeError, "before image load"):
                recover_before_activation(
                    {"state": self.root / "state", "evidence": self.evidence}, "fixture", operation,
                    backup_path=self.backup.path, checksum=self.backup.manifest_sha256,
                    approve=lambda review: review["digest"])
            stop.assert_not_called()
        self.assertTrue(self.store.status()["blocked"])

    def test_backup_recovery_rejects_apply_attempt_before_any_service_change(self):
        operation = self.operation
        self.store.begin("fixture", operation, self.images, INTENT, ["apply", "containers"],
                         expected_committed=self.committed)
        self.store.advance("fixture", operation, "inputs-verified")
        self.store.advance("fixture", operation, "backup-complete", backup=operation)
        private_write(self.evidence / (operation + ".apply-attempt.json"), {"operation": operation})
        with patch.object(native, "stop_native") as stop:
            with self.assertRaisesRegex(source.NativeError, "before image load"):
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
        channel.receive.side_effect = [request, {"event": "backup"}]
        incoming = self.root / "incoming"
        incoming.mkdir(mode=0o700)
        storage = {"incoming": incoming}
        with patch("tool.deployment_operator.receive_backup",
                   return_value=(self.backup.path, self.backup.manifest_sha256)), \
                patch("tool.deployment_pi_handover.recover_before_activation") as recover:
            recover.return_value = {"event": "recovered", "operation": operation}
            serve(channel, storage)
        self.assertEqual(recover.call_args.args, (storage, "fixture", operation))
        self.assertEqual(recover.call_args.kwargs["checksum"], self.backup.manifest_sha256)
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
