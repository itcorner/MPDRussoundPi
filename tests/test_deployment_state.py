import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool.deployment_images import resolve
from tool.deployment_state import PHASES, StateError, StateStore
from test_deployment_images import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]
INTENT = "sha256:" + "d" * 64


@unittest.skipUnless(os.name == "posix", "deployment state requires a local POSIX filesystem")
class DeploymentStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-state-")
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "state"
        self.store = StateStore(self.root, owner_uid=os.getuid())
        self.store.initialize()
        self.operation = str(uuid4())
        self.selection = resolve("external", "linux/arm64", FixtureRegistry())
        self.required = ["apply", "web-health", "backend-routing"]

    def begin(self, deployment="demo", operation=None):
        self.store.begin(deployment, operation or self.operation, self.selection, INTENT, self.required)

    def ready(self):
        for phase in PHASES[1:]:
            self.store.advance("demo", self.operation, phase)

    def receipt(self):
        return {"operation": self.operation, "intent_digest": INTENT,
                "invocation": "verified-complete",
                "required": dict.fromkeys(self.required, "passed")}

    def record(self):
        return json.loads((self.root / "demo.json").read_bytes())

    def overwrite_record(self, value):
        (self.root / "demo.json").write_text(json.dumps(value))

    def test_successful_separate_finalization_and_next_reservation(self):
        self.assertFalse(self.store.status()["blocked"])
        self.begin()
        pending = self.record()["pending"]
        self.assertEqual(pending["phase"], "prepared")
        self.assertEqual(pending["candidate"], self.selection)
        self.assertIsNone(self.record()["committed"])
        self.assertTrue(self.store.status()["blocked"])
        self.ready()
        self.assertTrue(self.store.status()["blocked"])
        self.store.finalize("demo", self.operation, self.receipt())
        state = self.store.status()
        self.assertFalse(state["blocked"])
        self.assertFalse(state["runtime_verified"])
        committed = self.record()["committed"]
        self.assertEqual(committed["selection"]["candidate"], self.selection)
        self.assertIsNone(self.record()["pending"])
        self.assertFalse((self.root / "reservation").exists())
        with self.assertRaises(StateError):
            self.store.finalize("demo", self.operation, self.receipt())
        self.operation = str(uuid4())
        self.begin()
        self.assertEqual(self.record()["committed"], committed)

    def test_hostwide_exclusion_includes_other_deployment_ids(self):
        self.begin()
        original = (self.root / "demo.json").read_bytes()
        for deployment in ("demo", "different"):
            with self.subTest(deployment=deployment), self.assertRaisesRegex(StateError, "existing reservation"):
                self.begin(deployment, str(uuid4()))
        self.assertEqual((self.root / "demo.json").read_bytes(), original)
        self.assertFalse((self.root / "different.json").exists())

    def test_committed_operation_identity_cannot_be_replayed_under_another_deployment(self):
        self.begin()
        self.ready()
        self.store.finalize("demo", self.operation, self.receipt())
        previous = (self.root / "demo.json").read_bytes()
        with self.assertRaisesRegex(StateError, "already committed"):
            self.begin("different")
        self.assertEqual((self.root / "demo.json").read_bytes(), previous)
        self.assertFalse((self.root / "different.json").exists())
        self.assertTrue((self.root / "reservation").is_dir())

    def test_owner_and_record_files_are_private(self):
        self.begin()
        for path, mode in ((self.root, 0o700), (self.root / "reservation", 0o700),
                           (self.root / "reservation/owner.json", 0o600),
                           (self.root / "demo.json", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual(path.stat().st_uid, os.getuid())

    def test_invalid_requests_do_not_acquire_reservation(self):
        for deployment, operation, intent, required in (
            ("../demo", self.operation, INTENT, self.required),
            ("Demo", self.operation, INTENT, self.required),
            ("demo", "invalid", INTENT, self.required),
            ("demo", self.operation, "invalid", self.required),
            ("demo", self.operation, INTENT, []),
            ("demo", self.operation, INTENT, ["apply", "apply"]),
            ("demo", self.operation, INTENT, [["invalid"]]),
        ):
            with self.subTest(deployment=deployment, required=required), self.assertRaises(StateError):
                self.store.begin(deployment, operation, self.selection, intent, required)
            self.assertEqual(list(self.root.iterdir()), [])
        malformed = copy.deepcopy(self.selection)
        malformed["images"]["web"]["reference"] = "web:dev"
        with self.assertRaises(StateError):
            self.store.begin("demo", self.operation, malformed, INTENT, self.required)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wrong_owner_or_skipped_phase_cannot_change_record(self):
        self.begin()
        original = (self.root / "demo.json").read_bytes()
        for deployment, operation, phase in (
            ("different", self.operation, "inputs-verified"),
            ("demo", str(uuid4()), "inputs-verified"),
            ("demo", self.operation, "apply-complete"),
            ("demo", self.operation, "unknown"),
        ):
            with self.subTest(phase=phase), self.assertRaises(StateError):
                self.store.advance(deployment, operation, phase)
            self.assertEqual((self.root / "demo.json").read_bytes(), original)
        self.assertTrue((self.root / "reservation").exists())

    def test_invalid_reservation_schema_cannot_advance(self):
        self.begin()
        owner_path = self.root / "reservation/owner.json"
        owner = json.loads(owner_path.read_bytes())
        owner["schema_version"] = True
        owner_path.write_text(json.dumps(owner))
        with self.assertRaises(StateError):
            self.store.advance("demo", self.operation, "inputs-verified")
        with self.assertRaises(StateError):
            self.store.status()
        self.assertEqual(self.record()["pending"]["phase"], "prepared")

    def test_early_incomplete_or_mismatched_completion_keeps_pending(self):
        self.begin()
        with self.assertRaises(StateError):
            self.store.finalize("demo", self.operation, self.receipt())
        self.ready()
        original = (self.root / "demo.json").read_bytes()
        for overrides in (
            {"operation": str(uuid4())}, {"intent_digest": "sha256:" + "0" * 64},
            {"invocation": "exited-zero"}, {"required": {}},
            {"required": {**dict.fromkeys(self.required, "passed"), "unexpected": "passed"}},
            *({"required": {**dict.fromkeys(self.required, "passed"), "apply": result}}
              for result in ("failed", "unknown", "not-needed", "rescued", "unreachable")),
        ):
            with self.subTest(overrides=overrides), self.assertRaises(StateError):
                self.store.finalize("demo", self.operation, {**self.receipt(), **overrides})
            self.assertEqual((self.root / "demo.json").read_bytes(), original)
            self.assertFalse((self.root / "reservation/finalizing.json").exists())

    def test_existing_commit_is_preserved_and_requires_backup(self):
        self.begin()
        self.ready()
        self.store.finalize("demo", self.operation, self.receipt())
        previous = self.record()["committed"]
        self.operation = str(uuid4())
        self.begin()
        self.store.advance("demo", self.operation, "inputs-verified")
        with self.assertRaisesRegex(StateError, "requires a backup"):
            self.store.advance("demo", self.operation, "backup-complete")
        self.assertEqual(self.record()["pending"]["phase"], "inputs-verified")
        backup = str(uuid4())
        self.store.advance("demo", self.operation, "backup-complete", backup=backup)
        self.assertEqual(self.record()["pending"]["backup"], backup)
        self.assertEqual(self.record()["committed"], previous)

    def test_empty_reservation_survives_and_blocks_without_age_checks(self):
        reservation = self.root / "reservation"
        reservation.mkdir(mode=0o700)
        os.utime(reservation, (1, 1))
        with self.assertRaises(StateError):
            self.store.status()
        with self.assertRaisesRegex(StateError, "existing reservation"):
            self.begin()
        self.assertTrue(reservation.exists())
        self.assertEqual(list(reservation.iterdir()), [])

    def test_orphan_pending_record_blocks_all_deployment_ids(self):
        self.begin()
        (self.root / "reservation/owner.json").unlink()
        (self.root / "reservation").rmdir()
        self.assertTrue(self.store.status()["blocked"])
        with self.assertRaisesRegex(StateError, "unresolved pending"):
            self.begin("different", str(uuid4()))
        self.assertTrue((self.root / "reservation").exists())
        self.assertIsNotNone(self.record()["pending"])
        self.assertFalse((self.root / "different.json").exists())

    def test_symlink_state_root_ancestor_and_record_are_rejected(self):
        link = self.parent / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        for path in (link, link / "child", self.root / ".." / "state"):
            with self.subTest(path=path), self.assertRaises(StateError):
                StateStore(path, owner_uid=os.getuid()).status()
        self.begin()
        original = (self.root / "demo.json").read_bytes()
        outside = self.parent / "outside.json"
        outside.write_bytes(original)
        outside.chmod(0o600)
        (self.root / "demo.json").unlink()
        (self.root / "demo.json").symlink_to(outside)
        with self.assertRaises(StateError):
            self.store.status()
        self.assertEqual(outside.read_bytes(), original)

    def test_unsafe_permissions_ownership_and_hardlinks_are_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(StateError):
            self.store.initialize()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.root.chmod(0o700)
        with self.assertRaises(StateError):
            StateStore(self.root, owner_uid=os.getuid() + 1).status()
        self.begin()
        record = self.root / "demo.json"
        record.chmod(0o644)
        with self.assertRaises(StateError):
            self.store.status()
        record.chmod(0o600)
        os.link(record, self.parent / "hardlink")
        with self.assertRaises(StateError):
            self.store.status()

    def test_fifo_record_does_not_hang(self):
        os.mkfifo(self.root / "demo.json", 0o600)
        with self.assertRaises(StateError):
            self.store.status()

    def test_corrupt_schema_and_leftover_staging_block(self):
        self.begin()
        original = self.record()
        for value in ({"schema_version": 1}, {**original, "schema_version": True},
                      {**original, "deployment": "other"},
                      {**original, "pending": {**original["pending"], "phase": "unknown"}}):
            self.overwrite_record(value)
            with self.subTest(value=value), self.assertRaises(StateError):
                self.store.status()
        (self.root / "demo.json").write_text("{invalid")
        with self.assertRaises(StateError):
            self.store.status()
        self.overwrite_record(original)
        (self.root / "demo.json.new").write_text("interrupted")
        with self.assertRaises(StateError):
            self.store.status()

    def test_reservation_persistence_error_never_creates_pending_or_releases(self):
        with patch("tool.deployment_state.os.fsync", side_effect=OSError("test I/O failure")):
            with self.assertRaisesRegex(StateError, "uncertain"):
                self.begin()
        self.assertTrue((self.root / "reservation").is_dir())
        self.assertFalse((self.root / "demo.json").exists())
        with self.assertRaises(StateError):
            self.begin()

    def test_pending_write_error_retains_owner_and_previous_commit(self):
        self.begin()
        self.ready()
        self.store.finalize("demo", self.operation, self.receipt())
        previous = (self.root / "demo.json").read_bytes()
        self.operation = str(uuid4())
        original_write = self.store._write

        def fail_pending(directory, name, value, **kwargs):
            if name == "demo.json":
                raise OSError("test disk full")
            return original_write(directory, name, value, **kwargs)

        with patch.object(self.store, "_write", side_effect=fail_pending):
            with self.assertRaisesRegex(StateError, "uncertain"):
                self.begin()
        self.assertEqual((self.root / "demo.json").read_bytes(), previous)
        self.assertTrue((self.root / "reservation/owner.json").is_file())
        self.assertTrue(self.store.status()["blocked"])

    def test_failed_atomic_replace_preserves_previous_record_and_partial_write(self):
        self.begin()
        original = (self.root / "demo.json").read_bytes()
        with patch("tool.deployment_state.os.replace", side_effect=OSError("test rename failure")):
            with self.assertRaisesRegex(StateError, "uncertain"):
                self.store.advance("demo", self.operation, "inputs-verified")
        self.assertEqual((self.root / "demo.json").read_bytes(), original)
        self.assertTrue((self.root / "demo.json.new").exists())
        with self.assertRaises(StateError):
            self.store.advance("demo", self.operation, "inputs-verified")
        self.assertTrue((self.root / "reservation").exists())

    def test_interrupted_finalization_blocks_duplicate_dispatch(self):
        self.begin()
        self.ready()
        original_write = self.store._write

        def fail_commit(directory, name, value, **kwargs):
            if name == "demo.json":
                raise OSError("test commit failure")
            return original_write(directory, name, value, **kwargs)

        with patch.object(self.store, "_write", side_effect=fail_commit):
            with self.assertRaisesRegex(StateError, "uncertain"):
                self.store.finalize("demo", self.operation, self.receipt())
        self.assertTrue((self.root / "reservation/finalizing.json").is_file())
        self.assertIsNotNone(self.record()["pending"])
        with self.assertRaisesRegex(StateError, "repeated finalization"):
            self.store.finalize("demo", self.operation, self.receipt())

    def test_lost_release_fsync_reports_uncertain_even_with_absent_reservation(self):
        self.begin()
        self.ready()
        fsync = os.fsync

        def fail_after_release(fd):
            if not (self.root / "reservation").exists():
                raise OSError("test final directory sync failure")
            return fsync(fd)

        with patch("tool.deployment_state.os.fsync", side_effect=fail_after_release):
            with self.assertRaisesRegex(StateError, "reservation may be absent; do not retry"):
                self.store.finalize("demo", self.operation, self.receipt())
        self.assertFalse((self.root / "reservation").exists())
        self.assertIsNotNone(self.record()["committed"])
        self.assertIsNone(self.record()["pending"])
        with self.assertRaises(StateError):
            self.store.finalize("demo", self.operation, self.receipt())

    def child(self, script, *args):
        return subprocess.Popen([sys.executable, "-c", script, str(self.root), *args],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_competing_processes_only_one_acquires_hostwide_reservation(self):
        selection_path = self.parent / "selection.json"
        selection_path.write_text(json.dumps(self.selection))
        script = """
import json, os, sys
from pathlib import Path
from tool.deployment_state import StateStore, StateError
store = StateStore(Path(sys.argv[1]), owner_uid=os.getuid())
try:
    store.begin(sys.argv[2], sys.argv[3], json.loads(Path(sys.argv[4]).read_text()),
                sys.argv[5], ['apply'])
except StateError:
    raise SystemExit(2)
"""
        first = self.child(script, "one", str(uuid4()), str(selection_path), INTENT)
        second = self.child(script, "two", str(uuid4()), str(selection_path), INTENT)
        results = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(stdout + stderr, "")
            results.append(process.returncode)
        self.assertEqual(sorted(results), [0, 2])
        self.assertEqual(len(self.store.status()["deployments"]), 1)

    def test_process_death_after_reservation_creation_leaves_persistent_block(self):
        script = """
import os, sys
from pathlib import Path
root = Path(sys.argv[1])
os.mkdir(root / 'reservation', 0o700)
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
os._exit(73)
"""
        child = self.child(script)
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 73, stdout + stderr)
        with self.assertRaisesRegex(StateError, "existing reservation"):
            self.begin()
        self.assertTrue((self.root / "reservation").is_dir())

    def test_completed_child_exit_does_not_release_pending_reservation(self):
        selection_path = self.parent / "selection.json"
        selection_path.write_text(json.dumps(self.selection))
        script = """
import json, os, sys
from pathlib import Path
from tool.deployment_state import StateStore
store = StateStore(Path(sys.argv[1]), owner_uid=os.getuid())
store.begin('demo', sys.argv[2], json.loads(Path(sys.argv[3]).read_text()), sys.argv[4], ['apply'])
os._exit(0)
"""
        child = self.child(script, self.operation, str(selection_path), INTENT)
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stdout + stderr)
        self.assertTrue(self.store.status()["blocked"])
        self.assertIsNotNone(self.record()["pending"])
        with self.assertRaises(StateError):
            self.begin()

    def test_operator_cli_has_no_mutation_or_unlock_action(self):
        for action in ("apply", "finalize", "unlock", "initialize"):
            result = subprocess.run([sys.executable, "-m", "tool.deployment_state", action],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
