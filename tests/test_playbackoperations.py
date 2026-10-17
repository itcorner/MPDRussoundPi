import importlib
import json
from contextlib import closing
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
from werkzeug.serving import make_server

from web.playback_status import PlaybackAgentConfig, PlaybackStatusClient
from web.russound_server import RussoundHTTPServer

import test_mpdbackend as backend_tests

backend = backend_tests.backend
with patch.object(sys, "path", [str(backend_tests.SCRIPTS_DIR), *sys.path]):
    with patch.dict(sys.modules, {"lib.mpdbackend": backend}):
        operations = importlib.reload(importlib.import_module("lib.playbackoperations"))
        agent = importlib.reload(importlib.import_module("lib.playbackagent"))


class PlaybackOperationTests(unittest.TestCase):
    setUp = backend_tests.MPDBackendTests.setUp

    def prepare(self, **kwargs):
        for index, stream in enumerate(self.config["streams"]):
            stream.update(id=f"output_{index}", label=stream["name"])
        self.path.write_text(json.dumps(self.config))
        self.manager = operations.OperationManager(self.api, self.path.parent / "operations.sqlite3", **kwargs)
        self.addCleanup(self.manager.close)
        return self.manager

    def request(self, action="stop", **kwargs):
        _, _, revision = self.api.configuration_snapshot()
        return {
            "action": action,
            "expected_config_revision": revision,
            "expected_control_revision": self.api.control_revision(),
            "idempotency_key": "a-unique-key-for-test", **kwargs,
        }

    def test_acceptance_is_durable_and_does_not_mutate_playback(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request())
        self.assertEqual(record["status"], "queued")
        self.client.stop.assert_not_called()
        self.assertFalse(self.api.state_path.exists())
        with closing(sqlite3.connect(manager.path)) as db:
            saved = json.loads(db.execute("SELECT result FROM operations").fetchone()[0])
        self.assertEqual(record, saved)
        self.assertEqual(manager.path.stat().st_mode & 0o777, 0o600)

    def test_lost_response_retry_uses_same_id_even_after_execution(self):
        manager = self.prepare()
        request = self.request()
        first = manager.submit("output_0", request)
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        second = manager.submit("output_0", request)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "applied")
        self.client.stop.assert_called_once()
        self.assertTrue(second["requested_intention_persisted"])

    def test_same_key_different_payload_conflicts_and_invalid_requests_rejected(self):
        manager = self.prepare()
        manager.submit("output_0", self.request())
        with self.assertRaises(operations.OperationError) as caught:
            manager.submit("output_0", self.request("play"))
        self.assertEqual(caught.exception.status, 409)
        for changes in (
            {"action": "clear"}, {"gain_db": -3}, {"expected_control_revision": "invalid"},
            {"idempotency_key": "short"}, {"action": "set_gain", "gain_db": True},
            {"action": "set_gain", "gain_db": float("nan")}, {"action": "set_gain", "gain_db": 1},
        ):
            with self.assertRaises(operations.OperationError):
                manager.submit("output_0", self.request(**changes))
        self.client.stop.assert_not_called()

    def test_output_lookup_is_stable_id_and_missing_output_is_404(self):
        manager = self.prepare()
        with self.assertRaises(operations.OperationError) as caught:
            manager.submit("unknown", self.request())
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(operations.OperationError):
            manager.submit("../other", self.request())

    def test_capacity_rejects_before_acceptance(self):
        manager = self.prepare(capacity=1)
        manager.submit("output_0", self.request())
        with self.assertRaises(operations.OperationError) as caught:
            manager.submit("output_1", self.request())
        self.assertEqual(caught.exception.status, 429)

    def test_newer_queued_manual_command_supersedes_older(self):
        manager = self.prepare()
        old = manager.submit("output_0", self.request("play"))
        new = manager.submit("output_0", self.request("stop", idempotency_key="a-second-unique-key"))
        self.assertEqual(manager.get(old["id"])["status"], "superseded")
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        self.assertEqual(manager.get(new["id"])["status"], "applied")
        self.client.playid.assert_not_called()

    def test_submission_and_execution_reject_stale_configuration(self):
        manager = self.prepare()
        request = self.request()
        accepted = manager.submit("output_0", request)
        self.config["streams"][1]["gain_db"] = -4
        self.path.write_text(json.dumps(self.config))
        with self.assertRaises(operations.OperationError) as caught:
            manager.submit("output_1", request)
        self.assertEqual(caught.exception.status, 409)
        manager.run_once()
        self.assertEqual(manager.get(accepted["id"])["error"]["code"], "revision_conflict")
        self.client.stop.assert_not_called()

    def test_cli_intention_write_even_same_value_invalidates_queued_operation(self):
        manager = self.prepare()
        self.client.status.return_value = {"state": "stop"}
        self.api.stop("FM4 Radio")
        accepted = manager.submit("output_0", self.request())
        self.api.stop("FM4 Radio")
        self.client.stop.reset_mock()
        manager.run_once()
        self.assertEqual(manager.get(accepted["id"])["error"]["code"], "revision_conflict")
        self.client.stop.assert_not_called()

    def test_shared_backend_lock_does_not_reject_read_only_submission(self):
        manager = self.prepare()
        with backend.setup_lock("mpd_control.lock"):
            record = manager.submit("output_0", self.request())
        self.assertEqual(record["status"], "queued")
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        self.assertEqual(manager.get(record["id"])["status"], "applied")

    def test_busy_execution_waits_for_lock_and_applies_once(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request())
        self.client.status.return_value = {"state": "stop"}
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            with backend.setup_lock("mpd_control.lock"):
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(locked.wait(2))

        def release_lock(_):
            self.assertEqual(manager.get(record["id"])["phase"], "waiting_for_backend")
            release.set()
            holder.join(2)
            self.assertFalse(holder.is_alive())

        with patch.object(manager._stop, "wait", side_effect=release_lock):
            manager.run_once()
        self.assertEqual(manager.get(record["id"])["status"], "applied")
        self.client.stop.assert_called_once()

    def test_busy_execution_stops_at_deadline_without_mutation(self):
        manager = self.prepare(deadline_seconds=0.05)
        record = manager.submit("output_0", self.request())
        with backend.setup_lock("mpd_control.lock"):
            manager.run_once()
        self.assertEqual(manager.get(record["id"])["error"]["code"], "backend_busy")
        self.client.stop.assert_not_called()

    def test_consecutive_gain_edits_survive_transient_contention(self):
        manager = self.prepare()
        self.client.status.return_value = {"state": "play"}
        actual = self.api.manual_operation
        for index, gain in enumerate((-11.2, -18.1)):
            request = self.request("set_gain", gain_db=gain, idempotency_key=f"consecutive-gain-{index}")
            record = manager.submit("output_0", request)
            attempts = []

            def execute(*args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise backend.BackendBusy("supervisor")
                return actual(*args, **kwargs)

            with patch.object(self.api, "manual_operation", side_effect=execute):
                manager.run_once()
            self.assertEqual(manager.get(record["id"])["status"], "applied")
            self.assertEqual(self.api.get_gain("Radio 88.6"), gain)
            self.assertEqual(len(attempts), 2)
        self.assertEqual(self.set_sink_gain.call_count, 2)

    def test_waiting_for_lock_still_checks_revisions_before_mutating(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request())
        actual = self.api.manual_operation
        attempts = []

        def execute(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                self.config["streams"][1]["gain_db"] = -4
                self.path.write_text(json.dumps(self.config))
                raise backend.BackendBusy("supervisor")
            return actual(*args, **kwargs)

        with patch.object(self.api, "manual_operation", side_effect=execute):
            manager.run_once()
        self.assertEqual(manager.get(record["id"])["error"]["code"], "revision_conflict")
        self.client.stop.assert_not_called()

    def test_expired_operation_does_not_mutate(self):
        manager = self.prepare(deadline_seconds=0)
        record = manager.submit("output_0", self.request())
        manager.run_once()
        self.assertEqual(manager.get(record["id"])["status"], "expired")
        self.client.stop.assert_not_called()
        self.assertFalse(self.api.state_path.exists())

    def test_failed_mpd_command_preserves_saved_intention_and_reports_partial_outcome(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request())
        self.client.stop.side_effect = backend.mpd.MPDError("private-stream-url")
        manager.run_once()
        result = manager.get(record["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "mpd_failed")
        self.assertTrue(result["requested_intention_persisted"])
        self.assertIsNone(result["applied"])
        self.assertNotIn("private-stream-url", json.dumps(result))
        self.assertEqual(json.loads(self.api.state_path.read_text()), {"Radio 88.6": "stop"})

    def test_failed_audio_readiness_preserves_play_intention(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request("play"))
        self.wait_for_audio.side_effect = RuntimeError("unavailable")
        manager.run_once()
        result = manager.get(record["id"])
        self.assertEqual(result["error"]["code"], "audio_failed")
        self.assertTrue(result["requested_intention_persisted"])
        self.client.playid.assert_not_called()

    def test_intention_save_failure_never_sends_mpd_command(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request())
        with patch.object(backend, "write_json", side_effect=OSError("read only")):
            manager.run_once()
        result = manager.get(record["id"])
        self.assertEqual(result["error"]["code"], "intention_save_failed")
        self.assertFalse(result["requested_intention_persisted"])
        self.client.stop.assert_not_called()

    def test_selected_output_preserves_other_intentions_and_calibration(self):
        manager = self.prepare()
        self.api.initialize_intentions()
        config_before = self.path.read_bytes()
        record = manager.submit("output_0", self.request())
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        self.assertEqual(manager.get(record["id"])["status"], "applied")
        self.assertEqual(self.path.read_bytes(), config_before)
        self.assertEqual(json.loads(self.api.state_path.read_text()), {
            "Radio 88.6": "stop", "FM4 Radio": "play", "OE3 Radio": "play",
        })
        self.client.connect.assert_called_once_with("localhost", self.config["streams"][0]["port"])
        self.set_sink_gain.assert_not_called()

    def test_gain_while_paused_is_persisted_without_starting_and_reapplied_later(self):
        manager = self.prepare()
        self.client.status.return_value = {"state": "pause"}
        record = manager.submit("output_0", self.request("set_gain", gain_db=-65.1))
        manager.run_once()
        result = manager.get(record["id"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["gain_application"], "on_next_play")
        self.assertTrue(result["gain_persisted"])
        self.set_sink_gain.assert_not_called()
        self.client.playid.assert_not_called()
        self.assertEqual(self.api.get_gain("Radio 88.6"), -65.1)
        self.client.status.return_value = {"state": "play", "songid": "7"}
        self.api.start("Radio 88.6")
        self.set_sink_gain.assert_called_once_with("stereo_front", -65.1)

    def test_active_gain_application_failure_is_explicit_and_does_not_save(self):
        manager = self.prepare()
        record = manager.submit("output_0", self.request("set_gain", gain_db=-4))
        original = self.path.read_bytes()
        self.set_sink_gain.side_effect = RuntimeError("failed")
        manager.run_once()
        result = manager.get(record["id"])
        self.assertEqual(result["error"]["code"], "audio_failed")
        self.assertFalse(result["gain_persisted"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_gain_persistence_and_rollback_failure_are_distinct(self):
        manager = self.prepare()
        for rollback_failed in (False, True):
            request = self.request("set_gain", gain_db=-6, idempotency_key=f"gain-save-key-{rollback_failed}")
            record = manager.submit("output_0", request)
            self.set_sink_gain.side_effect = [None, RuntimeError("rollback")] if rollback_failed else None
            with patch.object(self.api, "_save", side_effect=OSError("read only")):
                manager.run_once()
            result = manager.get(record["id"])
            self.assertEqual(result["error"]["code"], "gain_rollback_failed" if rollback_failed else "gain_save_failed")
            self.assertFalse(result["gain_persisted"])

    def test_worker_restart_keeps_queued_and_marks_running_unknown_without_replay(self):
        manager = self.prepare()
        queued = manager.submit("output_0", self.request())
        interrupted = manager.submit("output_1", self.request())
        manager._update(interrupted, status="running", phase="intention_persisted", requested_intention_persisted=True)
        manager.close()
        restarted = operations.OperationManager(self.api, manager.path)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(queued["id"])["status"], "queued")
        result = restarted.get(interrupted["id"])
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertTrue(result["requested_intention_persisted"])
        self.assertIsNone(result["applied"])
        self.assertEqual(result["recovery_observation"]["observed_state"], "play")
        self.client.stop.assert_not_called()

    def test_only_one_worker_can_own_ledger(self):
        manager = self.prepare()
        with self.assertRaises(RuntimeError):
            operations.OperationManager(self.api, manager.path)

    def test_background_worker_executes_and_stops_cleanly(self):
        manager = self.prepare()
        self.client.status.return_value = {"state": "stop"}
        manager.start()
        record = manager.submit("output_0", self.request())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and manager.get(record["id"])["status"] not in operations.TERMINAL:
            time.sleep(0.01)
        self.assertEqual(manager.get(record["id"])["status"], "applied")
        manager.close()
        self.assertFalse(manager._thread.is_alive())

    def test_retention_and_ledger_bound_do_not_forget_fresh_idempotency_keys(self):
        manager = self.prepare(ledger_limit=1)
        record = manager.submit("output_0", self.request())
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        with self.assertRaises(operations.OperationError) as caught:
            manager.submit("output_1", self.request())
        self.assertEqual(caught.exception.status, 429)
        with patch.object(operations.time, "time", return_value=record["created_at"] + 86401):
            manager.submit("output_1", self.request())
        with self.assertRaises(operations.OperationError) as caught:
            manager.get(record["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_http_authenticated_commands_and_lookup_and_read_only_default(self):
        manager = self.prepare()
        observer = agent.AgentObserver(self.path, backend=self.api, controls_enabled=True)
        self.addCleanup(observer.close)
        client = agent.create_app(observer, "musicpi", "x" * 32, manager).test_client()
        auth = {"Authorization": "Bearer " + "x" * 32}
        route = "/v1/outputs/output_0/operations"
        self.assertEqual(client.post(route, json=self.request()).status_code, 401)
        response = client.post(route, json=self.request(), headers=auth)
        self.assertEqual(response.status_code, 202)
        record = response.json["operation"]
        self.assertEqual(client.get("/v1/operations/" + record["id"], headers=auth).json["operation"], record)
        self.assertEqual(client.get("/v1/capabilities", headers=auth).json["write_operations"], list(operations.ACTIONS))
        self.assertEqual(client.post(route, data="{}", headers=auth).status_code, 400)
        self.assertEqual(client.post(route, data="x" * 3000, content_type="application/json", headers=auth).status_code, 413)
        observer.tick()
        output = client.get("/v1/outputs/output_0", headers=auth).json["output"]
        self.assertEqual(output["control_revision"], self.api.control_revision())
        self.assertEqual(output["control"]["mode"], "manual")
        readonly = agent.create_app(observer, "musicpi", "x" * 32).test_client()
        self.assertEqual(readonly.post(route, json=self.request(), headers=auth).status_code, 404)

    def test_web_to_agent_http_operation_and_completion_end_to_end(self):
        manager = self.prepare()
        observer = agent.AgentObserver(self.path, backend=self.api, controls_enabled=True)
        self.addCleanup(observer.close)
        observer.tick()
        app = agent.create_app(observer, "musicpi", "x" * 32, manager)
        server = make_server("127.0.0.1", 0, app)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        token = self.path.parent / "token"
        token.write_text("x" * 32)
        client = PlaybackStatusClient(PlaybackAgentConfig(
            f"http://127.0.0.1:{server.server_port}", "musicpi", str(token),
        ))
        self.addCleanup(client.close)
        self.assertTrue(client.poll_once())
        web = RussoundHTTPServer(None, None, playback_clients=[client]).app.test_client()
        reply = web.post("/api/playback/agents/musicpi/outputs/output_0/operations", json=self.request())
        self.assertEqual(reply.status_code, 202)
        operation_id = reply.json["operation"]["id"]
        self.client.status.return_value = {"state": "stop"}
        manager.run_once()
        completed = web.get("/api/playback/agents/musicpi/operations/" + operation_id)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json["operation"]["status"], "applied")
        self.assertTrue(completed.json["operation"]["requested_intention_persisted"])
        self.assertEqual(json.loads(self.api.state_path.read_text()), {"Radio 88.6": "stop"})
