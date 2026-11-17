import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_deployment_build import fixture_bundle
from test_deployment_native import selection_fixture, spec_fixture
from tool import deployment_native as native
from tool import deployment_native_source as source
from tool.deployment_backup import FileBackup
from tool.deployment_load import select_bundle
from tool.deployment_pi_handover import capture_backup, verify_backup
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
        with self.assertRaisesRegex(StateError, "Only native recovery"):
            self.store.resolve_recovery("fixture", self.operation, receipt,
                                        restore_native=True, native_previous=self.previous)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.previous)
        self.assertEqual(state["deployments"]["fixture"]["pending"]["operation"], self.operation)

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
