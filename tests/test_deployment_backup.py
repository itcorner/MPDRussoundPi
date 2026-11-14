import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool import deployment_backup as backups
from tool.deployment_state import StateError, StateStore
from tool.deployment_images import resolve
from test_deployment_images import FixtureRegistry


ROOT = Path(__file__).resolve().parents[1]


class FileBackupTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(os, "listxattr"):
            metadata = patch.object(backups.os, "listxattr", return_value=[], create=True)
            metadata.start()
            self.addCleanup(metadata.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-backup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / "backups"
        self.storage.mkdir(mode=0o700)
        self.source = self.root / "live-data"
        self.source.mkdir(mode=0o750)
        for directory in ("config", "data", "secrets", "empty"):
            (self.source / directory).mkdir(mode=0o700 if directory == "secrets" else 0o750)
        self.config = self.source / "config/settings.json"
        self.config.write_text(json.dumps({"notes": "preserve unknown fields", "zones": [1, 2]}))
        self.config.chmod(0o640)
        (self.source / "secrets/token").write_bytes(b"synthetic-private-token")
        (self.source / "secrets/token").chmod(0o600)
        (self.source / "data/binary").write_bytes(bytes(range(256)) * 100)
        (self.source / "data/binary").chmod(0o644)
        (self.source / "data/empty").touch(mode=0o600)
        self.operation = str(uuid4())
        self.guard_calls = 0

    def guard(self):
        self.guard_calls += 1

    def capture(self, **kwargs):
        return backups.FileBackup.capture(
            self.storage, self.operation, {"web": self.source}, verify_quiescent=self.guard, **kwargs)

    def rewrite_manifest(self, backup, value):
        path = backup.path / "manifest.json"
        raw = json.dumps(value).encode()
        path.chmod(0o600)
        path.write_bytes(raw)
        path.chmod(0o400)
        backup.manifest_sha256 = hashlib.sha256(raw).hexdigest()

    def test_roundtrip_exact_content_modes_ownership_timestamps_empty_directories(self):
        backup = self.capture()
        before = {}
        for path in [self.source, *self.source.rglob("*")]:
            info = path.stat()
            before[path.relative_to(self.source)] = (
                info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        report = backup.verify()
        self.assertEqual(report["method"], "stopped-writers")
        self.assertFalse(report["live_restore_supported"])
        self.assertNotIn(str(self.source), json.dumps(report))
        self.assertGreater(self.guard_calls, 5)
        destination = self.root / "recovered"
        result = backup.restore_new(destination, verify_quiescent=self.guard)
        self.assertFalse(result["live_cutover"])
        self.assertFalse(result["reservation_cleared"])
        for relative, expected in before.items():
            path = destination / "web" / relative
            info = path.stat()
            actual = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_mtime_ns,
                      path.read_bytes() if path.is_file() else None)
            self.assertEqual(actual, expected)
        self.assertEqual(self.config.read_text(), (destination / "web/config/settings.json").read_text())

    def test_private_backups_are_not_a_copy_of_source_world_readable_modes(self):
        backup = self.capture()
        self.assertEqual(stat.S_IMODE(backup.path.stat().st_mode), 0o700)
        for path in backup.path.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertTrue((backup.path / "manifest.json").is_file())
        with self.assertRaises(backups.BackupError):
            self.capture()

    def test_new_process_can_verify_offhost_copy_with_separately_retained_manifest_identity(self):
        backup = self.capture()
        offhost = self.root / "offhost"
        offhost.mkdir(mode=0o700)
        shutil.copytree(backup.path, offhost / self.operation)
        script = """
import os, sys
from pathlib import Path
from tool.deployment_backup import FileBackup
if not hasattr(os, 'listxattr'):
    os.listxattr = lambda fd: []
backup = FileBackup(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
assert backup.verify()['method'] == 'stopped-writers'
backup.restore_new(Path(sys.argv[4]), verify_quiescent=lambda: None)
"""
        destination = self.root / "offhost-restored"
        result = subprocess.run([sys.executable, "-c", script, str(offhost), self.operation,
                                 backup.manifest_sha256, str(destination)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((destination / "web/secrets/token").read_bytes(),
                         (self.source / "secrets/token").read_bytes())

    def test_existing_restore_destination_never_overwritten(self):
        backup = self.capture()
        target = self.root / "existing"
        target.mkdir(mode=0o700)
        (target / "preserve").write_bytes(b"keep")
        with self.assertRaises(backups.BackupError):
            backup.restore_new(target, verify_quiescent=self.guard)
        self.assertEqual((target / "preserve").read_bytes(), b"keep")
        self.assertEqual(list(target.iterdir()), [target / "preserve"])

    def test_restore_never_overlaps_sources_or_backup_storage(self):
        backup = self.capture()
        for path in (self.source, self.source / "new", self.root, backup.path / "new", self.storage / "new"):
            with self.subTest(path=path), self.assertRaises(backups.BackupError):
                backup.restore_new(path, verify_quiescent=self.guard)
        self.assertEqual((self.source / "secrets/token").read_bytes(), b"synthetic-private-token")

    def test_capture_requires_quiescence_and_guard_failure_retains_partial_backup(self):
        def not_stopped():
            raise backups.BackupError("Writers remain active")

        with self.assertRaisesRegex(backups.BackupError, "Writers remain active"):
            backups.FileBackup.capture(self.storage, self.operation, {"web": self.source},
                                       verify_quiescent=not_stopped)
        self.assertFalse((self.storage / self.operation).exists())
        calls = 0

        def interrupted():
            nonlocal calls
            calls += 1
            if calls == 7:
                raise backups.BackupError("Writer status became uncertain")

        with self.assertRaisesRegex(backups.BackupError, "uncertain"):
            backups.FileBackup.capture(self.storage, self.operation, {"web": self.source},
                                       verify_quiescent=interrupted)
        path = self.storage / self.operation
        self.assertTrue(path.is_dir())
        self.assertFalse((path / "manifest.json").exists())
        self.assertTrue(any(path.glob("*.blob")))

    def test_source_change_during_copy_is_detected(self):
        original = backups.copy_stream

        def mutate(source, target, size):
            result = original(source, target, size)
            if target is not None:
                self.config.write_bytes(b"changed-during-backup")
            return result

        with patch.object(backups, "copy_stream", side_effect=mutate):
            with self.assertRaisesRegex(backups.BackupError, "changed"):
                self.capture()
        self.assertFalse((self.storage / self.operation / "manifest.json").exists())

    def test_change_to_already_copied_sibling_is_detected_before_manifest_completion(self):
        original = backups.copy_stream
        copies = 0

        def mutate(source, target, size):
            nonlocal copies
            result = original(source, target, size)
            if target is not None:
                copies += 1
                if copies == 3:
                    self.config.write_bytes(b"changed-after-its-own-copy-finished")
            return result

        with patch.object(backups, "copy_stream", side_effect=mutate):
            with self.assertRaisesRegex(backups.BackupError, "changed"):
                self.capture()
        self.assertFalse((self.storage / self.operation / "manifest.json").exists())

    def test_manifest_and_blob_tampering_missing_extra_symlink_and_hardlink_fail(self):
        for fault in ("manifest", "content", "truncated", "missing", "extra", "mode", "symlink", "hardlink"):
            with self.subTest(fault=fault):
                self.operation = str(uuid4())
                backup = self.capture()
                path = next(backup.path.glob("*.blob"))
                if fault == "manifest":
                    path = backup.path / "manifest.json"
                    path.chmod(0o600)
                    path.write_bytes(b"{}")
                    path.chmod(0o400)
                elif fault in ("content", "truncated"):
                    raw = path.read_bytes()
                    path.chmod(0o600)
                    path.write_bytes(b"x" * len(raw) if fault == "content" else b"")
                    path.chmod(0o400)
                elif fault == "missing":
                    path.unlink()
                elif fault == "extra":
                    (backup.path / "partial.new").touch()
                elif fault == "mode":
                    path.chmod(0o644)
                elif fault == "symlink":
                    path.unlink()
                    path.symlink_to(self.config)
                else:
                    os.link(path, self.root / ("hardlink-" + self.operation))
                with self.assertRaises(backups.BackupError):
                    backup.verify()
                target = self.root / ("recovery-" + self.operation)
                with self.assertRaises(backups.BackupError):
                    backup.restore_new(target, verify_quiescent=self.guard)
                self.assertFalse(target.exists())

    def test_strict_manifest_paths_types_sizes_and_source_coverage_fail_before_restore(self):
        backup = self.capture()
        original = json.loads((backup.path / "manifest.json").read_bytes())
        examples = []
        for path in ("../escape", "/absolute", "web/../escape", "web//escape", "web/./escape",
                     "web\\escape", "other/escape", "web/config/settings.json/child", "web/" + "deep/" * 64 + "leaf"):
            candidate = copy.deepcopy(original)
            candidate["entries"][-1]["path"] = path
            examples.append(candidate)
        for field, value in (("uid", -1), ("uid", 2**32 - 1), ("gid", 2**32),
                             ("mtime_ns", 2**63), ("mode", 0o777), ("mode", 0o4755), ("type", "symlink")):
            candidate = copy.deepcopy(original)
            candidate["entries"][-1][field] = value
            examples.append(candidate)
        for field, value in (("blob", "../escape"), ("blob", "manifest.json"),
                             ("sha256", "invalid"), ("size", -1), ("size", True)):
            candidate = copy.deepcopy(original)
            entry = next(item for item in candidate["entries"] if item["type"] == "file")
            entry[field] = value
            examples.append(candidate)
        candidate = copy.deepcopy(original)
        candidate["entries"].append(candidate["entries"][-1])
        examples.append(candidate)
        candidate = copy.deepcopy(original)
        candidate["sources"]["missing"] = "/missing"
        examples.append(candidate)
        for value in examples:
            with self.subTest(value=value):
                self.rewrite_manifest(backup, value)
                with self.assertRaises((backups.BackupError, StateError)):
                    backup.restore_new(self.root / "not-created", verify_quiescent=self.guard)
                self.assertFalse((self.root / "not-created").exists())

    def test_malformed_or_excessively_nested_json_is_rejected_without_creating_restore(self):
        backup = self.capture()
        path = backup.path / "manifest.json"
        for raw in (b"{", b"\xff", b"[" * 2000 + b"0" + b"]" * 2000):
            with self.subTest(raw_length=len(raw)):
                path.chmod(0o600)
                path.write_bytes(raw)
                path.chmod(0o400)
                backup.manifest_sha256 = hashlib.sha256(raw).hexdigest()
                with self.assertRaises(backups.BackupError):
                    backup.restore_new(self.root / "not-created", verify_quiescent=self.guard)
                self.assertFalse((self.root / "not-created").exists())

    def test_unsafe_source_links_types_permissions_and_overlapping_roots_fail(self):
        link = self.source / "symlink"
        link.symlink_to(self.config)
        with self.assertRaises(backups.BackupError):
            self.capture()
        link.unlink()
        os.link(self.config, self.source / "hardlink")
        self.operation = str(uuid4())
        with self.assertRaises(backups.BackupError):
            self.capture()
        (self.source / "hardlink").unlink()
        os.mkfifo(self.source / "fifo", 0o600)
        self.operation = str(uuid4())
        with self.assertRaises(backups.BackupError):
            self.capture()
        (self.source / "fifo").unlink()
        self.config.chmod(0o666)
        self.operation = str(uuid4())
        with self.assertRaises(backups.BackupError):
            self.capture()
        for sources in ({"web": self.source, "child": self.source / "data"}, {"web": self.storage}):
            with self.assertRaises(backups.BackupError):
                backups.FileBackup.capture(self.storage, str(uuid4()), sources, verify_quiescent=self.guard)

    def test_unsafe_storage_and_restore_ancestor_symlinks_are_rejected(self):
        link = self.root / "alias"
        link.symlink_to(self.storage, target_is_directory=True)
        with self.assertRaises(backups.BackupError):
            backups.FileBackup.capture(link, self.operation, {"web": self.source}, verify_quiescent=self.guard)
        backup = self.capture()
        link.unlink()
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(backups.BackupError):
            backup.restore_new(link / "restored", verify_quiescent=self.guard)
        self.assertFalse((self.root / "restored").exists())

    def test_file_and_directory_fsync_failures_never_look_like_completed_backup(self):
        for point in (1, 3, 5):
            self.operation = str(uuid4())
            calls = 0
            original = os.fsync

            def failing(fd):
                nonlocal calls
                calls += 1
                if calls == point:
                    raise OSError("test I/O failure")
                return original(fd)

            with self.subTest(point=point), patch.object(backups.os, "fsync", side_effect=failing):
                with self.assertRaises(backups.BackupError):
                    self.capture()
            path = self.storage / self.operation
            self.assertTrue(path.exists())
            self.assertFalse((path / "manifest.json").exists())

    def test_restore_write_or_metadata_failure_retains_partial_target_and_backup(self):
        backup = self.capture()
        for fault in ("write", "metadata"):
            target = self.root / fault
            with self.subTest(fault=fault):
                if fault == "metadata":
                    patcher = patch.object(backups.FileBackup, "_metadata", side_effect=OSError("test metadata"))
                else:
                    def fail_write(fd, value):
                        raise OSError("test disk full")

                    patcher = patch.object(backups.os, "write", side_effect=fail_write)
                with patcher, self.assertRaises(backups.BackupError):
                    backup.restore_new(target, verify_quiescent=self.guard)
                self.assertTrue(target.is_dir())
                self.assertTrue((backup.path / "manifest.json").is_file())
                backup.verify()
                with self.assertRaises(backups.BackupError):
                    backup.restore_new(target, verify_quiescent=self.guard)

    def test_silent_corruption_by_destination_write_is_caught_by_rereading_restored_bytes(self):
        backup = self.capture()
        write = os.write

        def corrupt(fd, content):
            return write(fd, b"x" * len(content))

        with patch.object(backups.os, "write", side_effect=corrupt):
            with self.assertRaisesRegex(backups.BackupError, "independent byte verification"):
                backup.restore_new(self.root / "corrupted", verify_quiescent=self.guard)
        backup.verify()

    def test_short_writes_are_completed_instead_of_truncating_data(self):
        write = os.write

        def short_write(fd, content):
            return write(fd, content[:5])

        with patch.object(backups.os, "write", side_effect=short_write):
            backup = self.capture()
            backup.restore_new(self.root / "short-writes", verify_quiescent=self.guard)
        self.assertEqual((self.root / "short-writes/web/data/binary").read_bytes(),
                         (self.source / "data/binary").read_bytes())

    def test_process_loss_before_manifest_write_retains_unusable_partial_backup(self):
        script = """
import os, sys
from pathlib import Path
from tool import deployment_backup as backups
if not hasattr(os, 'listxattr'):
    os.listxattr = lambda fd: []
write_file = backups.write_file
def interrupted(directory, name, content, **kwargs):
    if name == 'manifest.json':
        os._exit(73)
    return write_file(directory, name, content, **kwargs)
backups.write_file = interrupted
backups.FileBackup.capture(Path(sys.argv[1]), sys.argv[2], {'web':Path(sys.argv[3])},
                          verify_quiescent=lambda:None)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.storage),
                                 self.operation, str(self.source)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 73, result.stderr)
        path = self.storage / self.operation
        self.assertTrue(any(path.glob("*.blob")))
        self.assertFalse((path / "manifest.json").exists())
        with self.assertRaises(backups.BackupError):
            backups.FileBackup(self.storage, self.operation, "0" * 64).verify()
        with self.assertRaises(backups.BackupError):
            self.capture()

    def test_byte_and_entry_limits_preserve_partial_evidence(self):
        for name, value in (("MAX_BYTES", 1), ("MAX_ENTRIES", 1)):
            self.operation = str(uuid4())
            with self.subTest(limit=name), patch.object(backups, name, value):
                with self.assertRaises(backups.BackupError):
                    self.capture()
            self.assertTrue((self.storage / self.operation).is_dir())
            self.assertFalse((self.storage / self.operation / "manifest.json").exists())

    def test_sqlite_unclean_exit_wal_is_preserved_and_recovers_in_isolated_restore(self):
        database = self.source / "data/ledger.sqlite3"
        script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('pragma journal_mode=wal')
db.execute('pragma wal_autocheckpoint=0')
db.execute('create table events (id integer primary key, value text)')
db.execute("insert into events(value) values ('committed-before-backup')")
db.commit()
os._exit(0)
"""
        result = subprocess.run([sys.executable, "-c", script, str(database)], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(str(database) + "-wal").exists())
        backup = self.capture()
        original_wal = Path(str(database) + "-wal").read_bytes()
        destination = self.root / "restored-sqlite"
        backup.restore_new(destination, verify_quiescent=self.guard)
        restored = destination / "web/data/ledger.sqlite3"
        self.assertEqual(Path(str(restored) + "-wal").read_bytes(), original_wal)
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("pragma integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("select value from events").fetchall(), [("committed-before-backup",)])
        self.assertEqual(Path(str(database) + "-wal").read_bytes(), original_wal)

    def test_backup_restore_never_clears_reservation_or_pending_record(self):
        store = StateStore(self.root / "state", owner_uid=os.getuid())
        store.initialize()
        store.begin("demo", self.operation, resolve("external", "linux/arm64", FixtureRegistry()),
                    "sha256:" + "d" * 64, ["apply"])
        previous = (store.root / "demo.json").read_bytes()
        backup = self.capture()
        backup.restore_new(self.root / "recovery", verify_quiescent=self.guard)
        self.assertTrue(store.status()["blocked"])
        self.assertEqual((store.root / "demo.json").read_bytes(), previous)
        self.assertTrue((store.root / "reservation").is_dir())

    def test_single_file_roots_and_zero_byte_files_are_supported(self):
        backup = backups.FileBackup.capture(self.storage, self.operation,
                                            {"compose": self.config, "empty": self.source / "data/empty"},
                                            verify_quiescent=self.guard)
        destination = self.root / "files-only"
        backup.restore_new(destination, verify_quiescent=self.guard)
        self.assertEqual((destination / "compose").read_bytes(), self.config.read_bytes())
        self.assertEqual((destination / "empty").read_bytes(), b"")

    def test_redacted_errors_and_reports_exclude_paths_content_and_hashes(self):
        backup = self.capture()
        report = json.dumps(backup.verify())
        blob = next(backup.path.glob("*.blob"))
        raw = blob.read_bytes()
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt")
        blob.chmod(0o400)
        with self.assertRaises(backups.BackupError) as caught:
            backup.verify()
        diagnostic = report + str(caught.exception)
        for private in (str(self.source), str(self.config), "synthetic-private-token",
                        backup.manifest_sha256, hashlib.sha256(raw).hexdigest()):
            self.assertNotIn(private, diagnostic)

    def test_returned_quiescence_failure_status_is_not_silently_accepted(self):
        with self.assertRaisesRegex(backups.BackupError, "must raise"):
            backups.FileBackup.capture(self.storage, self.operation, {"web": self.source},
                                       verify_quiescent=lambda: False)
        self.assertFalse((self.storage / self.operation).exists())

    def test_unknown_extended_metadata_is_not_silently_lost(self):
        with patch.object(backups.os, "listxattr", return_value=["system.posix_acl_access"], create=True):
            with self.assertRaisesRegex(backups.BackupError, "fidelity-preserving"):
                self.capture()
        self.assertFalse((self.storage / self.operation / "manifest.json").exists())

    def test_added_backup_file_attributes_are_rejected_during_verification(self):
        backup = self.capture()
        for path in (backup.path / "manifest.json", next(backup.path.glob("*.blob"))):
            info = path.stat()

            def added_attribute(fd):
                current = os.fstat(fd)
                return ["user.unexpected"] if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) else []

            with self.subTest(member=path.name), patch.object(
                    backups.os, "listxattr", side_effect=added_attribute, create=True):
                with self.assertRaisesRegex(backups.BackupError, "fidelity-preserving"):
                    backup.verify()

    @unittest.skipUnless(sys.platform == "linux", "real Linux extended attributes")
    def test_real_extended_attributes_fail_instead_of_disappearing_on_restore(self):
        os.setxattr(self.config, "user.backup-fixture", b"private-metadata")
        with self.assertRaisesRegex(backups.BackupError, "fidelity-preserving"):
            self.capture()
        self.assertEqual(os.getxattr(self.config, "user.backup-fixture"), b"private-metadata")


if __name__ == "__main__":
    unittest.main()
