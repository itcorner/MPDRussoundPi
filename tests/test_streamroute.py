import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    routes = importlib.import_module("lib.streamroute")


SINKS = [{"index": 43, "name": "sink.stereo_front"}, {"index": 296, "name": "usb_master"}]


def client(index=253, sink=296, name="pw_stereo_front", application="Music Player Daemon"):
    return {"index": index, "sink": sink, "properties": {
        "application.name": application, "application.process.binary": "mpd",
        "media.name": name, "target.object": "sink.stereo_front",
    }}


class StreamRouteTests(unittest.TestCase):
    def test_actual_route_not_requested_target_determines_correctness(self):
        with patch.object(routes, "_objects", side_effect=[SINKS, [client()]]):
            route = routes.inspect_route("stereo_front")
        self.assertFalse(route.correct)
        self.assertEqual(route.client_index, 253)
        self.assertEqual(route.actual_sink, "usb_master")
        self.assertEqual(route.expected_sink, "sink.stereo_front")

    def test_correct_route_and_unrelated_clients(self):
        with patch.object(routes, "_objects", side_effect=[SINKS, [
            client(sink=43), client(name="pw_stereo_rear"), client(application="Other"),
        ]]):
            self.assertTrue(routes.inspect_route("stereo_front").correct)

    def test_missing_sink_missing_client_and_ambiguity_fail_explicitly(self):
        for sinks, clients, message in (
            (SINKS[1:], [], "virtual sink"),
            (SINKS, [], "No active"),
            (SINKS, [client(), client(index=254)], "Ambiguous"),
            (SINKS, [client(sink=999)], "disappeared"),
            (SINKS, [{"properties": None}], "properties"),
            ([{"index": True, "name": "sink.stereo_front"}], [], "identity"),
            (SINKS, [client(index="253")], "identity"),
        ):
            with self.subTest(message=message):
                with patch.object(routes, "_objects", side_effect=[sinks, clients]):
                    with self.assertRaisesRegex(RuntimeError, message):
                        routes.inspect_route("stereo_front")

    def test_recovery_moves_current_client_then_verifies_twice(self):
        initial = routes.StreamRoute(253, "usb_master", "sink.stereo_front")
        settled = routes.StreamRoute(253, "sink.stereo_front", "sink.stereo_front")
        with patch.object(routes, "inspect_route", side_effect=[initial, settled, settled]):
            with patch.object(routes, "_pactl") as pactl, patch.object(routes.time, "sleep"):
                self.assertEqual(routes.recover_route("stereo_front"), settled)
        pactl.assert_called_once_with("move-sink-input", "253", "sink.stereo_front")

    def test_correct_route_is_idempotent_and_client_recreation_is_rediscovered(self):
        wrong = routes.StreamRoute(900, "usb_master", "sink.stereo_front")
        old = routes.StreamRoute(253, "sink.stereo_front", "sink.stereo_front")
        new = routes.StreamRoute(900, "sink.stereo_front", "sink.stereo_front")
        for snapshots, moves in (([old, old], 0), ([old, wrong, new, new], 1)):
            with self.subTest(moves=moves):
                with patch.object(routes, "inspect_route", side_effect=snapshots):
                    with patch.object(routes, "_pactl") as pactl, patch.object(routes.time, "sleep"):
                        routes.recover_route("stereo_front")
                self.assertEqual(pactl.call_count, moves)
                if moves:
                    pactl.assert_called_once_with("move-sink-input", "900", "sink.stereo_front")

    def test_missing_startup_client_waits_then_settles(self):
        correct = routes.StreamRoute(253, "sink.stereo_front", "sink.stereo_front")
        with patch.object(routes, "inspect_route", side_effect=[
            routes.RouteUnavailable("not ready"), correct, correct,
        ]), patch.object(routes.time, "sleep"):
            self.assertTrue(routes.recover_route("stereo_front").correct)

    def test_ignored_move_timeout_and_command_failure_are_not_success(self):
        wrong = routes.StreamRoute(253, "usb_master", "sink.stereo_front")
        with patch.object(routes, "inspect_route", return_value=wrong):
            with patch.object(routes, "_pactl") as pactl:
                with patch.object(routes.time, "monotonic", side_effect=[0, 6]):
                    with self.assertRaisesRegex(RuntimeError, "Cannot recover"):
                        routes.recover_route("stereo_front")
                pactl.side_effect = RuntimeError("move failed")
                with self.assertRaisesRegex(RuntimeError, "move failed"):
                    routes.recover_route("stereo_front")
        with patch.object(routes, "inspect_route", side_effect=routes.RouteUnavailable("missing sink")):
            with patch.object(routes.time, "monotonic", side_effect=[0, 6]):
                with self.assertRaisesRegex(RuntimeError, "missing sink"):
                    routes.recover_route("stereo_front")

    def test_startup_timeout_waits_for_late_playback_client(self):
        correct = routes.StreamRoute(254, "sink.stereo_front", "sink.stereo_front")
        late = [routes.RouteUnavailable("no client")] * 3 + [correct, correct]
        with patch.object(routes, "inspect_route", side_effect=late):
            with patch.object(routes.time, "monotonic", side_effect=[0, 4, 6, 8, 9]):
                with patch.object(routes.time, "sleep"):
                    self.assertEqual(routes.recover_route("stereo_front", timeout=12), correct)

    def test_ambiguous_clients_never_moved(self):
        with patch.object(routes, "inspect_route", side_effect=RuntimeError("Ambiguous")):
            with patch.object(routes, "_pactl") as pactl:
                with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                    routes.recover_route("stereo_front")
        pactl.assert_not_called()

    def test_pactl_json_validation_and_command_failures(self):
        with patch.object(routes.subprocess, "run") as run:
            run.return_value = Mock(stdout=json.dumps(SINKS))
            self.assertEqual(routes._objects("sinks"), SINKS)
            self.assertEqual(run.call_args.args[0], ["pactl", "--format=json", "list", "sinks"])
            self.assertTrue(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            for value in ("broken", "{}", "[1]"):
                run.return_value.stdout = value
                with self.assertRaises(RuntimeError):
                    routes._objects("sinks")
            run.side_effect = subprocess.CalledProcessError(1, ["pactl"], stderr="server unavailable")
            with self.assertRaisesRegex(RuntimeError, "server unavailable"):
                routes._objects("sinks")
            run.side_effect = subprocess.TimeoutExpired(["pactl"], 2)
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                routes._objects("sinks")
