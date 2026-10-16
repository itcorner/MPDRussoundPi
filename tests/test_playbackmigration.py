import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_mpdbackend as fixtures

backend, SCRIPTS_DIR = fixtures.backend, fixtures.SCRIPTS_DIR

with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    migration = importlib.import_module("playbackmigrate")
    identity = importlib.import_module("lib.playbackidentity")


class OfflineMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.json"
        self.config.write_bytes((SCRIPTS_DIR / "config.json").read_bytes())
        self.original = self.config.read_bytes()
        self.names = [entry["name"] for entry in json.loads(self.original)["streams"]]
        self.state = self.root / "playback.json"
        self.states = dict(zip(self.names, ("play", "pause", "stop")))
        self.state.write_text(json.dumps(self.states))
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({
            name: {"id": output, "label": output.title()}
            for name, output in zip(self.names, ("front", "centerlfe", "rear"))
        }))
        self.output = self.root / "bundle"

    def prepare(self):
        return migration.prepare_bundle(self.config, self.state, self.mapping, self.output)

    def test_bundle_preserves_all_operational_fields_and_exact_rollback_bytes(self):
        state_bytes = self.state.read_bytes()
        revision = self.prepare()
        prepared = json.loads((self.output / "config.prepared.json").read_bytes())
        preview = json.loads((self.output / "control-v1.preview.json").read_bytes())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.state.read_bytes(), state_bytes)
        self.assertEqual((self.output / "config.original.json").read_bytes(), self.original)
        self.assertEqual((self.output / "playback.original.json").read_bytes(), state_bytes)
        self.assertEqual((self.output / "playback.legacy.json").read_bytes(), state_bytes)
        for entry in prepared["streams"]:
            del entry["id"], entry["label"]
        self.assertEqual(prepared, json.loads(self.original))
        self.assertEqual(preview["outputs"], {
            output: {"mode": "manual", "intention": state}
            for output, state in zip(("front", "centerlfe", "rear"), ("play", "pause", "stop"))
        })
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        self.assertEqual(revision, manifest["prepared_config_revision"])
        for name, digest in manifest["sha256"].items():
            self.assertEqual(migration.hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_destination_refused_without_overwrite(self):
        self.prepare()
        original = (self.output / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual((self.output / "manifest.json").read_bytes(), original)

    def test_missing_corrupt_unknown_or_incomplete_intentions_rejected(self):
        for value in ({}, {**self.states, "unknown": "play"},
                      {**self.states, self.names[0]: "bad"},
                      {"schema_version": 5, "outputs": {}}, []):
            self.state.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.output.exists())
        self.state.unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()

    def test_mapping_rejects_duplicate_partial_and_identity_reassignment(self):
        mapping = json.loads(self.mapping.read_text())
        for bad in ({}, {**mapping, "unknown": {"id": "extra", "label": "Extra"}},
                    {**mapping, self.names[1]: mapping[self.names[0]]}):
            self.mapping.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                self.prepare()
        config = json.loads(self.original)
        for entry in config["streams"]:
            entry.update(mapping[entry["name"]])
        self.config.write_text(json.dumps(config))
        mapping[self.names[0]]["id"] = "replacement"
        self.mapping.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValueError, "Cannot change"):
            self.prepare()

    def test_versioned_preview_rejects_unsupported_versions_and_bad_records(self):
        self.prepare()
        config = json.loads((self.output / "config.prepared.json").read_bytes())
        preview = json.loads((self.output / "control-v1.preview.json").read_bytes())
        migration.validate_control_preview(preview, config)
        for version in (0, 2, True, "1"):
            with self.assertRaises(ValueError):
                migration.validate_control_preview({**preview, "schema_version": version}, config)
        for entry in ({"mode": "automatic", "intention": "play"},
                      {"mode": "manual", "intention": "invalid"},
                      {"mode": "manual", "intention": "play", "extra": 1}):
            bad = copy.deepcopy(preview)
            bad["outputs"]["front"] = entry
            with self.assertRaises(ValueError):
                migration.validate_control_preview(bad, config)
        with self.assertRaises(ValueError):
            migration.validate_control_preview({**preview, "outputs": {}}, config)

    def test_runtime_refuses_preview_instead_of_defaulting_to_play(self):
        self.prepare()
        preview = self.output / "control-v1.preview.json"
        api = backend.MPDBackend(self.config, preview)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            with self.assertRaises(ValueError):
                api.initialize_intentions()
        self.assertEqual(json.loads(preview.read_bytes())["schema_version"], 1)

    def test_partial_bundle_is_cleaned_on_write_failure(self):
        with patch.object(Path, "chmod", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_cli_preparation_is_offline_and_reports_failures(self):
        args = ["--config", str(self.config), "--intentions", str(self.state),
                "--identities", str(self.mapping), "--output", str(self.output)]
        with patch.object(backend, "mpd") as mpd, patch("builtins.print") as output:
            migration.main(args)
            mpd.MPDClient.assert_not_called()
            self.assertIn("No operational files changed", output.call_args.args[0])
        with self.assertRaises(SystemExit) as caught:
            migration.main(args)
        self.assertEqual(caught.exception.code, 1)


class IdentityRevisionTests(unittest.TestCase):
    setUp = fixtures.MPDBackendTests.setUp

    def add_ids(self):
        for entry, output in zip(self.config["streams"], ("front", "centerlfe", "rear")):
            entry.update(id=output, label=output.title())
        self.path.write_text(json.dumps(self.config))

    def test_optional_legacy_and_strict_all_or_none_identity(self):
        self.assertEqual(identity.identities(self.config["streams"]), {})
        self.config["streams"][0].update(id="front", label="Front")
        self.path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            self.api.list_streams()
        self.add_ids()
        self.assertEqual(len(self.api.list_streams()), 3)
        for invalid in ("", "../bad", "UPPER", "x" * 65, None):
            self.config["streams"][0]["id"] = invalid
            self.path.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                self.api.list_streams()

    def test_label_requires_identity_and_preserves_config_on_invalid_input(self):
        revision = self.api.configuration_snapshot()[2]
        with self.assertRaises(ValueError):
            self.api.set_label("Radio 88.6", "Display", expected_revision=revision)
        self.add_ids()
        original = self.path.read_bytes()
        revision = self.api.configuration_snapshot()[2]
        for label in ("", " ", "bad\nlabel"):
            with self.assertRaises(ValueError):
                self.api.set_label("Radio 88.6", label, expected_revision=revision)
        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_gain_rejected_before_any_audio_or_config_change(self):
        revision = self.api.configuration_snapshot()[2]
        self.api.set_gain("Radio 88.6", -5, expected_revision=revision)
        self.recover_route.reset_mock()
        self.set_sink_gain.reset_mock()
        original = self.path.read_bytes()
        self.client.reset_mock()
        with self.assertRaises(backend.RevisionConflict):
            self.api.set_gain("Radio 88.6", -4, expected_revision=revision)
        self.assertEqual(self.path.read_bytes(), original)
        self.recover_route.assert_not_called()
        self.set_sink_gain.assert_not_called()
        self.client.connect.assert_not_called()

    def test_legacy_gain_write_invalidates_revision_and_label_changes_only_label(self):
        self.add_ids()
        revision = self.api.configuration_snapshot()[2]
        self.api.set_gain("Radio 88.6", -5)
        with self.assertRaises(backend.RevisionConflict):
            self.api.set_label("Radio 88.6", "New label", expected_revision=revision)
        config, _, revision = self.api.configuration_snapshot()
        self.api.set_label("Radio 88.6", "New label", expected_revision=revision)
        updated = json.loads(self.path.read_text())
        self.assertEqual(updated["streams"][0].pop("label"), "New label")
        config["streams"][0].pop("label")
        self.assertEqual(updated, config)

    def test_concurrent_gain_edit_cannot_both_accept_same_revision(self):
        revision = self.api.configuration_snapshot()[2]
        entered, release = threading.Event(), threading.Event()
        failures = []

        def gain(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")

        def first():
            try:
                self.api.set_gain("Radio 88.6", -5, expected_revision=revision)
            except Exception as error:
                failures.append(error)

        self.set_sink_gain.side_effect = gain
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(backend.BackendError, "lock is busy"):
                self.api.set_gain("Radio 88.6", -4, expected_revision=revision)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.api.get_gain("Radio 88.6"), -5)
