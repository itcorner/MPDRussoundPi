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
from types import SimpleNamespace

from tool.deployment_inputs import InputError, InputSnapshot, runtime_traversal


ROOT = Path(__file__).resolve().parents[1]


class ProtectedInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-inputs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.staging = self.root / "staging"
        self.staging.mkdir(mode=0o700)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.source = self.root / "source-private"
        self.source.write_bytes(b"synthetic-private-input\x00with-binary-bytes")
        self.source.chmod(0o600)
        self.operation = str(uuid4())

    def snapshot(self):
        return InputSnapshot.create(self.staging, self.operation, {"credentials": self.source})

    def install(self, snapshot, target=None):
        snapshot.install_new("credentials", target or self.runtime / "installed",
                             owner_uid=os.getuid(), group_gid=os.getgid())

    def test_exact_bytes_private_files_and_runtime_survives_staging_cleanup(self):
        snapshot = self.snapshot()
        approved = self.source.read_bytes()
        snapshot.verify_sources()
        self.assertEqual(stat.S_IMODE(snapshot.path.stat().st_mode), 0o700)
        for path in snapshot.path.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.install(snapshot)
        installed = self.runtime / "installed"
        self.assertEqual(installed.read_bytes(), approved)
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o400)
        snapshot.cleanup(operation_resolved=True)
        self.assertFalse(snapshot.path.exists())
        self.assertEqual(self.source.read_bytes(), approved)
        result = subprocess.run([sys.executable, "-c",
                                 "import pathlib,sys; assert pathlib.Path(sys.argv[1]).read_bytes() == "
                                 "b'synthetic-private-input\\x00with-binary-bytes'", str(installed)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot.verify_installed("credentials", installed, owner_uid=os.getuid(), group_gid=os.getgid())

    def test_changed_source_bytes_identity_permissions_and_missing_source_block(self):
        for change in ("content", "mode", "replace", "missing", "symlink"):
            with self.subTest(change=change):
                snapshot = InputSnapshot.create(self.staging, str(uuid4()), {"credentials": self.source})
                original = self.source.read_bytes()
                if change == "content":
                    self.source.write_bytes(b"changed")
                elif change == "mode":
                    self.source.chmod(0o400)
                elif change == "replace":
                    replacement = self.root / "replacement"
                    replacement.write_bytes(original)
                    replacement.chmod(0o600)
                    replacement.replace(self.source)
                else:
                    self.source.unlink()
                    if change == "symlink":
                        other = self.root / "other"
                        other.write_bytes(original)
                        other.chmod(0o600)
                        self.source.symlink_to(other)
                with self.assertRaises(InputError):
                    snapshot.verify_sources()
                with self.assertRaises(InputError):
                    self.install(snapshot)
                self.assertFalse((self.runtime / "installed").exists())
                if self.source.is_symlink() or self.source.exists():
                    self.source.unlink()
                self.source.write_bytes(original)
                self.source.chmod(0o600)

    def test_snapshot_content_manifest_permissions_and_unexpected_files_block(self):
        for change in ("content", "manifest", "mode", "extra"):
            with self.subTest(change=change):
                snapshot = InputSnapshot.create(self.staging, str(uuid4()), {"credentials": self.source})
                path = snapshot.path / ("manifest.json" if change == "manifest" else "credentials.input")
                if change == "extra":
                    (snapshot.path / "unexpected").write_text("retain evidence")
                else:
                    path.chmod(0o600)
                    if change != "mode":
                        path.write_bytes(b"changed")
                        path.chmod(0o400)
                with self.assertRaises(InputError):
                    snapshot.verify_sources()
                with self.assertRaises(InputError):
                    snapshot.cleanup(operation_resolved=True)
                self.assertTrue(snapshot.path.exists())

    def test_existing_destination_and_staging_destination_never_overwritten(self):
        snapshot = self.snapshot()
        destination = self.runtime / "installed"
        destination.write_bytes(b"preserve-existing")
        destination.chmod(0o400)
        with self.assertRaises(InputError):
            self.install(snapshot)
        self.assertEqual(destination.read_bytes(), b"preserve-existing")
        with self.assertRaises(InputError):
            self.install(snapshot, snapshot.path / "runtime")
        with self.assertRaises(InputError):
            self.install(snapshot, self.staging / "runtime")
        with self.assertRaises(InputError):
            self.install(snapshot, self.root / "missing-parent" / "runtime")
        self.assertTrue(snapshot.path.exists())

    def test_unresolved_operation_does_not_clean_staging(self):
        snapshot = self.snapshot()
        before = {path.name: path.read_bytes() for path in snapshot.path.iterdir()}
        with self.assertRaises(InputError):
            snapshot.cleanup(operation_resolved=False)
        self.assertEqual({path.name: path.read_bytes() for path in snapshot.path.iterdir()}, before)

    def test_cleanup_removes_only_the_resolved_operations_known_files(self):
        snapshot = self.snapshot()
        other = InputSnapshot.create(self.staging, str(uuid4()), {"credentials": self.source})
        before = {path.name: path.read_bytes() for path in other.path.iterdir()}
        snapshot.cleanup(operation_resolved=True)
        self.assertFalse(snapshot.path.exists())
        self.assertEqual({path.name: path.read_bytes() for path in other.path.iterdir()}, before)
        other.verify_sources()

    def test_duplicate_operation_and_symlink_paths_block(self):
        snapshot = self.snapshot()
        with self.assertRaises(InputError):
            self.snapshot()
        alias = self.root / "alias"
        alias.symlink_to(self.staging, target_is_directory=True)
        with self.assertRaises(InputError):
            InputSnapshot.create(alias, str(uuid4()), {"credentials": self.source})
        alias.unlink()
        alias.symlink_to(self.source)
        with self.assertRaises(InputError):
            InputSnapshot.create(self.staging, str(uuid4()), {"credentials": alias})
        self.assertTrue(snapshot.path.exists())

    def test_unsafe_input_types_permissions_and_hardlinks_block(self):
        self.source.chmod(0o644)
        with self.assertRaises(InputError):
            self.snapshot()
        self.source.chmod(0o600)
        os.link(self.source, self.root / "hardlink")
        with self.assertRaises(InputError):
            InputSnapshot.create(self.staging, str(uuid4()), {"credentials": self.source})
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(InputError):
            InputSnapshot.create(self.staging, str(uuid4()), {"credentials": fifo})

    def test_partial_snapshot_and_installation_failure_retain_evidence(self):
        with patch("tool.deployment_inputs.os.fsync", side_effect=OSError("test I/O failure")):
            with self.assertRaises(InputError):
                self.snapshot()
        self.assertTrue((self.staging / self.operation).exists())
        snapshot = InputSnapshot.create(self.staging, str(uuid4()), {"credentials": self.source})
        with patch("tool.deployment_inputs.os.fsync", side_effect=OSError("test I/O failure")):
            with self.assertRaises(InputError):
                self.install(snapshot)
        self.assertTrue(snapshot.path.exists())
        self.assertTrue((self.runtime / "installed").exists())

    def test_source_change_after_snapshot_before_install_blocks_even_with_valid_staging(self):
        snapshot = self.snapshot()
        snapshot.verify_sources()
        self.source.write_bytes(b"changed-between-backup-and-apply")
        with self.assertRaises(InputError):
            self.install(snapshot)
        self.assertFalse((self.runtime / "installed").exists())
        self.assertEqual((snapshot.path / "credentials.input").read_bytes(),
                         b"synthetic-private-input\x00with-binary-bytes")

    def test_installed_copy_changed_content_group_or_mode_fails_verification(self):
        snapshot = self.snapshot()
        self.install(snapshot)
        installed = self.runtime / "installed"
        with self.assertRaises(InputError):
            snapshot.verify_installed("credentials", installed,
                                      owner_uid=os.getuid(), group_gid=os.getgid() + 1)
        installed.chmod(0o600)
        with self.assertRaises(InputError):
            snapshot.verify_installed("credentials", installed,
                                      owner_uid=os.getuid(), group_gid=os.getgid())
        installed.write_bytes(b"tampered")
        installed.chmod(0o400)
        with self.assertRaises(InputError):
            snapshot.verify_installed("credentials", installed,
                                      owner_uid=os.getuid(), group_gid=os.getgid())

    def test_diagnostics_do_not_reveal_contents_source_paths_or_fingerprints(self):
        snapshot = self.snapshot()
        manifest = json.loads((snapshot.path / "manifest.json").read_bytes())
        self.source.write_bytes(b"changed")
        with self.assertRaises(InputError) as caught:
            self.install(snapshot)
        diagnostic = str(caught.exception)
        for private in (str(self.source), self.source.name, "synthetic-private-input",
                        manifest["inputs"]["credentials"]["fingerprint"]):
            self.assertNotIn(private, diagnostic)

    def test_root_readability_does_not_substitute_for_service_ancestor_traversal(self):
        private_ancestor = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o700)
        with patch.object(Path, "stat", return_value=private_ancestor):
            with self.assertRaisesRegex(InputError, "ancestor traversal"):
                runtime_traversal(Path("/private/runtime/credentials"), owner=10001, group=10001)
            runtime_traversal(Path("/private/runtime/credentials"), owner=0, group=0)


if __name__ == "__main__":
    unittest.main()
