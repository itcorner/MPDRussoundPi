"""Schema versions of the web application's persisted files (mpdrussoundpi.common.dataschema)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mpdrussoundpi.common import dataschema
from test_admin_auth import FAST, PASSWORD
from web.admin_auth import AdminAuth, hash_password
from web.config_store import JsonFileStore
from web.russound_controller import RussoundController

CONFIG = {"backend": {"host": "amp", "port": 9621}, "controllers": [{"id": 1, "zone_count": 6}],
          "zones": [], "inputs": [], "shortcuts": []}
SAVE = {"zone_slots": [{"controller": 1, "zone": 1, "name": "Kitchen", "enabled": True, "visible": True}],
        "source_slots": [{"id": 1, "name": "Radio", "enabled": True}], "shortcuts": []}


class ModuleTests(unittest.TestCase):
    def test_missing_field_is_version_one_and_malformed_versions_are_rejected(self):
        self.assertEqual(dataschema.version_of({"zones": []}), 1)
        self.assertEqual(dataschema.version_of({"schema_version": 1}), 1)
        for bad in (0, -1, True, "1", 1.0, None):
            with self.subTest(bad=bad), self.assertRaises(dataschema.SchemaError):
                dataschema.version_of({"schema_version": bad})

    def test_load_refuses_newer_and_too_old_files_and_migrates_forward(self):
        value, version = dataschema.load("config", {"zones": []})
        self.assertEqual((value, version), ({"zones": []}, 1))
        with self.assertRaises(dataschema.NewerSchemaError) as raised:
            dataschema.load("config", {"schema_version": 2})
        self.assertIn("newer version", str(raised.exception))
        self.assertEqual(raised.exception.version, 2)
        schemas = {**dataschema.SCHEMAS, "config": dataschema.Schema("the settings", current=3, oldest=2)}
        steps = {2: lambda value: {**{key: item for key, item in value.items() if key != "old"},
                                   "renamed": value["old"], "schema_version": 3}}
        with mock.patch.dict(dataschema.SCHEMAS, schemas), mock.patch.dict(dataschema.MIGRATIONS, {"config": steps}):
            self.assertEqual(dataschema.load("config", {"schema_version": 2, "old": 5}),
                             ({"schema_version": 3, "renamed": 5}, 2))
            with self.assertRaisesRegex(dataschema.SchemaError, "no longer reads"):
                dataschema.load("config", {"zones": []})
            self.assertEqual(dataschema.label(), "config=2-3;lockout=1;state=1")

    def test_every_file_has_its_migrations_up_to_the_current_version(self):
        for name, schema in dataschema.SCHEMAS.items():
            self.assertEqual(sorted(dataschema.MIGRATIONS[name]), list(range(schema.oldest, schema.current)), name)

    def test_the_label_round_trips_and_malformed_labels_are_ignored(self):
        self.assertEqual(dataschema.label(), "config=1;lockout=1;state=1")
        self.assertEqual(dataschema.parse_label(dataschema.label()),
                         {"config": (1, 1), "lockout": (1, 1), "state": (1, 1)})
        self.assertEqual(dataschema.parse_label("config=1-3"), {"config": (1, 3)})
        for bad in (None, "", "config", "config=x", "config=3-1", "config=0", "config=1;config=2", "Config=1",
                    "config=1;" + "x" * 200):
            with self.subTest(bad=bad):
                self.assertIsNone(dataschema.parse_label(bad))

    def test_problems_name_what_the_target_cannot_read(self):
        current = {"config": 1, "state": 1, "lockout": 1}
        self.assertEqual(dataschema.problems(dataschema.parse_label(dataschema.label()), current), [])
        self.assertEqual(dataschema.problems(None, current), [], "an image without the label reads version 1")
        self.assertEqual(dataschema.problems(None, {**current, "config": 2}),
                         ["the settings (format 2; that version reads up to 1)"])
        self.assertEqual(dataschema.problems({"config": (2, 3), "state": (1, 1)}, {"config": 1, "state": 1}),
                         ["the settings (format 1; that version reads from 2)"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_versioned_store_reads_old_files_stamps_writes_and_keeps_newer_files_read_only(self):
        path = self.root / "state.json"
        store = JsonFileStore(path, schema="state")
        path.write_text(json.dumps({"zones": []}))
        snapshot = store.current()
        self.assertEqual((snapshot.load_state, snapshot.schema_version, snapshot.read_only), ("valid", 1, None))
        store.writable()
        self.assertEqual(store.prepare({"zones": []}), {"zones": [], "schema_version": 1})
        path.write_text(json.dumps({"schema_version": 2, "zones": []}))
        snapshot = store.current()
        self.assertEqual((snapshot.load_state, snapshot.schema_version), ("valid", 2))
        self.assertIn("newer version", snapshot.read_only)
        self.assertEqual(store.value(), {"schema_version": 2, "zones": []}, "shown as far as it is understood")
        with self.assertRaises(dataschema.NewerSchemaError):
            store.writable()
        path.write_text(json.dumps({"schema_version": "two"}))
        self.assertEqual(store.current().load_state, "invalid")

    def test_an_unversioned_store_is_unchanged(self):
        path = self.root / "other.json"
        path.write_text(json.dumps({"schema_version": 9}))
        store = JsonFileStore(path)
        self.assertEqual((store.current().load_state, store.current().read_only), ("valid", None))
        self.assertEqual(store.prepare({"a": 1}), {"a": 1})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path, self.state_path = self.root / "config.json", self.root / "state.json"

    def test_saves_write_the_current_version_and_the_editor_does_not_show_it(self):
        self.config_path.write_text(json.dumps(CONFIG))
        controller = RussoundController(self.config_path, self.state_path)
        payload = controller.update_config_zones(payload=SAVE)
        self.assertNotIn("schema_version", payload["config"])
        self.assertTrue(payload["config_writable"])
        self.assertIsNone(payload["read_only"])
        self.assertEqual(json.loads(self.config_path.read_text())["schema_version"], 1)
        self.assertEqual(json.loads(self.state_path.read_text())["schema_version"], 1)

    def test_newer_settings_are_shown_but_never_saved(self):
        self.config_path.write_text(json.dumps({**CONFIG, "schema_version": 2, "future": {"x": 1}}))
        before = self.config_path.read_bytes()
        controller = RussoundController(self.config_path, self.state_path)
        editor = controller.build_config_editor_payload()
        self.assertFalse(editor["config_writable"])
        self.assertIn("newer version", editor["read_only"])
        self.assertEqual(editor["config"]["backend"]["host"], "amp")
        with self.assertRaises(dataschema.NewerSchemaError):
            controller.update_config_zones(payload=SAVE)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.state_path.exists(), "nothing was written")

    def test_newer_room_state_keeps_rooms_working_without_saving_it(self):
        self.config_path.write_text(json.dumps(CONFIG))
        newer = json.dumps({"schema_version": 5, "system_power": False, "zones": []})
        self.state_path.write_text(newer)
        controller = RussoundController(self.config_path, self.state_path)
        with self.assertLogs("web.russound_controller", "WARNING") as logged:
            controller.persist_state(controller.load_state(refresh_backend=False))
            controller.persist_state(controller.load_state(refresh_backend=False))
        self.assertEqual(len(logged.records), 1, "warned once")
        self.assertEqual(self.state_path.read_text(), newer)


class LockoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.password = self.root / "admin.scrypt"
        self.password.write_text(hash_password(PASSWORD, **FAST) + "\n")
        self.lockout = self.root / "admin-lockout.json"

    def test_the_lockout_file_is_versioned_and_a_newer_one_is_left_alone(self):
        auth = AdminAuth(self.password, self.lockout)
        auth.login("wrong", "a")
        self.assertEqual(json.loads(self.lockout.read_text())["schema_version"], 1)
        newer = json.dumps({"schema_version": 3, "failures": 1})
        self.lockout.write_text(newer)
        with self.assertLogs("web.admin_auth", "WARNING"):
            again = AdminAuth(self.password, self.lockout)
        again.login("wrong", "a")
        self.assertEqual(self.lockout.read_text(), newer)


if __name__ == "__main__":
    unittest.main()
