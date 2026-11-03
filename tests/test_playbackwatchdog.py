"""Stall watchdog unit tests with a fake clock and scripted probes."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    watchdog_module = importlib.import_module("lib.playbackwatchdog")
    outputconfig = importlib.import_module("lib.outputconfig")

ProbeResult = watchdog_module.ProbeResult
PLAYING = ProbeResult(True, "play", 1.0, "7")
UNREACHABLE = ProbeResult(False)


class Output:
    def __init__(self, identity, intention="play"):
        self.id = identity
        self.intention = intention


class Harness:
    def __init__(self, state_path=None, **overrides):
        self.now = 0.0
        self.results = {}
        self.probes = []
        self.restarts = []
        self.restart_error = None
        self.escalations = []
        self.audio_error = None
        self.watchdog = watchdog_module.StallWatchdog(
            outputconfig.WatchdogConfig(**overrides),
            probe=self._probe, restart=self._restart, escalate=self.escalations.append,
            audio_gate=self._gate, state_path=state_path, clock=lambda: self.now,
        )

    def _probe(self, output):
        self.probes.append((self.now, output.id))
        # Healthy default: playing with elapsed advancing along the fake clock.
        return self.results.get(output.id, ProbeResult(True, "play", self.now, "7"))

    def _restart(self, identity):
        if self.restart_error is not None:
            raise self.restart_error
        self.restarts.append((self.now, identity))

    def _gate(self):
        if self.audio_error is not None:
            raise self.audio_error

    def run(self, outputs, seconds, step=2.0):
        """Advance the fake clock like the supervisor loop; collect restarted IDs."""
        restarted = []
        end = self.now + seconds
        while self.now < end:
            restarted.extend(self.watchdog.tick(outputs))
            self.now += step
        return restarted


class StallWatchdogTests(unittest.TestCase):
    def test_unresponsive_output_is_restarted_once_and_probing_pauses_for_grace(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        with self.assertLogs(level="WARNING"):
            restarted = harness.run([Output("front")], 130)
        self.assertEqual(restarted, ["front"])
        self.assertEqual(len(harness.restarts), 1)
        self.assertGreaterEqual(harness.restarts[0][0], 120)
        probed_after = [at for at, _ in harness.probes if at > harness.restarts[0][0]]
        self.assertFalse([at for at in probed_after if at < harness.restarts[0][0] + 90])

    def test_frozen_elapsed_with_play_state_is_restarted(self):
        harness = Harness()
        harness.results["front"] = ProbeResult(True, "play", 42.0, "7")
        with self.assertLogs(level="WARNING") as logs:
            restarted = harness.run([Output("front")], 130)
        self.assertEqual(restarted, ["front"])
        self.assertIn("frozen", "\n".join(logs.output))

    def test_advancing_elapsed_and_song_changes_reset_the_baseline(self):
        harness = Harness()
        outputs = [Output("front")]
        for _ in range(60):  # 10 minutes of healthy playback
            harness.results["front"] = ProbeResult(True, "play", harness.now, "7")
            self.assertEqual(harness.watchdog.tick(outputs), ())
            harness.now += 10
        frozen = ProbeResult(True, "play", 42.0, "7")
        harness.results["front"] = frozen
        self.assertEqual(harness.run(outputs, 110), [])
        # A station change (new song ID) resets the frozen baseline.
        harness.results["front"] = ProbeResult(True, "play", 42.0, "8")
        self.assertEqual(harness.run(outputs, 110), [])
        self.assertEqual(harness.restarts, [])

    def test_paused_and_stopped_intentions_are_never_probed_or_restarted(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        harness.results["rear"] = UNREACHABLE
        restarted = harness.run([Output("front", "pause"), Output("rear", "stop")], 600)
        self.assertEqual(restarted, [])
        self.assertEqual(harness.probes, [])
        self.assertEqual(harness.restarts, [])

    def test_stopped_player_with_play_intention_is_left_to_the_reconciler(self):
        harness = Harness()
        harness.results["front"] = ProbeResult(True, "stop", None, None)
        self.assertEqual(harness.run([Output("front")], 600), [])
        self.assertEqual(harness.restarts, [])

    def test_rate_limit_then_escalation_restarts_audio_stack_and_all_outputs(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        outputs = [Output("front"), Output("rear")]
        with self.assertLogs(level="WARNING"):
            restarted = harness.run(outputs, 800)
        self.assertEqual([identity for _, identity in harness.restarts], ["front"] * 3)
        self.assertEqual(harness.escalations, [("front", "rear")])
        # The escalation restarts every output, so the supervisor clears all backoffs.
        self.assertEqual(restarted, ["front", "front", "front", "front", "rear"])

    def test_second_escalation_within_window_holds_and_logs_once(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        outputs = [Output("front")]
        with self.assertLogs(level="WARNING"):
            harness.run(outputs, 800)  # three restarts, then the escalation
        self.assertEqual(len(harness.escalations), 1)
        with self.assertLogs(level="ERROR") as logs:
            harness.run(outputs, 1600)
        self.assertEqual(len(harness.escalations), 1)
        self.assertEqual(len([line for line in logs.output if "holding" in line]), 1)

    def test_restart_is_deferred_while_audio_is_not_ready_and_fires_when_back(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        harness.audio_error = RuntimeError("USB card missing")
        outputs = [Output("front")]
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(harness.run(outputs, 600), [])
        self.assertEqual(harness.restarts, [])
        self.assertEqual(len([line for line in logs.output if "deferred" in line]), 1)
        harness.audio_error = None
        harness.now += 10  # past the gate retry backoff
        with self.assertLogs(level="ERROR"):
            self.assertEqual(harness.watchdog.tick(outputs), ("front",))
        self.assertEqual(len(harness.restarts), 1)

    def test_failed_restart_backs_off_then_is_retried(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        harness.restart_error = subprocess.SubprocessError("systemctl failed")
        outputs = [Output("front")]
        with self.assertLogs(level="ERROR"):
            self.assertEqual(harness.run(outputs, 130), [])
        self.assertEqual(harness.escalations, [])
        harness.restart_error = None
        harness.now += 90  # past the failed-restart backoff
        with self.assertLogs(level="ERROR"):
            self.assertEqual(harness.watchdog.tick(outputs), ("front",))
        self.assertEqual(len(harness.restarts), 1)

    def test_disabled_watchdog_never_probes(self):
        harness = Harness(enabled=False)
        harness.results["front"] = UNREACHABLE
        self.assertEqual(harness.run([Output("front")], 600), [])
        self.assertEqual(harness.probes, [])

    def test_intention_change_clears_a_latched_stall(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        harness.audio_error = RuntimeError("not ready")
        with self.assertLogs(level="WARNING"):
            harness.run([Output("front")], 130)  # latched, deferred
        self.assertEqual(harness.watchdog.tick([Output("front", "stop")]), ())
        harness.audio_error = None
        self.assertEqual(harness.watchdog.tick([Output("front", "stop")]), ())
        self.assertEqual(harness.restarts, [])

    def test_state_file_records_restarts_deferrals_and_escalations(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "watchdog.json"
            harness = Harness(state_path=state_path)
            harness.results["front"] = UNREACHABLE
            with self.assertLogs(level="WARNING"):
                harness.run([Output("front")], 800)
            value = json.loads(state_path.read_text())
            front = value["outputs"]["front"]
            self.assertEqual(len(front["restarts"]), 3)
            self.assertEqual(front["restarts"][0]["reason"], "unresponsive")
            self.assertIn("at", front["restarts"][0])
            self.assertIsNone(front["deferred"])
            self.assertEqual(value["escalations"][0]["output"], "front")

    def test_removed_outputs_are_forgotten(self):
        harness = Harness()
        harness.results["front"] = UNREACHABLE
        harness.watchdog.tick([Output("front")])
        harness.watchdog.tick([Output("rear")])
        self.assertEqual(set(harness.watchdog._tracking), {"rear"})


if __name__ == "__main__":
    unittest.main()
