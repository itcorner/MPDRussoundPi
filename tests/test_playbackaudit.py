import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
fake_mpd = types.ModuleType("mpd")
fake_mpd.MPDError = type("MPDError", (Exception,), {})
fake_mpd.MPDClient = Mock()
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    with patch.dict(sys.modules, {"mpd": fake_mpd}):
        audit = importlib.import_module("playbackaudit")
        routes = importlib.import_module("lib.streamroute")


class PlaybackAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "config.json"
        self.config.write_text((SCRIPTS / "config.json").read_text())
        self.output = self.directory / "audit"
        state_home = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.directory)})
        state_home.start()
        self.addCleanup(state_home.stop)
        self.recorder = audit.Recorder(self.config, self.output, 72)
        self.addCleanup(self.recorder.close)
        self.stream = self.recorder.backend.list_outputs()[0]

    def events(self):
        self.recorder.handler.flush()
        return [json.loads(line) for line in (self.output / "observations.jsonl").read_text().splitlines()]

    def state(self, elapsed="10", **extra):
        return {"state": "play", "elapsed": elapsed, "songid": "1", "queue_entries": "1",
                "current_url": self.stream.url, **extra}

    def test_deadline_summary_and_incidents_survive_recorder_restart(self):
        self.recorder.condition("route:front", "wrong route")
        self.recorder.samples["playback"] = 3
        self.recorder.summary("interrupted")
        self.recorder.close()
        resumed = audit.Recorder(self.config, self.output, 999)
        try:
            self.assertEqual(resumed.deadline, self.recorder.deadline)
            self.assertEqual(resumed.counts["route:front"], 1)
            self.assertEqual(resumed.samples["playback"], 3)
            self.assertIn("route:front", resumed.problems)
        finally:
            resumed.close()

    def test_invalid_existing_run_is_not_overwritten(self):
        path = self.output / "run.json"
        path.write_text('{"config_path": "wrong"}')
        with self.assertRaises(ValueError):
            audit.Recorder(self.config, self.output, 72)
        self.assertEqual(path.read_text(), '{"config_path": "wrong"}')

    def test_progress_stall_and_recovery_are_timestamped(self):
        with patch.object(audit, "playback", side_effect=[self.state(), self.state(), self.state("30")]):
            with patch.object(audit.time, "monotonic", side_effect=lambda: self.now):
                for self.now in (10, 20, 30):
                    self.recorder.check_playback(self.stream, "play")
        events = self.events()
        self.assertTrue(any(e["kind"] == "incident" and e["key"].startswith("progress:") for e in events))
        recovery = next(e for e in events if e["kind"] == "recovered")
        self.assertGreaterEqual(recovery["observed_duration_seconds"], 0)

    def test_stop_pause_url_queue_and_output_error_detection(self):
        for intention in ("pause", "stop"):
            with patch.object(audit, "playback", return_value=self.state()):
                self.recorder.check_playback(self.stream, intention)
            self.assertIn(f"state:{self.stream.id}", self.recorder.problems)
        with patch.object(audit, "playback", return_value=self.state(error="output failed", current_url="wrong", queue_entries="2")):
            self.recorder.check_playback(self.stream, "play")
        for kind in ("state", "url", "queue"):
            self.assertIn(f"{kind}:{self.stream.id}", self.recorder.problems)

    def test_disconnect_or_measurement_error_does_not_stop_other_observations(self):
        self.recorder.observe("one", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        result = self.recorder.observe("two", lambda: "healthy")
        self.assertEqual(result, "healthy")
        self.assertIn("measurement:one", self.recorder.problems)
        self.recorder.observe("one", lambda: "back")
        self.assertNotIn("measurement:one", self.recorder.problems)

    def test_route_and_both_channel_gain_checks_are_read_only(self):
        route = routes.StreamRoute(42, "master", "sink.stereo_front")
        with patch.object(audit, "inspect_route", return_value=route):
            with patch.object(audit, "command", return_value="front-left: 65536 / 100%, front-right: 65536 / 100%"):
                self.recorder.check_audio(self.stream, "play")
        self.assertIn(f"route:{self.stream.id}", self.recorder.problems)
        self.assertIn(f"gain:{self.stream.id}", self.recorder.problems)

    def test_inactive_gain_is_recorded_but_not_required_to_match(self):
        with patch.object(audit, "inspect_route") as route:
            with patch.object(audit, "command", return_value="front-left: 65536 / 100%, front-right: 65536 / 100%"):
                self.recorder.check_audio(self.stream, "stop")
        route.assert_not_called()
        self.assertNotIn(f"gain:{self.stream.id}", self.recorder.problems)

    def test_signal_silence_serializes_without_nonfinite_json(self):
        with patch.object(audit, "signal_levels", return_value=[float("-inf"), float("-inf")]):
            self.recorder.check_signal(self.stream, "play")
        event = next(e for e in self.events() if e["kind"] == "signal")
        self.assertEqual(event["rms_db"], ["-inf", "-inf"])
        self.assertIn("programme silence", next(e["detail"] for e in self.events() if e["kind"] == "incident"))

    def test_signal_parser_checks_duration_stereo_and_timeout(self):
        completed = subprocess.CompletedProcess([], 0, stdout="out_time_us=2000000\n",
                                                stderr="RMS level dB: -20\nRMS level dB: -21\nRMS level dB: -20.5\n")
        with patch.object(audit.subprocess, "run", return_value=completed) as run:
            self.assertEqual(audit.signal_levels("stereo_front"), [-20, -21])
            self.assertEqual(run.call_args.kwargs["timeout"], 12)
            completed.stdout = "out_time_us=100\n"
            with self.assertRaisesRegex(RuntimeError, "shorter"):
                audit.signal_levels("stereo_front")
            completed.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "failed"):
                audit.signal_levels("stereo_front")

    def test_command_errors_permissions_and_timeout_are_explicit(self):
        with patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                audit.command(["pactl"])
        with patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "permission denied")):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                audit.command(["journalctl"])
        with patch.object(audit.subprocess, "run", side_effect=subprocess.TimeoutExpired("pactl", 5)):
            self.recorder.observe("command", lambda: audit.command(["pactl"]))
        self.assertIn("measurement:command", self.recorder.problems)

    def test_journal_cursor_and_overflow_are_recorded(self):
        with patch.object(audit, "command", return_value='{"MESSAGE":"USB disconnect"}\n-- cursor: abc\n') as run:
            self.recorder.journals("kernel")
            self.recorder.journals("kernel")
        self.assertIn("--after-cursor", run.call_args.args[0])
        self.assertIn("abc", run.call_args.args[0])
        self.assertTrue(any(e["kind"] == "journal" for e in self.events()))
        with patch.object(audit, "command", return_value='{"MESSAGE":"event"}\n' * 1000):
            self.recorder.journals("user")
        self.assertIn("journal_overflow:user", self.recorder.problems)

    def test_once_checks_all_streams_and_never_calls_recovery(self):
        with patch.object(self.recorder, "intentions", return_value={s.id: "play" for s in self.recorder.backend.list_outputs()}):
            with patch.object(self.recorder, "check_playback", return_value=None) as playback:
                with patch.object(self.recorder, "check_audio", return_value=None) as audio:
                    with patch.object(self.recorder, "check_signal", return_value=None) as signal:
                        with patch.object(self.recorder, "journals", return_value=None), patch.object(self.recorder, "resources", return_value=None):
                            with patch.object(audit, "audio_ready", return_value=None) as ready, patch.object(audit, "command", return_value=""):
                                self.recorder.run(once=True)
        for operation in (playback, audio, signal):
            self.assertEqual(operation.call_count, 3)
        ready.assert_called_once_with(self.config.resolve())
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "snapshot")

    def test_service_is_independent_of_playback_units_and_not_started(self):
        with patch.object(audit.Path, "home", return_value=self.directory):
            with patch.object(audit.subprocess, "run") as run:
                path = audit.install_service(self.config, self.output, 72)
        text = path.read_text()
        self.assertIn("Restart=on-failure", text)
        self.assertIn("--output", text)
        self.assertNotIn("Requires=", text)
        self.assertNotIn("BindsTo=", text)
        run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)

    def test_invalid_duration_and_recorder_lock_prevent_run(self):
        for hours in ("0", "-1", "nan", "inf"):
            with self.assertRaises(SystemExit) as error:
                audit.main([str(self.config), "--output", str(self.output), "--hours", hours])
            self.assertEqual(error.exception.code, 2)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}):
            with audit.setup_lock("playback_audit.lock"):
                with self.assertRaises(SystemExit):
                    audit.main([str(self.config), "--output", str(self.output), "--once"])

    def test_protocol_reads_only_and_disconnects_after_failure(self):
        client = Mock()
        client.status.return_value = {"state": "play", "songid": "1"}
        client.playlistinfo.return_value = [{"id": "1", "file": self.stream.url}]
        with patch.object(audit, "mpd", fake_mpd):
            with patch.object(fake_mpd, "MPDClient", return_value=client):
                self.assertEqual(audit.playback(self.stream)["current_url"], self.stream.url)
                self.assertEqual([call[0] for call in client.method_calls],
                                 ["connect", "status", "playlistinfo", "disconnect"])
                client.status.side_effect = fake_mpd.MPDError("offline")
                with self.assertRaises(fake_mpd.MPDError):
                    audit.playback(self.stream)
                self.assertEqual(client.disconnect.call_count, 2)

    def test_expired_run_does_not_sample_or_extend_deadline(self):
        self.recorder.deadline = audit.time.time() - 1
        with patch.object(audit, "command", return_value=""):
            with patch.object(self.recorder, "check_playback") as check:
                self.recorder.run()
        check.assert_not_called()
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "completed")

    def test_log_rotation_is_bounded_and_write_failure_is_not_silent(self):
        self.recorder.handler.maxBytes = 250
        self.recorder.handler.backupCount = 2
        for index in range(20):
            self.recorder.record("test", index=index, text="x" * 100)
        self.assertEqual(len(list(self.output.glob("observations.jsonl*"))), 3)
        with patch.object(self.recorder.handler, "shouldRollover", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "Cannot persist"):
                self.recorder.record("test")

    def test_sampling_cadences_are_independent_and_sigterm_style_stop_is_respected(self):
        self.now = 1
        def wait(_seconds):
            self.now += 10
            if self.now >= 41:
                self.recorder.stop.set()
        with patch.object(audit.time, "monotonic", side_effect=lambda: self.now):
            with patch.object(self.recorder.stop, "wait", side_effect=wait):
                with patch.object(self.recorder, "intentions", return_value={}):
                    with patch.object(self.recorder, "check_playback", return_value=None) as playback:
                        with patch.object(self.recorder, "check_audio", return_value=None) as audio:
                            with patch.object(self.recorder, "check_signal", return_value=None) as signal:
                                with patch.object(audit, "audio_ready", return_value=None), patch.object(audit, "command", return_value=""):
                                    with patch.object(self.recorder, "journals", return_value=None), patch.object(self.recorder, "resources", return_value=None):
                                        self.recorder.run()
        self.assertEqual(playback.call_count, 12)
        self.assertEqual(audio.call_count, 6)
        self.assertEqual(signal.call_count, 3)
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "interrupted")

    def test_public_output_directory_and_unowned_unit_are_refused(self):
        self.output.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            audit.Recorder(self.config, self.output, 72)
        target = self.directory / ".config/systemd/user" / audit.UNIT
        target.parent.mkdir(parents=True)
        target.write_text("unrelated service")
        with patch.object(audit.Path, "home", return_value=self.directory):
            with self.assertRaisesRegex(ValueError, "unowned"):
                audit.install_service(self.config, self.output, 72)
        self.assertEqual(target.read_text(), "unrelated service")
