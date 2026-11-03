import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import subprocess


SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    with patch.dict(sys.modules, {"mpd": Mock()}):
        supervisor = importlib.import_module("mpdsupervisor")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(supervisor, "MPDBackend")
        self.api = patcher.start().return_value
        self.addCleanup(patcher.stop)
        watchdog_patcher = patch.object(supervisor, "create_watchdog")
        self.create_watchdog = watchdog_patcher.start()
        self.addCleanup(watchdog_patcher.stop)
        self.watchdog = self.create_watchdog.return_value
        self.watchdog.tick.return_value = ()
        self.streams = []
        for name in ("stereo_front", "stereo_centerlfe", "stereo_rear"):
            stream = Mock()
            stream.id = name
            self.streams.append(stream)
        self.api.list_outputs.return_value = self.streams

    def run_once(self):
        with patch.object(sys, "argv", ["mpdsupervisor.py", "--once", "config.json"]):
            with patch.object(supervisor.subprocess, "run") as run:
                supervisor.main()
                return run

    def test_once_starts_configured_services_and_reconciles_independently(self):
        run = self.run_once()
        self.assertEqual(self.api.reconcile.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0],
                         ["systemctl", "--user", "start", "mpd_stereo_front.service"])

    def test_one_stream_failure_does_not_block_other_streams(self):
        self.api.reconcile.side_effect = [supervisor.BackendError("offline"), None, None]
        with self.assertLogs(level="ERROR"), self.assertRaises(SystemExit) as caught:
            self.run_once()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(self.api.reconcile.call_count, 3)

    def test_service_failure_is_not_success_and_other_services_are_attempted(self):
        with patch.object(sys, "argv", ["mpdsupervisor.py", "--once", "config.json"]):
            with patch.object(supervisor.subprocess, "run", side_effect=[
                subprocess.CalledProcessError(1, ["systemctl"], stderr="not ready"), None, None,
            ]):
                with self.assertLogs(level="ERROR"), self.assertRaises(SystemExit):
                    supervisor.main()
        self.assertEqual(self.api.reconcile.call_count, 2)

    def test_initialize_only_does_not_start_services_or_playback(self):
        with patch.object(sys, "argv", ["mpdsupervisor.py", "--initialize", "config.json"]):
            supervisor.main()
        self.api.initialize_intentions.assert_called_once()
        self.api.reconcile.assert_not_called()

    def test_failed_stream_backoff_leaves_healthy_streams_running(self):
        self.api.reconcile.side_effect = [supervisor.BackendError("offline"), None, None, None, None]
        with patch.object(sys, "argv", ["mpdsupervisor.py", "config.json"]):
            with patch.object(supervisor.subprocess, "run"):
                with patch.object(supervisor.time, "monotonic", return_value=0):
                    with patch.object(supervisor.time, "sleep", side_effect=[None, KeyboardInterrupt]):
                        with self.assertLogs(level="ERROR"), self.assertRaises(KeyboardInterrupt):
                            supervisor.main()
        self.assertEqual(self.api.reconcile.call_count, 5)

    def run_continuous(self, loops=2):
        with patch.object(sys, "argv", ["mpdsupervisor.py", "config.json"]):
            with patch.object(supervisor.subprocess, "run"):
                with patch.object(supervisor.time, "monotonic", return_value=0):
                    sleeps = [None] * (loops - 1) + [KeyboardInterrupt]
                    with patch.object(supervisor.time, "sleep", side_effect=sleeps):
                        with self.assertRaises(KeyboardInterrupt):
                            supervisor.main()

    def test_once_mode_does_not_create_or_tick_a_watchdog(self):
        self.run_once()
        self.create_watchdog.assert_not_called()

    def test_continuous_mode_creates_watchdog_from_configuration(self):
        self.run_continuous(loops=1)
        config = self.api.configuration_snapshot.return_value[0].watchdog
        self.create_watchdog.assert_called_once_with(self.api, config)
        self.watchdog.tick.assert_called_once_with(self.streams)

    def test_disabled_watchdog_configuration_skips_the_watchdog(self):
        self.api.configuration_snapshot.return_value[0].watchdog.enabled = False
        self.run_continuous(loops=1)
        self.create_watchdog.assert_not_called()

    def test_watchdog_restart_clears_the_reconcile_backoff(self):
        self.api.reconcile.side_effect = [supervisor.BackendError("offline")] + [None] * 5
        self.watchdog.tick.return_value = ("stereo_front",)
        with self.assertLogs(level="ERROR"):
            self.run_continuous(loops=2)
        # Without the watchdog clearing the backoff, the failed output would be skipped.
        self.assertEqual(self.api.reconcile.call_count, 6)
