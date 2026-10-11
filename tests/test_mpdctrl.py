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
        self.config = {
            "streams": [
                {"name": name, "gain_db": -index * 3.0,
                 "audio_output": {"type": "pulse", "sink": f"stereo_{index}"}}
                for index, name in enumerate(self.instances)
            ],
            "outputmappers": {"audio_devices": [{"sinks": [
                {"name": f"stereo_{index}"} for index in range(3)
            ]}]},
        }
        self.read = patch.object(mpdctrl.readconfig, "read_and_parse_json", return_value=self.config)
        self.convert = patch.object(
            mpdctrl, "config2mpd", side_effect=lambda entry, *args, **kwargs: self.instances[entry["name"]]
        )
        self.read.start()
        self.convert.start()
        gain_patch = patch.object(mpdctrl, "set_sink_gain")
        self.gain = gain_patch.start()
        self.addCleanup(gain_patch.stop)
        for method in ("inspect_route", "recover_route"):
            route_patch = patch.object(mpdctrl, method)
            setattr(self, method, route_patch.start())
            self.addCleanup(route_patch.stop)
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
                            self.gain.reset_mock()
                            mpdctrl.main([option, "--stream", name, "config.json"])
                            getattr(self.instances[name], method).assert_called_once_with()
                            for other_name, instance in self.instances.items():
                                if other_name != name:
                                    self.assertEqual(instance.mock_calls, [])
                            if option == "--start":
                                index = list(self.instances).index(name)
                                self.gain.assert_called_once_with(f"stereo_{index}", -index * 3.0)
                            else:
                                self.gain.assert_not_called()

    def test_default_start_and_stop_all(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--start", "config.json"])
                mpdctrl.main(["--stop", "config.json"])
        for instance in self.instances.values():
            instance.play.assert_called_once_with()
            instance.stop.assert_called_once_with()
        self.assertEqual(self.gain.call_count, 3)

    def test_apply_gain_does_not_change_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--apply-gain", "--stream", "FM4 Radio", "config.json"])
        self.gain.assert_called_once_with("stereo_1", -3.0)
        self.recover_route.assert_called_once_with("stereo_1")
        for instance in self.instances.values():
            self.assertEqual(instance.mock_calls, [])

    def test_invalid_gain_or_shared_sink_rejected_before_operations(self):
        for value in (True, "0", 1, float("nan"), float("-inf")):
            with self.subTest(value=value):
                self.config["streams"][0]["gain_db"] = value
                with self.assertRaises(SystemExit) as error:
                    mpdctrl.main(["--start", "config.json"])
                self.assertEqual(error.exception.code, 2)
        self.config["streams"][0]["gain_db"] = 0
        self.config["streams"][1]["audio_output"]["sink"] = "stereo_0"
        with self.assertRaises(SystemExit):
            mpdctrl.main(["--start", "--stream", "Radio 88.6", "config.json"])
        self.gain.assert_not_called()

    def test_gain_failure_stops_affected_stream_and_releases_lock(self):
        self.gain.side_effect = RuntimeError("pactl failed")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with self.assertRaises(SystemExit) as error:
                    mpdctrl.main(["--start", "config.json"])
                self.assertEqual(error.exception.code, 1)
                with mpdctrl.setup_lock("mpd_control.lock"):
                    pass
        self.instances["Radio 88.6"].play.assert_called_once_with()
        self.instances["Radio 88.6"].stop.assert_called_once_with()
        for name in ("FM4 Radio", "OE3 Radio"):
            self.instances[name].play.assert_not_called()

    def test_missing_gain_defaults_to_unity(self):
        del self.config["streams"][0]["gain_db"]
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--apply-gain", "--stream", "Radio 88.6", "config.json"])
        self.gain.assert_called_once_with("stereo_0", 0.0)

    def test_route_check_selected_and_all_without_lock_or_mutation(self):
        with patch.dict(os.environ, {}, clear=True):
            mpdctrl.main(["--check-route", "--stream", "FM4 Radio", "config.json"])
            self.inspect_route.assert_called_once_with("stereo_1")
            self.inspect_route.reset_mock()
            mpdctrl.main(["--check-route", "config.json"])
        self.assertEqual(self.inspect_route.call_count, 3)
        self.recover_route.assert_not_called()
        self.gain.assert_not_called()
        for instance in self.instances.values():
            self.assertEqual(instance.mock_calls, [])

    def test_route_check_reports_failure_and_continues_other_streams(self):
        self.inspect_route.side_effect = [
            RuntimeError("missing playback"),
            Mock(correct=False, client_index=253, actual_sink="master", expected_sink="sink.stereo_1"),
            Mock(correct=True),
        ]
        with self.assertRaises(SystemExit) as error:
            mpdctrl.main(["--check-route", "config.json"])
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.inspect_route.call_count, 3)

    def test_recovery_selected_and_all_preserves_mpd_and_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--recover-route", "--stream", "Radio 88.6", "config.json"])
                self.recover_route.assert_called_once_with("stereo_0")
                self.recover_route.reset_mock()
                mpdctrl.main(["--recover-route", "config.json"])
        self.assertEqual(self.recover_route.call_count, 3)
        self.gain.assert_not_called()
        for instance in self.instances.values():
            self.assertEqual(instance.mock_calls, [])

    def test_start_recovers_route_before_gain(self):
        calls = Mock()
        calls.attach_mock(self.instances["Radio 88.6"].play, "play")
        calls.attach_mock(self.recover_route, "recover")
        calls.attach_mock(self.gain, "gain")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                mpdctrl.main(["--start", "--stream", "Radio 88.6", "config.json"])
        self.assertEqual([item[0] for item in calls.mock_calls], ["play", "recover", "gain"])

    def test_recovery_failure_releases_lock_and_blocks_gain(self):
        self.recover_route.side_effect = RuntimeError("ambiguous client")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                for action in ("--recover-route", "--apply-gain", "--start"):
                    with self.subTest(action=action), self.assertRaises(SystemExit) as error:
                        mpdctrl.main([action, "--stream", "Radio 88.6", "config.json"])
                    self.assertEqual(error.exception.code, 1)
                    with mpdctrl.setup_lock("mpd_control.lock"):
                        pass
        self.gain.assert_not_called()
        self.instances["Radio 88.6"].stop.assert_called_once_with()

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
                    for option in ("--start", "--stop", "--recover-route", "--apply-gain"):
                        with self.assertRaises(SystemExit) as error:
                            mpdctrl.main([option, "config.json"])
                        self.assertEqual(error.exception.code, 1)
                    mpdctrl.main(["--status", "config.json"])
                    mpdctrl.main(["--check-route", "config.json"])
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
