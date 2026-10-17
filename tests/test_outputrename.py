import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_mpdbackend as fixtures

SCRIPTS_DIR = fixtures.SCRIPTS_DIR

with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    rename = importlib.import_module("outputrename")


class OutputRenameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        os.chmod(self.root, 0o700)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config = json.loads((SCRIPTS_DIR / "config.json").read_text())
        for entry, (name, identity) in zip(config["streams"], (
            ("Radio 88.6", "radio_886"), ("FM4 Radio", "fm4_radio"), ("OE3 Radio", "oe3_radio"),
        )):
            entry.update(name=name, id=identity, label=name)
        config["streams"][1].update(
            queue_managed=True, source_provider="tunein",
            source_station_id="s8007", source_station_name="ORF Hitradio OE3",
        )
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(config))
        self.state = self.root / "playback.json"
        self.state.write_text(json.dumps({"Radio 88.6": "play", "FM4 Radio": "pause", "OE3 Radio": "stop"}))

    def test_renames_streams_ids_labels_and_intentions_to_channels(self):
        renames = rename.rename_outputs(self.config, self.state)
        self.assertEqual(renames["FM4 Radio"], ("Stereo CenterLFE", "stereo_centerlfe", "Stereo CenterLFE"))
        streams = json.loads(self.config.read_text())["streams"]
        self.assertEqual(
            [(entry["name"], entry["id"], entry["label"]) for entry in streams],
            [("Stereo Front", "stereo_front", "Stereo Front"),
             ("Stereo CenterLFE", "stereo_centerlfe", "Stereo CenterLFE"),
             ("Stereo Rear", "stereo_rear", "Stereo Rear")],
        )
        self.assertEqual(streams[1]["source_station_id"], "s8007")
        self.assertEqual(streams[1]["port"], 6602)
        self.assertEqual(
            json.loads(self.state.read_text()),
            {"Stereo Front": "play", "Stereo CenterLFE": "pause", "Stereo Rear": "stop"},
        )

    def test_dry_run_and_rejections_leave_files_unchanged(self):
        config, state = self.config.read_bytes(), self.state.read_bytes()
        self.assertEqual(rename.rename_outputs(self.config, self.state, dry_run=True)["Radio 88.6"][1], "stereo_front")
        self.state.write_text(json.dumps({"Unknown": "play"}))
        with self.assertRaisesRegex(ValueError, "unknown streams"):
            rename.rename_outputs(self.config, self.state)
        self.state.write_bytes(state)
        shared = json.loads(config)
        shared["streams"][2]["audio_output"]["sink"] = "stereo_front"
        self.config.write_text(json.dumps(shared))
        with self.assertRaisesRegex(ValueError, "exclusive"):
            rename.rename_outputs(self.config, self.state)
        self.assertEqual(self.state.read_bytes(), state)

    def test_refuses_pending_operations(self):
        database = self.root / "operations.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE operations (output_id TEXT, status TEXT)")
            db.execute("INSERT INTO operations VALUES ('fm4_radio', 'applied')")
        rename.assert_operations_idle(database)
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO operations VALUES ('fm4_radio', 'queued')")
        with self.assertRaisesRegex(ValueError, "pending"):
            rename.assert_operations_idle(database)


if __name__ == "__main__":
    unittest.main()
