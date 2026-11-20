import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from web.config_store import RACY_WINDOW_NS, ConfigStore, JsonFileStore
from web.config_types import validate_russound_config

VALID = {"controllers": [{"id": 1, "zone_count": 6}]}


def age(path: Path, seconds: float = 10.0) -> None:
    """Move the file's timestamps out of the racy window."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class JsonFileStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.store = ConfigStore(self.path, validate_russound_config, b"k" * 32)

    def test_load_states_and_generations_follow_content(self):
        missing = self.store.current()
        self.assertEqual((missing.load_state, missing.value), ("missing", None))
        self.path.write_text(json.dumps(VALID))
        valid = self.store.current()
        self.assertEqual((valid.load_state, valid.value), ("valid", VALID))
        self.path.write_text("{broken")
        invalid = self.store.current()
        self.assertEqual(invalid.load_state, "invalid")
        self.assertIn("Cannot load configuration", invalid.error)
        self.path.write_bytes(b"\xff\xfe")
        self.assertEqual(self.store.current().load_state, "unreadable")
        self.path.unlink()
        self.assertEqual(self.store.current().load_state, "missing")
        generations = [missing.generation, valid.generation, invalid.generation]
        self.assertEqual(generations, sorted(set(generations)))

    def test_settled_file_is_not_reread(self):
        self.path.write_text(json.dumps(VALID))
        age(self.path)
        first = self.store.current()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unchanged file reread")):
            for _ in range(3):
                self.assertIs(self.store.current(), first)

    def test_same_size_rewrite_inside_racy_window_is_detected(self):
        self.path.write_text('{"controllers": [{"id": 1, "zone_count": 6}]}')
        before = self.store.current()
        stat = self.path.stat()
        # Same inode, same size and (forced) same timestamp: only the bytes differ.
        self.path.write_text('{"controllers": [{"id": 2, "zone_count": 6}]}')
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, stat.st_size)
        after = self.store.current()
        self.assertEqual(after.value["controllers"][0]["id"], 2)
        self.assertGreater(after.generation, before.generation)

    def test_touch_keeps_value_generation_and_revision(self):
        self.path.write_text(json.dumps(VALID))
        before, revision = self.store.current(), self.store.revision()
        age(self.path, 30)
        after = self.store.current()
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(self.store.revision(), revision)

    def test_value_is_a_private_copy(self):
        self.path.write_text(json.dumps(VALID))
        value = self.store.config()
        value["controllers"].append({"id": 2, "zone_count": 6})
        self.assertEqual(self.store.config(), VALID)

    def test_revision_tracks_bytes_absence_and_key(self):
        absent = self.store.revision()
        self.path.write_text(json.dumps(VALID))
        present = self.store.revision()
        self.assertNotEqual(absent, present)
        self.path.write_text(json.dumps(VALID) + "\n")
        self.assertNotEqual(self.store.revision(), present)
        other = ConfigStore(self.path, validate_russound_config, b"x" * 32)
        self.assertNotEqual(other.revision(), self.store.revision())

    def test_record_write_refreshes_after_own_write(self):
        self.path.write_text(json.dumps(VALID))
        age(self.path)
        self.store.current()
        self.path.write_text(json.dumps({"controllers": [{"id": 3, "zone_count": 6}]}))
        age(self.path)  # even with a settled timestamp, the writer's notice rereads
        self.assertEqual(self.store.record_write().value["controllers"][0]["id"], 3)

    def test_plain_store_has_no_validation_and_its_own_error_prefix(self):
        path = self.path.with_name("state.json")
        store = JsonFileStore(path, error_prefix="Cannot load state")
        path.write_text("[1, 2]")
        self.assertEqual(store.value(), [1, 2])
        path.write_text("{")
        self.assertTrue(store.current().error.startswith("Cannot load state"))

    def test_racy_window_is_a_few_seconds(self):
        self.assertTrue(1_000_000_000 <= RACY_WINDOW_NS <= 5_000_000_000)


if __name__ == "__main__":
    unittest.main()
