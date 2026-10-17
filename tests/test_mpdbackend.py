import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
fake_mpd = types.ModuleType("mpd")
fake_mpd.MPDError = type("MPDError", (Exception,), {})
fake_mpd.MPDClient = Mock()
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    with patch.dict(sys.modules, {"mpd": fake_mpd}):
        backend = importlib.import_module("lib.mpdbackend")


class MPDBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        config = json.loads((SCRIPTS_DIR / "config.json").read_text())
        self.path.write_text(json.dumps(config))
        self.config = config
        self.api = backend.MPDBackend(self.path, self.path.parent / "playback.json")
        tunein_patcher = patch.object(
            self.api._tunein, "resolve", return_value="https://stream.example/live.mp3",
        )
        self.tunein_resolve = tunein_patcher.start()
        self.addCleanup(tunein_patcher.stop)
        self.client = Mock()
        self.client.status.return_value = {"state": "play", "songid": "7"}
        self.client.playlistinfo.return_value = [
            {"file": config["streams"][0]["stream"], "id": "7"},
        ]
        self.client.addid.return_value = "8"
        for target, value in (("mpd", fake_mpd),):
            patcher = patch.object(backend, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(fake_mpd, "MPDClient", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        for method in ("recover_route", "set_sink_gain", "inspect_route", "wait_for_audio"):
            patcher = patch.object(backend, method)
            setattr(self, method, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def prepare_source_operation(self, initial_state="play"):
        config = json.loads(self.path.read_text())
        for index, stream in enumerate(config["streams"]):
            stream.update(id=f"output_{index}", label=stream["name"])
        self.path.write_text(json.dumps(config))
        queue = [{"file": config["streams"][0]["stream"], "id": "7"}]
        state = {"state": initial_state, "songid": "7"}
        next_id = [8]

        self.client.playlistinfo.side_effect = lambda: [dict(song) for song in queue]
        self.client.status.side_effect = lambda: dict(state)

        def stop():
            state["state"] = "stop"

        def deleteid(song_id):
            queue[:] = [song for song in queue if song["id"] != song_id]

        def addid(uri):
            song_id = str(next_id[0])
            next_id[0] += 1
            queue.append({"file": uri, "id": song_id})
            return song_id

        def playid(song_id):
            state.update(state="play", songid=song_id)

        self.client.stop.side_effect = stop
        self.client.deleteid.side_effect = deleteid
        self.client.addid.side_effect = addid
        self.client.playid.side_effect = playid
        return config, queue, state

    def select_station(self, station_id="s231065", station_name="Radio Example", progress=None):
        _, _, revision = self.api.configuration_snapshot()
        self.api.manual_operation(
            "output_0", "set_tunein_station", None,
            expected_config_revision=revision,
            expected_control_revision=self.api.control_revision(),
            progress=progress or Mock(), station_id=station_id, station_name=station_name,
        )

    def test_list_and_status_selected_and_all(self):
        self.assertEqual(len(self.api.list_streams()), 3)
        status = self.api.status("Stereo Front")[0]
        self.assertEqual(status.state, "play")
        self.assertEqual(status.gain_db, self.config["streams"][0]["gain_db"])
        self.assertEqual(len(self.api.status()), 3)
        self.client.disconnect.assert_called()

    def test_repeated_start_reuses_queue_without_restarting_current_song(self):
        for _ in range(2):
            self.api.start("Stereo Front")
        self.client.addid.assert_not_called()
        self.client.playid.assert_not_called()
        self.assertEqual(self.recover_route.call_count, 2)
        self.set_sink_gain.assert_called_with("stereo_front", self.config["streams"][0]["gain_db"])

    def test_tunein_station_selection_replaces_only_owned_song_and_reuses_station(self):
        original, queue, state = self.prepare_source_operation()
        first = "https://stream.example/live.mp3"
        second = "https://stream2.example/live.mp3"
        phases = []
        self.tunein_resolve.side_effect = [first, first, second]
        self.select_station(progress=phases.append)
        self.select_station()
        self.select_station("s67089", "Radio Other")

        saved = json.loads(self.path.read_text())
        selected = saved["streams"][0]
        self.assertEqual(selected["stream"], second)
        self.assertEqual(selected["source_provider"], "tunein")
        self.assertEqual(selected["source_station_id"], "s67089")
        self.assertEqual(selected["source_station_name"], "Radio Other")
        self.assertTrue(selected["queue_managed"])
        self.assertEqual(selected["managed_song_id"], "9")
        self.assertEqual(saved["streams"][1:], original["streams"][1:])
        self.assertEqual(saved["outputmappers"], original["outputmappers"])
        self.assertEqual(queue, [{"file": second, "id": "9"}])
        self.assertEqual(state, {"state": "play", "songid": "9"})
        self.client.addid.assert_any_call(first)
        self.client.addid.assert_any_call(second)
        self.assertEqual(self.client.addid.call_count, 2)
        self.assertEqual([call.args[0] for call in self.client.deleteid.call_args_list], ["7", "8"])
        self.assertIn("source_persisted", phases)
        self.assertEqual(self.api._intentions()["Stereo Front"], "play")
        restarted = backend.MPDBackend(self.path, self.api.state_path)
        add_count = self.client.addid.call_count
        with patch.object(restarted._tunein, "resolve", return_value=second):
            restarted.reconcile("Stereo Front")
        self.assertEqual(self.client.addid.call_count, add_count)
        self.assertEqual(queue, [{"file": second, "id": "9"}])

    def test_tunein_selection_resolves_once_and_waits_longer_for_new_stream_route(self):
        self.prepare_source_operation()
        self.select_station()
        self.assertEqual(self.tunein_resolve.call_count, 1)
        self.recover_route.assert_called_once_with(
            "stereo_front", timeout=backend.STARTUP_ROUTE_TIMEOUT,
        )

    def test_reconcile_skips_tunein_resolution_while_saved_source_plays(self):
        _, queue, state = self.prepare_source_operation()
        self.select_station()
        self.tunein_resolve.reset_mock()
        self.recover_route.reset_mock()
        self.api.reconcile("Stereo Front")
        self.tunein_resolve.assert_not_called()
        self.recover_route.assert_called_once_with("stereo_front")

        state["error"] = "stream failed"
        self.client.clearerror.side_effect = lambda: state.pop("error", None)
        self.api.reconcile("Stereo Front")
        self.tunein_resolve.assert_called_once_with("s231065")
        self.assertEqual(len(queue), 1)

    def test_tunein_station_selection_starts_from_stopped_state(self):
        self.prepare_source_operation("stop")
        self.select_station()
        self.assertEqual(self.client.playid.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text())["streams"][0]["stream"],
                         "https://stream.example/live.mp3")

    def test_tunein_station_selection_starts_from_paused_state(self):
        self.prepare_source_operation("pause")
        self.select_station()
        self.assertEqual(self.client.playid.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text())["streams"][0]["stream"],
                         "https://stream.example/live.mp3")

    def test_tunein_station_selection_refuses_unmanaged_queue_without_persisting(self):
        _, queue, _ = self.prepare_source_operation()
        queue.append({"file": "https://unmanaged.example/live", "id": "99"})
        original = self.path.read_bytes()
        with self.assertRaisesRegex(backend.MPDOperationError, "unmanaged"):
            self.select_station()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.api.state_path.exists())
        self.client.deleteid.assert_not_called()
        self.client.addid.assert_not_called()

    def test_tunein_station_persistence_failure_leaves_selection_unchanged(self):
        self.prepare_source_operation()
        original = self.path.read_bytes()
        with patch.object(backend, "write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(backend.BackendError, "persist"):
                self.select_station()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.api.state_path.exists())
        self.client.deleteid.assert_not_called()
        self.client.addid.assert_not_called()

    def test_failed_station_switch_keeps_desired_station_and_supervisor_retries(self):
        _, queue, _ = self.prepare_source_operation()
        self.tunein_resolve.return_value = "https://stream.example/live.mp3"
        self.client.deleteid.side_effect = fake_mpd.MPDError("private URL secret")
        with self.assertRaises(backend.MPDOperationError) as caught:
            self.select_station("s8080", "New Radio")
        self.assertNotIn("secret", str(caught.exception))
        saved = json.loads(self.path.read_text())["streams"][0]
        self.assertEqual(saved["stream"], "https://stream.example/live.mp3")
        self.assertEqual(saved["source_station_id"], "s8080")
        self.assertEqual(self.api._intentions()["Stereo Front"], "play")
        self.client.deleteid.side_effect = lambda song_id: queue.__setitem__(
            slice(None), [song for song in queue if song["id"] != song_id],
        )
        self.api.reconcile("Stereo Front")
        self.assertEqual(self.client.addid.call_args.args[0], "https://stream.example/live.mp3")

    def test_start_adds_missing_url_and_plays_it(self):
        self.client.playlistinfo.return_value = []
        self.api.start("Stereo Front")
        self.client.addid.assert_called_once_with(self.config["streams"][0]["stream"])
        self.client.playid.assert_called_once_with("8")

    def test_pause_stop_preserve_playlist_and_verify_state(self):
        for action, state in ((self.api.pause, "pause"), (self.api.stop, "stop")):
            self.client.status.return_value = {"state": state}
            action("Stereo CenterLFE")
        self.client.pause.assert_called_once_with(1)
        self.client.stop.assert_called_once_with()
        self.client.playlistinfo.assert_not_called()

    def test_gain_active_applies_and_persists_preserving_fields_and_mode(self):
        self.path.chmod(0o640)
        self.api.set_gain("Stereo Front", -6.4)
        self.recover_route.assert_called_once_with("stereo_front")
        self.set_sink_gain.assert_called_once_with("stereo_front", -6.4)
        updated = json.loads(self.path.read_text())
        self.assertEqual(updated["streams"][0]["gain_db"], -6.4)
        self.assertEqual(updated["outputmappers"], self.config["outputmappers"])
        self.assertEqual(updated["streams"][1], self.config["streams"][1])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.api.list_streams()[0].gain_db, -6.4)
        self.assertEqual(self.api.get_gain("Stereo Front"), -6.4)

    def test_stopped_and_paused_gain_persists_without_starting_audio(self):
        for state in ("stop", "pause"):
            self.client.status.return_value = {"state": state}
            self.api.set_gain("Stereo Front", -3)
        self.set_sink_gain.assert_not_called()
        self.recover_route.assert_not_called()
        self.client.playid.assert_not_called()

    def test_gain_apply_failure_leaves_configuration_unchanged(self):
        original = self.path.read_bytes()
        self.set_sink_gain.side_effect = RuntimeError("sink failed")
        with self.assertRaises(backend.BackendError):
            self.api.set_gain("Stereo Front", -6)
        self.assertEqual(self.path.read_bytes(), original)

    def test_persistence_failure_rolls_back_live_gain_and_cleans_temporary_file(self):
        original = self.path.read_bytes()
        with patch.object(backend.os, "replace", side_effect=OSError("read only")):
            with self.assertRaisesRegex(backend.BackendError, "cannot persist"):
                self.api.set_gain("Stereo Front", -6)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([call.args for call in self.set_sink_gain.call_args_list],
                         [("stereo_front", -6), ("stereo_front", self.config["streams"][0]["gain_db"])])
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["config.json", "mpd_control.lock"])

    def test_failed_start_stops_and_disconnects_and_unlocks(self):
        self.recover_route.side_effect = RuntimeError("wrong route")
        with self.assertRaisesRegex(backend.BackendError, "wrong route"):
            self.api.start("Stereo Front")
        self.client.stop.assert_called_once()
        self.client.disconnect.assert_called_once()
        with backend.setup_lock("mpd_control.lock"):
            pass

    def test_protocol_and_connect_errors_surface_with_disconnect(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        with self.assertRaisesRegex(backend.BackendError, "offline"):
            self.api.status("Stereo Front")
        self.client.disconnect.assert_called_once()

    def test_disconnect_failure_preserves_original_error(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        self.client.disconnect.side_effect = fake_mpd.MPDError("cleanup failed")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(backend.BackendError, "offline"):
            self.api.status("Stereo Front")

    def test_rollback_failure_reports_both_errors(self):
        self.set_sink_gain.side_effect = [None, RuntimeError("rollback failed")]
        with patch.object(backend.os, "replace", side_effect=OSError("read only")):
            with self.assertRaisesRegex(backend.BackendError, "read only.*rollback.*rollback failed"):
                self.api.set_gain("Stereo Front", -6)

    def test_failed_stop_after_start_error_reports_both_errors(self):
        self.recover_route.side_effect = RuntimeError("wrong route")
        self.client.stop.side_effect = fake_mpd.MPDError("stop failed")
        with self.assertRaisesRegex(backend.BackendError, "wrong route.*stop.*stop failed"):
            self.api.start("Stereo Front")

    def test_status_invalid_state_and_missing_configuration_fail(self):
        self.client.status.return_value = {"state": "invalid"}
        with self.assertRaisesRegex(backend.BackendError, "invalid MPD"):
            self.api.status("Stereo Front")
        self.path.unlink()
        with self.assertRaisesRegex(backend.BackendError, "Cannot read"):
            self.api.list_streams()

    def test_gain_saved_while_stopped_is_used_on_next_start(self):
        self.client.status.return_value = {"state": "stop"}
        self.api.set_gain("Stereo Front", -6)
        self.client.status.return_value = {"state": "play", "songid": "7"}
        self.api.start("Stereo Front")
        self.set_sink_gain.assert_called_once_with("stereo_front", -6)

    def test_first_boot_defaults_playing_but_redeployment_retains_intentions(self):
        self.api.initialize_intentions()
        self.assertEqual(self.api._intentions(), {s["name"]: "play" for s in self.config["streams"]})
        self.client.status.return_value = {"state": "stop"}
        self.api.stop("Stereo Front")
        self.api.initialize_intentions()
        self.assertEqual(self.api._intentions()["Stereo Front"], "stop")

    def test_intention_is_saved_even_if_mpd_is_offline(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        with self.assertRaises(backend.BackendError):
            self.api.stop("Stereo Front")
        self.assertEqual(self.api._intentions()["Stereo Front"], "stop")

    def test_reconcile_stopped_and_paused_intentions_never_start_audio(self):
        self.api._record_intention(self.api._select("Stereo Front"), "stop")
        self.client.status.side_effect = [{"state": "play"}, {"state": "stop"}]
        self.api.reconcile("Stereo Front")
        self.api._record_intention(self.api._select("Stereo Front"), "pause")
        self.client.status.side_effect = [{"state": "stop"}, {"state": "stop"}]
        self.api.reconcile("Stereo Front")
        self.client.playid.assert_not_called()
        self.wait_for_audio.assert_not_called()

    def test_reconcile_audio_failure_resumes_muted_then_routes_gains_and_unmutes(self):
        self.api.initialize_intentions()
        self.client.status.side_effect = [{"state": "pause", "error": "Failed to open audio output"},
                                          {"state": "play"}]
        calls = Mock()
        for name, method in (("volume", self.client.setvol), ("clear", self.client.clearerror),
                             ("play", self.client.playid), ("route", self.recover_route),
                             ("gain", self.set_sink_gain)):
            calls.attach_mock(method, name)
        self.api.reconcile("Stereo Front")
        self.assertEqual([c[0] for c in calls.mock_calls], ["volume", "clear", "play", "route", "gain", "volume"])
        self.assertEqual([c.args for c in self.client.setvol.call_args_list], [(0,), (100,)])
        self.assertEqual(self.api._intentions()["Stereo Front"], "play")

    def test_intention_save_failure_prevents_playback_mutation(self):
        with patch.object(backend, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(backend.BackendError):
                self.api.start("Stereo Front")
        self.client.connect.assert_not_called()

    def test_corrupted_intentions_fail_instead_of_defaulting_to_play(self):
        self.api.state_path.write_text('{"Stereo Front": "invalid"}')
        with self.assertRaises(ValueError):
            self.api.initialize_intentions()
        self.client.playid.assert_not_called()

    def test_readiness_failure_keeps_play_intention_but_never_starts(self):
        self.wait_for_audio.side_effect = RuntimeError("missing sink")
        with self.assertRaises(backend.BackendError):
            self.api.start("Stereo Front")
        self.assertEqual(self.api._intentions()["Stereo Front"], "play")
        self.client.connect.assert_not_called()

    def test_selector_recovery_preserves_stopped_paused_intentions_and_gain(self):
        self.config["outputmappers"]["audio_devices"][0]["hardware-selector"] = {
            "vendor": "0d8c", "product": "0102",
        }
        self.path.write_text(json.dumps(self.config))
        self.api.initialize_intentions()
        self.api._record_intention(self.api._select("Stereo CenterLFE"), "pause")
        self.api._record_intention(self.api._select("Stereo Rear"), "stop")
        config_before = self.path.read_bytes()
        intentions_before = self.api.state_path.read_bytes()
        self.wait_for_audio.side_effect = RuntimeError("Selected USB audio hardware is ambiguous")
        with self.assertRaisesRegex(backend.BackendError, "ambiguous"):
            self.api.reconcile("Stereo Front")
        self.client.connect.assert_not_called()
        self.client.status.return_value = {"state": "stop"}
        self.api.reconcile("Stereo CenterLFE")
        self.api.reconcile("Stereo Rear")
        self.assertEqual(self.wait_for_audio.call_count, 1)
        self.client.playid.assert_not_called()
        self.client.setvol.assert_not_called()
        self.set_sink_gain.assert_not_called()
        self.assertEqual(config_before, self.path.read_bytes())
        self.assertEqual(intentions_before, self.api.state_path.read_bytes())

    def test_invalid_selector_rejected_during_configuration_snapshot(self):
        self.config["outputmappers"]["audio_devices"][0]["hardware-selector"] = {
            "vendor": True, "product": "0102",
        }
        self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "USB identifier"):
            self.api.configuration_snapshot()
        self.client.connect.assert_not_called()

    def test_busy_lock_raises_exception_not_systemexit(self):
        with backend.setup_lock("mpd_control.lock"):
            with self.assertRaisesRegex(backend.BackendError, "busy"):
                self.api.stop()
        self.client.connect.assert_not_called()

    def test_invalid_gain_name_and_duplicate_ports_have_no_side_effects(self):
        for value in (True, "0", 1, float("nan")):
            with self.assertRaises(ValueError):
                self.api.set_gain("Stereo Front", value)
        with self.assertRaises(ValueError):
            self.api.start("missing")
        self.config["streams"][1]["port"] = 6601
        self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "port"):
            self.api.list_streams()
        self.client.connect.assert_not_called()

    def test_stream_configuration_requires_direct_http_urls(self):
        for value in (
            "file:///tmp/radio", "https:///radio", "https://radio.example/live#fragment",
            "https://radio.example:" + "8" * 1200,
        ):
            with self.subTest(value=value[:48]):
                self.config["streams"][0]["stream"] = value
                self.path.write_text(json.dumps(self.config))
                with self.assertRaisesRegex(ValueError, "HTTP"):
                    self.api.list_streams()
        self.client.connect.assert_not_called()

    def test_direct_stream_entries_still_require_a_url(self):
        del self.config["streams"][0]["stream"]
        self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "HTTP"):
            self.api.list_streams()

    def test_tunein_entry_without_url_resolves_and_saves_stream_on_start(self):
        rear = self.config["streams"][2]
        self.assertNotIn("stream", rear)
        self.assertEqual(rear["source_provider"], "tunein")
        self.assertEqual(self.api.list_streams()[2].url, "")
        queue: list[dict[str, str]] = []
        self.client.playlistinfo.side_effect = lambda: [dict(song) for song in queue]
        self.client.status.side_effect = lambda: {"state": "play", "songid": "8"}

        def addid(uri):
            queue.append({"file": uri, "id": "8"})
            return "8"

        self.client.addid.side_effect = addid
        self.api.initialize_intentions()
        self.api.reconcile("Stereo Rear")
        self.tunein_resolve.assert_called_once_with(rear["source_station_id"])
        saved = json.loads(self.path.read_text())["streams"][2]
        self.assertEqual(saved["stream"], "https://stream.example/live.mp3")
        self.assertEqual(saved["managed_song_id"], "8")
        self.assertEqual(queue, [{"file": "https://stream.example/live.mp3", "id": "8"}])

    def test_route_methods_do_not_touch_playback_or_gain(self):
        self.api.check_route("Stereo Front")
        self.inspect_route.assert_called_once_with("stereo_front")
        self.assertEqual(len(self.api.recover_route()), 3)
        self.client.connect.assert_not_called()
        self.set_sink_gain.assert_not_called()
