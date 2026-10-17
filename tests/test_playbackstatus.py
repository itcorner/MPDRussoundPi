from concurrent.futures import wait
from dataclasses import FrozenInstanceError
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
fake_mpd = types.ModuleType("mpd")
fake_mpd.MPDError = type("MPDError", (Exception,), {})
fake_mpd.MPDClient = Mock()
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    with patch.dict(sys.modules, {"mpd": fake_mpd}):
        status = importlib.import_module("lib.playbackstatus")
        backend = importlib.import_module("lib.mpdbackend")
        ready = importlib.import_module("lib.audioready")
        gain = importlib.import_module("lib.streamgain")


class PlaybackStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.config = json.loads((SCRIPTS / "config.json").read_text())
        for entry, identity in zip(self.config["streams"], ("front", "centerlfe", "rear")):
            entry.update(id=identity, label=identity.title() + " stream")
        self.save()
        self.api = backend.MPDBackend(self.path)
        self.clients = []
        self.factory = self.patch(backend, "mpd", fake_mpd)
        self.patch(fake_mpd, "MPDClient", side_effect=self.client)
        self.readiness = self.patch(status, "audio_ready_config")
        self.routes = self.patch(status, "inspect_route", side_effect=lambda sink:
                                 status.Route(f"sink.{sink}", f"sink.{sink}", True))
        self.gains = self.patch(status, "read_sink_gain", return_value=(49905, 49905))
        self.collector = status.PlaybackCollector(self.api)
        self.addCleanup(self.collector.close)
        self.fixtures = json.loads((Path(__file__).parent / "fixtures/playback-read-v1.json").read_text())

    def patch(self, module, name, *args, **kwargs):
        patcher = patch.object(module, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save(self):
        self.path.write_text(json.dumps(self.config))

    def state(self, **extra):
        return {"state": "play", "songid": "1", "playlistlength": "1", "playlist": "9",
                "elapsed": "1234.5", "bitrate": "128", "audio": "44100:24:2", **extra}

    def client(self):
        client = Mock()
        client.status.return_value = self.state()
        client.currentsong.return_value = {"id": "1", "title": "Example track", "name": "Stereo Front"}
        self.clients.append(client)
        return client

    def refresh(self, audio=False):
        jobs = self.collector.refresh(audio=audio)
        self.assertFalse(wait(jobs, timeout=5).not_done)
        for job in jobs:
            job.result()

    def output(self, identity="front"):
        return next(item for item in self.collector.snapshot() if item.id == identity)

    def test_cold_sorted_inventory_and_immutable_snapshots(self):
        self.assertEqual([item.id for item in self.collector.snapshot()], ["centerlfe", "front", "rear"])
        snapshot = self.output()
        self.assertEqual(snapshot.playback.to_dict(0, 10), self.fixtures["pending_probe"])
        with self.assertRaises(FrozenInstanceError):
            snapshot.label = "new"
        self.refresh()
        self.assertEqual(snapshot.playback.status, "pending")
        self.assertEqual(self.output().playback.status, "ok")

    def test_healthy_playback_matches_contract_fixture(self):
        self.refresh()
        value = self.output().playback.current.value.to_dict()
        expected = self.fixtures["healthy_inventory"]["outputs"][0]["playback"]["value"]
        self.assertEqual(value, expected)
        for client in self.clients:
            self.assertEqual(client.timeout, 3)
            self.assertEqual(client.connect.call_args.args[0], "localhost")
            self.assertEqual([call[0] for call in client.mock_calls],
                             ["connect", "status", "currentsong", "status", "disconnect"])

    def test_audio_probes_are_read_only_and_separate(self):
        original = self.path.read_bytes()
        self.refresh()
        self.refresh(audio=True)
        output = self.output()
        self.assertEqual(output.readiness.status, "ok")
        self.assertEqual(output.route.current.value.to_dict(),
                         self.fixtures["healthy_inventory"]["outputs"][0]["audio"]["route"]["value"])
        self.assertEqual(output.gain.current.value.measured_units, (49905, 49905))
        self.assertEqual(self.path.read_bytes(), original)
        for call in self.readiness.call_args_list:
            self.assertEqual(call.kwargs, {"enforce_profile": False})
        self.assertFalse((self.path.parent / "playback.json").exists())
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["config.json"])

    def test_selector_readiness_reaches_shared_validator_without_profile_mutation(self):
        device = self.config["outputmappers"]["audio_devices"][0]
        device["hardware-selector"] = {"vendor": "0d8c", "product": "0102"}
        self.save()
        self.collector.close()
        self.collector = status.PlaybackCollector(self.api)
        self.addCleanup(self.collector.close)
        before = self.path.read_bytes()
        properties = {
            "device.api": "alsa", "device.bus": "usb",
            "device.vendor.id": "0x0d8c", "device.product.id": "0x0102",
            "object.id": "10",
        }
        card = {"index": 1234, "name": "alsa_card.example.3",
                "active_profile": "output:analog-surround-51", "properties": properties}
        graph = [{"id": 10, "type": "PipeWire:Interface:Device", "info": {"props": properties}}]
        self.readiness.side_effect = ready.audio_ready_config
        def pactl(*args):
            if args == ("--format=json", "list", "cards"):
                return json.dumps([card])
            if args == ("--format=json", "list", "sinks"):
                return "[]"
            self.fail("Read-only collector attempted a profile mutation")
        with patch.object(ready, "_pactl", side_effect=pactl):
            with patch.object(ready, "pipewire_graph", return_value=graph):
                self.refresh(audio=True)
        for output in self.collector.snapshot():
            self.assertEqual(output.readiness.status, "error")
            self.assertEqual(output.readiness.error.code, "hardware_output_unavailable")
            self.assertNotIn("example", output.readiness.error.message)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((self.path.parent / "playback.json").exists())

    def test_selected_and_audio_only_refresh_preserve_read_isolation(self):
        jobs = self.collector.refresh(output_ids=("front",))
        for job in jobs:
            job.result(timeout=5)
        self.assertEqual(self.output().playback.status, "ok")
        self.assertEqual(self.output("rear").playback.status, "pending")
        self.assertEqual(len(self.clients), 1)
        jobs = self.collector.refresh(audio=True, playback=False)
        for job in jobs:
            job.result(timeout=5)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.output("rear").gain.status, "ok")
        with self.assertRaisesRegex(ValueError, "Unknown observation output ID"):
            self.collector.refresh(output_ids=("missing",))

    def test_missing_metadata_is_not_configured_station_fallback(self):
        client = self.client()
        client.currentsong.return_value = {
            "id": "1", "file": "https://user:private-pass@radio.example/live?token=private",
        }
        value = status.read_playback(client)
        self.assertEqual(value.now_playing.to_dict(),
                         {"title": None, "artist": None, "album": None, "station": None,
                          "metadata_available": False})
        self.assertEqual(value.current_source, "https://radio.example/...")
        self.assertNotIn("private", json.dumps(value.to_dict()))

    def test_repeated_tags_are_normalized_and_bounded(self):
        client = self.client()
        client.currentsong.return_value = {"id": "1", "artist": ["One", "Two"], "title": "x" * 600}
        value = status.read_playback(client)
        self.assertEqual(value.now_playing.artist, "One; Two")
        self.assertEqual(len(value.now_playing.title), 512)

    def test_metadata_changes_without_song_change(self):
        client = self.client()
        first = status.read_playback(client)
        client.currentsong.return_value = {"id": "1", "title": "Next programme"}
        second = status.read_playback(client)
        self.assertEqual(first.song_id, second.song_id)
        self.assertNotEqual(first.now_playing.title, second.now_playing.title)

    def test_empty_stopped_and_paused_queues_are_successful_reads(self):
        client = self.client()
        for state in ("stop", "pause"):
            client.status.return_value = {"state": state, "playlistlength": "0"}
            client.currentsong.return_value = {}
            value = status.read_playback(client)
            self.assertEqual(value.state, state)
            self.assertIsNone(value.song_id)
            self.assertIsNone(value.elapsed_seconds)
            self.assertEqual(value.queue_entries, 0)

    def test_song_id_race_retries_then_publishes_matching_metadata(self):
        client = self.client()
        client.status.side_effect = [self.state(), self.state(songid="2"),
                                     self.state(songid="2"), self.state(songid="2")]
        client.currentsong.side_effect = [{"id": "1", "title": "Old"}, {"id": "2", "title": "New"}]
        value = status.read_playback(client)
        self.assertEqual((value.song_id, value.now_playing.title), ("2", "New"))

    def test_unsettled_song_or_queue_race_is_bounded_and_explicit(self):
        client = self.client()
        for changed in ({"songid": "2"}, {"playlist": "10"}, {"state": "pause"}):
            client.status.side_effect = [self.state(), self.state(**changed)] * 2
            with self.assertRaises(status.InconsistentSnapshot):
                status.read_playback(client)
        self.assertEqual(client.currentsong.call_count, 6)

    def test_malformed_responses_and_nonfinite_fields_fail(self):
        client = self.client()
        for extra in ({"state": "invalid"}, {"elapsed": "nan"}, {"duration": "inf"},
                      {"playlistlength": "-1"}, {"playlistlength": "1.5"},
                      {"songid": "private"}, {"bitrate": True}, {"elapsed": []}):
            client.status.return_value = self.state(**extra)
            with self.subTest(extra=extra), self.assertRaises(status.InvalidObservation):
                status.read_playback(client)
        client.status.return_value = []
        with self.assertRaises(status.InvalidObservation):
            status.read_playback(client)
        client.status.return_value = self.state()
        client.currentsong.return_value = {"id": "1", "title": ["OK", 3]}
        with self.assertRaises(status.InvalidObservation):
            status.read_playback(client)

    def test_mpd_error_does_not_hide_reachability_and_is_redacted(self):
        client = self.client()
        client.status.return_value = self.state(error="Cannot open https://user:pass@host/live?token=secret")
        client.currentsong.return_value = {"id": "1", "title": "https://host/live?token=secret"}
        value = status.read_playback(client).to_dict()
        self.assertTrue(value["reachable"])
        self.assertEqual(value["mpd_error"], "MPD reported a playback error.")
        self.assertIn("[redacted]", value["now_playing"]["title"])
        for secret in ("secret", "pass@", "https://"):
            self.assertNotIn(secret, json.dumps(value))

    def test_stale_and_last_known_probe_fixture_shapes(self):
        fixture = self.fixtures["stale_readiness"]
        probe = status.Probe(
            "ok", fixture["attempted_at"],
            status.TimedValue(fixture["observed_at"], 0, status.Readiness()),
        )
        self.assertEqual(probe.to_dict(60.5, 30), fixture)
        fixture = self.fixtures["unreachable_playback"]
        old = fixture["last_known"]
        value = status.Playback("play", "1", 1, 1234.5, None, 128, "44100:24:2", None,
                                status.NowPlaying(None, None, None, None))
        probe = status.Probe(
            "error", fixture["attempted_at"],
            last_known=status.TimedValue(old["observed_at"], 0, value),
            error=status.ObservationError("mpd_unavailable", "MPD is unreachable."),
        )
        self.assertEqual(probe.to_dict(20, 10), fixture)

    def test_serialized_output_has_exact_contract_fields(self):
        self.refresh()
        self.refresh(audio=True)
        actual = self.output().to_dict(status.time.monotonic())
        expected = self.fixtures["healthy_inventory"]["outputs"][0]
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(actual["gain"], expected["gain"])
        self.assertEqual(set(actual["audio"]), set(expected["audio"]))
        for name in ("readiness", "route", "gain"):
            self.assertEqual(set(actual["audio"][name]), set(expected["audio"][name]))
        self.assertEqual(set(actual["playback"]), set(expected["playback"]))
        json.dumps(actual, allow_nan=False)
        detached = actual["audio"]["gain"]["value"]["measured_units"]
        detached[0] = 0
        self.assertEqual(self.output().gain.current.value.measured_units, (49905, 49905))

    def test_connect_failure_is_sanitized_and_disconnect_attempted(self):
        client = self.client()
        client.connect.side_effect = fake_mpd.MPDError("https://user:secret@private")
        with patch.object(fake_mpd, "MPDClient", return_value=client), self.assertLogs(level="WARNING") as logs:
            self.refresh()
        for output in self.collector.snapshot():
            self.assertEqual(output.playback.status, "error")
            self.assertEqual(output.playback.error.code, "mpd_unavailable")
            self.assertIsNone(output.playback.current)
        client.disconnect.assert_called()
        self.assertNotIn("secret", json.dumps(self.output().to_dict(status.time.monotonic())))
        self.assertNotIn("secret", str(logs.output))

    def test_disconnect_failure_is_not_success(self):
        client = self.client()
        client.disconnect.side_effect = fake_mpd.MPDError("private credential")
        with patch.object(fake_mpd, "MPDClient", return_value=client), self.assertLogs(level="WARNING"):
            self.refresh()
        self.assertEqual(self.output().playback.status, "error")

    def test_connect_and_cleanup_failure_keep_original_error_without_secrets(self):
        client = self.client()
        client.connect.side_effect = fake_mpd.MPDError("secret connect")
        client.disconnect.side_effect = fake_mpd.MPDError("secret cleanup")
        with patch.object(fake_mpd, "MPDClient", return_value=client), self.assertLogs(level="WARNING") as logs:
            self.refresh()
        self.assertNotIn("secret", str(logs.output))
        self.assertEqual(self.output().playback.error.code, "mpd_unavailable")

    def test_error_retains_last_known_then_recovers(self):
        self.refresh()
        first = self.output().playback.current
        client = self.client()
        client.connect.side_effect = OSError("offline")
        with patch.object(fake_mpd, "MPDClient", return_value=client), self.assertLogs(level="WARNING"):
            self.refresh()
        probe = self.output().playback
        self.assertIsNone(probe.current)
        self.assertEqual(probe.last_known, first)
        serialized = probe.to_dict(first.monotonic + 20, 10)
        self.assertTrue(serialized["stale"])
        self.assertEqual(serialized["last_known"]["age_ms"], 20000)
        self.refresh()
        self.assertEqual(self.output().playback.status, "ok")
        self.assertIsNone(self.output().playback.last_known)

    def test_inconsistent_and_invalid_probes_use_contract_error_codes(self):
        client = self.client()
        with patch.object(fake_mpd, "MPDClient", return_value=client):
            with patch.object(status, "read_playback", side_effect=status.InconsistentSnapshot()), self.assertLogs():
                self.refresh()
            self.assertEqual(self.output().playback.error.code, "snapshot_inconsistent")
            with patch.object(status, "read_playback", side_effect=status.InvalidObservation()), self.assertLogs():
                self.refresh()
            self.assertEqual(self.output().playback.error.code, "observation_invalid")

    def test_slow_output_does_not_delay_healthy_publication_or_queue_more_jobs(self):
        started, release, healthy = threading.Event(), threading.Event(), threading.Event()
        original = self.client
        def factory():
            client = original()
            def connect(host, port):
                if port == 6601:
                    started.set()
                    if not release.wait(5):
                        raise OSError("test timed out")
                else:
                    healthy.set()
            client.connect.side_effect = connect
            return client
        with patch.object(fake_mpd, "MPDClient", side_effect=factory):
            try:
                jobs = self.collector.refresh()
                self.assertTrue(started.wait(2))
                self.assertTrue(healthy.wait(2))
                for identity in ("centerlfe", "rear"):
                    self.collector._jobs[(identity, "playback")].result(timeout=2)
                    self.assertEqual(self.output(identity).playback.status, "ok")
                self.assertEqual(self.output().playback.status, "pending")
                slow = self.collector._jobs[("front", "playback")]
                self.collector.refresh()
                self.assertIs(self.collector._jobs[("front", "playback")], slow)
                self.assertLessEqual(len(self.collector._jobs), 3)
            finally:
                release.set()
                for job in jobs:
                    job.result(timeout=5)

    def test_slow_audio_does_not_block_playback_collection(self):
        started, release = threading.Event(), threading.Event()
        def readiness(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise OSError("test timed out")
        self.readiness.side_effect = readiness
        try:
            self.collector.refresh(audio=True)
            self.assertTrue(started.wait(2))
            for identity in ("front", "centerlfe", "rear"):
                self.collector._jobs[(identity, "playback")].result(timeout=2)
                self.assertEqual(self.output(identity).playback.status, "ok")
        finally:
            release.set()

    def test_stopped_and_paused_routes_are_not_checked(self):
        client = self.client()
        for state in ("stop", "pause"):
            client.status.return_value = self.state(state=state)
            with patch.object(fake_mpd, "MPDClient", return_value=client):
                self.refresh()
                self.refresh(audio=True)
            self.assertEqual(self.output().route.status, "not_applicable")
            self.assertEqual(self.output().to_dict(status.time.monotonic())["audio"]["route"],
                             self.fixtures["inactive_route"])
        self.routes.assert_not_called()
        self.gains.assert_called()

    def test_unknown_playback_cannot_be_called_inactive(self):
        client = self.client()
        client.connect.side_effect = OSError("offline")
        with patch.object(fake_mpd, "MPDClient", return_value=client), self.assertLogs():
            self.refresh(audio=True)
        self.assertEqual(self.output().route.status, "pending")
        self.routes.assert_not_called()

    def test_readiness_route_gain_fail_independently_with_sanitized_messages(self):
        self.refresh()
        self.readiness.side_effect = RuntimeError("private audio path")
        self.routes.side_effect = RuntimeError("secret route")
        self.gains.side_effect = subprocess.TimeoutExpired("private pactl", 2)
        with self.assertLogs():
            self.refresh(audio=True)
        output = self.output()
        self.assertEqual(output.readiness.error.code, "audio_unavailable")
        self.assertEqual(output.route.error.code, "route_unavailable")
        self.assertEqual(output.gain.error.code, "gain_unavailable")
        serialized = json.dumps(output.to_dict(status.time.monotonic()))
        self.assertNotIn("private", serialized)
        self.assertNotIn("secret", serialized)
        self.assertEqual(output.configured_db, -7.1)

    def test_incorrect_route_is_observed_not_repaired(self):
        self.refresh()
        self.routes.side_effect = lambda sink: status.Route(f"sink.{sink}", "master", False)
        self.refresh(audio=True)
        self.assertFalse(self.output().route.current.value.correct)
        self.assertEqual(self.output().route.status, "ok")

    def test_freshness_uses_monotonic_time_and_exact_thresholds(self):
        self.refresh()
        probe = self.output().playback
        now = probe.current.monotonic
        self.assertFalse(probe.to_dict(now + 9.999, 10)["stale"])
        self.assertTrue(probe.to_dict(now + 10, 10)["stale"])
        self.assertEqual(probe.to_dict(now - 5, 10)["age_ms"], 0)
        client = self.client()
        client.status.return_value = self.state(state="stop")
        with patch.object(fake_mpd, "MPDClient", return_value=client):
            self.refresh()
            self.refresh(audio=True)
        output = self.output()
        self.assertEqual(output.to_dict(output.playback.current.monotonic + 10)["audio"]["route"]["status"], "pending")

    def test_invalid_inventory_does_not_guess_ids_or_contact_mpd(self):
        for field, value in (("id", None), ("id", "Upper"), ("label", ""),
                             ("label", "\x00"), ("id", "rear")):
            original = self.config["streams"][0][field]
            self.config["streams"][0][field] = value
            self.save()
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                status.PlaybackCollector(self.api)
            self.config["streams"][0][field] = original
        self.assertEqual(self.clients, [])

    def test_config_revision_and_settings_use_same_read(self):
        raw = self.path.read_bytes()
        with patch.object(Path, "read_bytes", return_value=raw) as read:
            config, streams, revision = self.api.configuration_snapshot()
        read.assert_called_once()
        self.assertEqual(revision, hashlib.sha256(raw).hexdigest())
        self.assertEqual(streams[0].gain_db, config["streams"][0]["gain_db"])
        original_revision = self.collector.config_revision
        self.config["streams"][0]["gain_db"] = -9
        self.save()
        self.assertNotEqual(self.api.configuration_snapshot()[2], original_revision)
        self.assertEqual(self.output().configured_db, -7.1)

    def test_close_rejects_new_work_and_surfaces_unexpected_worker_failure(self):
        with patch.object(status, "read_playback", side_effect=TypeError("programming failure")):
            jobs = self.collector.refresh()
            wait(jobs, timeout=5)
        with self.assertRaisesRegex(TypeError, "programming"):
            self.collector.close()
        self.collector._jobs.clear()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.collector.refresh()


class GainInspectionTests(unittest.TestCase):
    def test_stereo_gain_read_has_only_get_command_and_bounded_timeout(self):
        result = subprocess.CompletedProcess([], 0, "left: 49905 / 76%, right: 49905 / 76%", "")
        with patch.object(gain.subprocess, "run", return_value=result) as run:
            self.assertEqual(gain.read_sink_gain("stereo_front"), (49905, 49905))
        self.assertEqual(run.call_args.args[0], ["pactl", "get-sink-volume", "sink.stereo_front"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_invalid_channels_diagnostics_and_command_errors_fail(self):
        for text in ("", "left: 100 / 1%", "left: 1 / 1%, right: 2 / 1%, rear: 3 / 1%"):
            with patch.object(gain.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, text, "")):
                with self.assertRaises(ValueError):
                    gain.read_sink_gain("front")
        with patch.object(gain.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "warning")):
            with self.assertRaises(RuntimeError):
                gain.read_sink_gain("front")
        for error in (OSError("missing"), subprocess.TimeoutExpired("pactl", 2),
                      subprocess.CalledProcessError(1, "pactl")):
            with patch.object(gain.subprocess, "run", side_effect=error):
                with self.assertRaises(type(error)):
                    gain.read_sink_gain("front")

    def test_captured_readiness_never_enforces_profile_or_rereads_config(self):
        config = json.loads((SCRIPTS / "config.json").read_text())
        card = {"name": "alsa_card.usb-0d8c_USB_Sound_Device-00", "active_profile": "output:wrong"}
        with patch.object(ready, "_pactl", side_effect=[json.dumps([card]), "[]"]) as pactl:
            with patch.object(Path, "read_text", side_effect=AssertionError("must not read file")):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    ready.audio_ready_config(config)
        self.assertEqual([call.args for call in pactl.call_args_list],
                         [("--format=json", "list", "cards"), ("--format=json", "list", "sinks")])
