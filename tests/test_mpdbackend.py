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

    def test_list_and_status_selected_and_all(self):
        self.assertEqual(len(self.api.list_streams()), 3)
        status = self.api.status("Radio 88.6")[0]
        self.assertEqual(status.state, "play")
        self.assertEqual(status.gain_db, self.config["streams"][0]["gain_db"])
        self.assertEqual(len(self.api.status()), 3)
        self.client.disconnect.assert_called()

    def test_repeated_start_reuses_queue_without_restarting_current_song(self):
        for _ in range(2):
            self.api.start("Radio 88.6")
        self.client.addid.assert_not_called()
        self.client.playid.assert_not_called()
        self.assertEqual(self.recover_route.call_count, 2)
        self.set_sink_gain.assert_called_with("stereo_front", self.config["streams"][0]["gain_db"])

    def test_start_adds_missing_url_and_plays_it(self):
        self.client.playlistinfo.return_value = []
        self.api.start("Radio 88.6")
        self.client.addid.assert_called_once_with(self.config["streams"][0]["stream"])
        self.client.playid.assert_called_once_with("8")

    def test_pause_stop_preserve_playlist_and_verify_state(self):
        for action, state in ((self.api.pause, "pause"), (self.api.stop, "stop")):
            self.client.status.return_value = {"state": state}
            action("FM4 Radio")
        self.client.pause.assert_called_once_with(1)
        self.client.stop.assert_called_once_with()
        self.client.playlistinfo.assert_not_called()

    def test_gain_active_applies_and_persists_preserving_fields_and_mode(self):
        self.path.chmod(0o640)
        self.api.set_gain("Radio 88.6", -6.4)
        self.recover_route.assert_called_once_with("stereo_front")
        self.set_sink_gain.assert_called_once_with("stereo_front", -6.4)
        updated = json.loads(self.path.read_text())
        self.assertEqual(updated["streams"][0]["gain_db"], -6.4)
        self.assertEqual(updated["outputmappers"], self.config["outputmappers"])
        self.assertEqual(updated["streams"][1], self.config["streams"][1])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.api.list_streams()[0].gain_db, -6.4)
        self.assertEqual(self.api.get_gain("Radio 88.6"), -6.4)

    def test_stopped_and_paused_gain_persists_without_starting_audio(self):
        for state in ("stop", "pause"):
            self.client.status.return_value = {"state": state}
            self.api.set_gain("Radio 88.6", -3)
        self.set_sink_gain.assert_not_called()
        self.recover_route.assert_not_called()
        self.client.playid.assert_not_called()

    def test_gain_apply_failure_leaves_configuration_unchanged(self):
        original = self.path.read_bytes()
        self.set_sink_gain.side_effect = RuntimeError("sink failed")
        with self.assertRaises(backend.BackendError):
            self.api.set_gain("Radio 88.6", -6)
        self.assertEqual(self.path.read_bytes(), original)

    def test_persistence_failure_rolls_back_live_gain_and_cleans_temporary_file(self):
        original = self.path.read_bytes()
        with patch.object(backend.os, "replace", side_effect=OSError("read only")):
            with self.assertRaisesRegex(backend.BackendError, "cannot persist"):
                self.api.set_gain("Radio 88.6", -6)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([call.args for call in self.set_sink_gain.call_args_list],
                         [("stereo_front", -6), ("stereo_front", self.config["streams"][0]["gain_db"])])
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["config.json", "mpd_control.lock"])

    def test_failed_start_stops_and_disconnects_and_unlocks(self):
        self.recover_route.side_effect = RuntimeError("wrong route")
        with self.assertRaisesRegex(backend.BackendError, "wrong route"):
            self.api.start("Radio 88.6")
        self.client.stop.assert_called_once()
        self.client.disconnect.assert_called_once()
        with backend.setup_lock("mpd_control.lock"):
            pass

    def test_protocol_and_connect_errors_surface_with_disconnect(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        with self.assertRaisesRegex(backend.BackendError, "offline"):
            self.api.status("Radio 88.6")
        self.client.disconnect.assert_called_once()

    def test_disconnect_failure_preserves_original_error(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        self.client.disconnect.side_effect = fake_mpd.MPDError("cleanup failed")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(backend.BackendError, "offline"):
            self.api.status("Radio 88.6")

    def test_rollback_failure_reports_both_errors(self):
        self.set_sink_gain.side_effect = [None, RuntimeError("rollback failed")]
        with patch.object(backend.os, "replace", side_effect=OSError("read only")):
            with self.assertRaisesRegex(backend.BackendError, "read only.*rollback.*rollback failed"):
                self.api.set_gain("Radio 88.6", -6)

    def test_failed_stop_after_start_error_reports_both_errors(self):
        self.recover_route.side_effect = RuntimeError("wrong route")
        self.client.stop.side_effect = fake_mpd.MPDError("stop failed")
        with self.assertRaisesRegex(backend.BackendError, "wrong route.*stop.*stop failed"):
            self.api.start("Radio 88.6")

    def test_status_invalid_state_and_missing_configuration_fail(self):
        self.client.status.return_value = {"state": "invalid"}
        with self.assertRaisesRegex(backend.BackendError, "invalid MPD"):
            self.api.status("Radio 88.6")
        self.path.unlink()
        with self.assertRaisesRegex(backend.BackendError, "Cannot read"):
            self.api.list_streams()

    def test_gain_saved_while_stopped_is_used_on_next_start(self):
        self.client.status.return_value = {"state": "stop"}
        self.api.set_gain("Radio 88.6", -6)
        self.client.status.return_value = {"state": "play", "songid": "7"}
        self.api.start("Radio 88.6")
        self.set_sink_gain.assert_called_once_with("stereo_front", -6)

    def test_first_boot_defaults_playing_but_redeployment_retains_intentions(self):
        self.api.initialize_intentions()
        self.assertEqual(self.api._intentions(), {s["name"]: "play" for s in self.config["streams"]})
        self.client.status.return_value = {"state": "stop"}
        self.api.stop("Radio 88.6")
        self.api.initialize_intentions()
        self.assertEqual(self.api._intentions()["Radio 88.6"], "stop")

    def test_intention_is_saved_even_if_mpd_is_offline(self):
        self.client.connect.side_effect = fake_mpd.MPDError("offline")
        with self.assertRaises(backend.BackendError):
            self.api.stop("Radio 88.6")
        self.assertEqual(self.api._intentions()["Radio 88.6"], "stop")

    def test_reconcile_stopped_and_paused_intentions_never_start_audio(self):
        self.api._record_intention(self.api._select("Radio 88.6"), "stop")
        self.client.status.side_effect = [{"state": "play"}, {"state": "stop"}]
        self.api.reconcile("Radio 88.6")
        self.api._record_intention(self.api._select("Radio 88.6"), "pause")
        self.client.status.side_effect = [{"state": "stop"}, {"state": "stop"}]
        self.api.reconcile("Radio 88.6")
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
        self.api.reconcile("Radio 88.6")
        self.assertEqual([c[0] for c in calls.mock_calls], ["volume", "clear", "play", "route", "gain", "volume"])
        self.assertEqual([c.args for c in self.client.setvol.call_args_list], [(0,), (100,)])
        self.assertEqual(self.api._intentions()["Radio 88.6"], "play")

    def test_intention_save_failure_prevents_playback_mutation(self):
        with patch.object(backend, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(backend.BackendError):
                self.api.start("Radio 88.6")
        self.client.connect.assert_not_called()

    def test_corrupted_intentions_fail_instead_of_defaulting_to_play(self):
        self.api.state_path.write_text('{"Radio 88.6": "invalid"}')
        with self.assertRaises(ValueError):
            self.api.initialize_intentions()
        self.client.playid.assert_not_called()

    def test_readiness_failure_keeps_play_intention_but_never_starts(self):
        self.wait_for_audio.side_effect = RuntimeError("missing sink")
        with self.assertRaises(backend.BackendError):
            self.api.start("Radio 88.6")
        self.assertEqual(self.api._intentions()["Radio 88.6"], "play")
        self.client.connect.assert_not_called()

    def test_busy_lock_raises_exception_not_systemexit(self):
        with backend.setup_lock("mpd_control.lock"):
            with self.assertRaisesRegex(backend.BackendError, "busy"):
                self.api.stop()
        self.client.connect.assert_not_called()

    def test_invalid_gain_name_and_duplicate_ports_have_no_side_effects(self):
        for value in (True, "0", 1, float("nan")):
            with self.assertRaises(ValueError):
                self.api.set_gain("Radio 88.6", value)
        with self.assertRaises(ValueError):
            self.api.start("missing")
        self.config["streams"][1]["port"] = 6601
        self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "port"):
            self.api.list_streams()
        self.client.connect.assert_not_called()

    def test_route_methods_do_not_touch_playback_or_gain(self):
        self.api.check_route("Radio 88.6")
        self.inspect_route.assert_called_once_with("stereo_front")
        self.assertEqual(len(self.api.recover_route()), 3)
        self.client.connect.assert_not_called()
        self.set_sink_gain.assert_not_called()
