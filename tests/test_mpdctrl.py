import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    with patch.dict(sys.modules, {"mpd": Mock()}):
        mpdctrl = importlib.import_module("mpdctrl")


class MPDControlTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(mpdctrl, "MPDBackend")
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.factory.return_value
        self.api.list_streams.return_value = [Mock(name="unused")]

    def test_shared_backend_selected_and_all_actions(self):
        for option in ("start", "stop", "pause", "apply-gain", "recover-route"):
            method = getattr(self.api, option.replace("-", "_"))
            if option == "recover-route":
                method.return_value = []
            for name in (None, "FM4 Radio"):
                with self.subTest(option=option, name=name):
                    method.reset_mock()
                    argv = [f"--{option}", "config.json"]
                    if name:
                        argv[1:1] = ["--stream", name]
                    mpdctrl.main(argv)
                    method.assert_called_once_with(name)

    def test_status_is_read_only(self):
        self.api.status.return_value = [Mock(name="stream", state="play", gain_db=-7.1)]
        mpdctrl.main(["--status", "config.json"])
        self.api.status.assert_called_once_with(None)
        self.api.start.assert_not_called()

    def test_revision_guarded_gain_and_label(self):
        mpdctrl.main(["--set-gain", "-6.1", "--stream", "FM4 Radio",
                      "--expected-revision", "revision", "config.json"])
        self.api.set_gain.assert_called_once_with("FM4 Radio", -6.1, expected_revision="revision")
        mpdctrl.main(["--set-label", "Kitchen", "--stream", "FM4 Radio",
                      "--expected-revision", "revision", "config.json"])
        self.api.set_label.assert_called_once_with("FM4 Radio", "Kitchen", expected_revision="revision")
        for argv in (["--set-gain", "-6"], ["--set-label", "Kitchen", "--stream", "FM4 Radio"],
                     ["--status", "--expected-revision", "revision"]):
            with self.assertRaises(SystemExit):
                mpdctrl.main([*argv, "config.json"])

    def test_errors_have_nonzero_exit(self):
        for error, code in ((ValueError("invalid"), 2), (mpdctrl.BackendError("offline"), 1)):
            self.api.start.side_effect = error
            with self.assertRaises(SystemExit) as caught:
                mpdctrl.main(["--start", "config.json"])
            self.assertEqual(caught.exception.code, code)

    def test_check_continues_after_unavailable_or_wrong_route(self):
        streams = []
        for name in ("one", "two", "three"):
            stream = Mock()
            stream.name = name
            streams.append(stream)
        self.api.list_streams.return_value = streams
        self.api.check_route.side_effect = [
            mpdctrl.BackendError("unavailable"), Mock(correct=False), Mock(correct=True),
        ]
        with self.assertRaises(SystemExit) as caught:
            mpdctrl.main(["--check-route", "config.json"])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.api.check_route.call_count, 3)
        self.api.recover_route.assert_not_called()

    def test_unknown_stream_rejected_and_actions_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            mpdctrl.main(["--check-route", "--stream", "unknown", "config.json"])
        with self.assertRaises(SystemExit):
            mpdctrl.main(["--start", "--stop", "config.json"])
