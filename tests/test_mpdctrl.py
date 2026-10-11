import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    with patch("logging.basicConfig"), patch.dict(sys.modules, {"mpd": Mock()}):
        mpdctrl = importlib.import_module("mpdctrl")


class MPDControlTests(unittest.TestCase):
    def setUp(self):
        self.instances = {name: Mock() for name in ("Radio 88.6", "FM4 Radio", "OE3 Radio")}
        self.config = {"streams": [{"name": name} for name in self.instances]}
        self.read = patch.object(mpdctrl.readconfig, "read_and_parse_json", return_value=self.config)
        self.convert = patch.object(
            mpdctrl, "config2mpd", side_effect=lambda entry, *args, **kwargs: self.instances[entry["name"]]
        )
        self.read.start()
        self.convert.start()
        self.addCleanup(self.read.stop)
        self.addCleanup(self.convert.stop)

    def test_individual_start_and_stop_each_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                for name in self.instances:
                    for option, method in (("--start", "play"), ("--stop", "stop")):
                        with self.subTest(name=name, option=option):
                            for instance in self.instances.values():
                                instance.reset_mock()
                            mpdctrl.main([option, "--stream", name, "config.json"])
                            getattr(self.instances[name], method).assert_called_once_with()
                            for other_name, instance in self.instances.items():
                                if other_name != name:
                                    self.assertEqual(instance.mock_calls, [])

    def test_default_start_and_stop_all(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--start", "config.json"])
                mpdctrl.main(["--stop", "config.json"])
        for instance in self.instances.values():
            instance.play.assert_called_once_with()
            instance.stop.assert_called_once_with()

    def test_status_individual_and_all_without_runtime(self):
        with patch.dict(os.environ, {}, clear=True):
            mpdctrl.main(["--status", "--stream", "FM4 Radio", "config.json"])
            self.instances["FM4 Radio"].get_stream_status.assert_called_once_with()
            self.instances["Radio 88.6"].get_stream_status.assert_not_called()
            mpdctrl.main(["--status", "config.json"])
        self.instances["Radio 88.6"].get_stream_status.assert_called_once_with()

    def test_invalid_or_ambiguous_selection_has_no_side_effects(self):
        for entries, name in ((self.config["streams"], "missing"), ([{"name": "same"}] * 2, "same")):
            with self.subTest(name=name):
                self.config["streams"] = entries
                with self.assertRaises(SystemExit) as error:
                    mpdctrl.main(["--start", "--stream", name, "config.json"])
                self.assertEqual(error.exception.code, 2)
        for instance in self.instances.values():
            self.assertEqual(instance.mock_calls, [])

    def test_removed_and_conflicting_options_rejected(self):
        for options in (["--auto"], ["--install"], ["--enable"], ["--disable"], ["--cleanup"], ["--start", "--stop"], []):
            with self.subTest(options=options), self.assertRaises(SystemExit) as error:
                mpdctrl.main([*options, "config.json"])
            self.assertEqual(error.exception.code, 2)

    def test_mutation_contention_and_unlocked_status(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with mpdctrl.setup_lock("mpd_control.lock"):
                    for option in ("--start", "--stop"):
                        with self.assertRaises(SystemExit) as error:
                            mpdctrl.main([option, "config.json"])
                        self.assertEqual(error.exception.code, 1)
                    mpdctrl.main(["--status", "config.json"])
        for instance in self.instances.values():
            instance.play.assert_not_called()
            instance.stop.assert_not_called()

    def test_play_failure_releases_lock_and_preserves_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                self.instances["Radio 88.6"].play.side_effect = OSError("connection failed")
                with self.assertRaisesRegex(OSError, "connection failed"):
                    mpdctrl.main(["--start", "--stream", "Radio 88.6", "config.json"])
                inode = (Path(directory) / "mpd_control.lock").stat().st_ino
                with mpdctrl.setup_lock("mpd_control.lock"):
                    self.assertEqual((Path(directory) / "mpd_control.lock").stat().st_ino, inode)
