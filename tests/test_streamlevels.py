import importlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    levels = importlib.import_module("streamlevels")
    gain = importlib.import_module("lib.streamgain")


SUMMARY = """
[info] Summary:
  Integrated loudness:
    I: -18.2 LUFS
    Threshold: -28.2 LUFS
  Loudness range:
    LRA: 4.3 LU
  True peak:
    Peak: -2.5 dBFS
"""
PROGRESS = "out_time_us=900000000\nprogress=end\n"


class StreamLevelTests(unittest.TestCase):
    def test_real_summary_and_duration_parsing(self):
        result = levels.parse_measurement("Radio", SUMMARY, PROGRESS, 900)
        self.assertEqual(result.integrated_lufs, -18.2)
        self.assertEqual(result.loudness_range_lu, 4.3)
        self.assertEqual(result.true_peak_dbtp, -2.5)
        self.assertEqual(result.measured_seconds, 900)

    def test_missing_silent_incomplete_and_corrupt_feeds_rejected(self):
        cases = [
            ("no summary", PROGRESS),
            (SUMMARY.replace("-18.2 LUFS", "-70.0 LUFS"), PROGRESS),
            (SUMMARY.replace("-2.5 dBFS", "-inf dBFS"), PROGRESS),
            (SUMMARY, "out_time_us=10000000\nprogress=end\n"),
            (SUMMARY, "out_time_us=900000000\nprogress=continue\n"),
            (SUMMARY + "\n[error] decoding failed", PROGRESS),
        ]
        for summary, progress in cases:
            with self.subTest(summary=summary, progress=progress), self.assertRaises(ValueError):
                levels.parse_measurement("Radio", summary, progress, 900)

    def test_warnings_are_reported(self):
        result = levels.parse_measurement("Radio", SUMMARY + "\n[warning] timestamp discontinuity", PROGRESS, 900)
        self.assertEqual(result.warnings, ["[warning] timestamp discontinuity"])

    def test_attenuation_matches_quietest_and_obeys_peak_ceiling(self):
        measurements = [
            levels.Measurement("loud", -10, 3, 0.5, 900, []),
            levels.Measurement("quiet", -25, 5, -3, 900, []),
            levels.Measurement("middle", -18, 4, -1, 900, []),
        ]
        target, gains = levels.recommend_gains(measurements, -1)
        self.assertEqual(target, -25)
        self.assertEqual(gains, {"loud": -15, "quiet": 0, "middle": -7})
        measurements[1] = levels.Measurement("quiet", -25, 5, 0, 900, [])
        target, gains = levels.recommend_gains(measurements, -1)
        self.assertEqual(target, -26)
        for item in measurements:
            self.assertLessEqual(gains[item.name], 0)
            self.assertAlmostEqual(item.integrated_lufs + gains[item.name], target)
            self.assertLessEqual(item.true_peak_dbtp + gains[item.name], -1)

    def test_rounding_never_boosts_or_exceeds_target(self):
        measurements = [
            levels.Measurement("a", -17.25, 1, -2, 900, []),
            levels.Measurement("b", -18.01, 1, -2, 900, []),
        ]
        target, gains = levels.recommend_gains(measurements, -1)
        self.assertEqual(gains["a"], -0.8)
        self.assertLessEqual(measurements[0].integrated_lufs + gains["a"], target)

    def test_pactl_uses_db_and_reports_failures(self):
        with patch.object(gain.subprocess, "run") as run:
            run.return_value.stdout = "Volume: front-left: 51264 / 78% / -6.40 dB, front-right: 51264 / 78% / -6.40 dB"
            gain.set_sink_gain("stereo_front", -6.4)
            self.assertEqual(run.call_args_list[0].args[0], ["pactl", "set-sink-volume", "sink.stereo_front", "51264"])
            self.assertTrue(run.call_args.kwargs["check"])
            gain.set_sink_gain("stereo_front", -6.4)
            self.assertEqual(run.call_args_list[2].args[0][-1], "51264")
            run.side_effect = subprocess.CalledProcessError(1, ["pactl"], stderr="sink missing")
            with self.assertRaisesRegex(RuntimeError, "sink missing"):
                gain.set_sink_gain("stereo_front", 0)

    def test_gain_retries_until_applied_and_rejects_ignored_commands(self):
        with patch.object(gain.subprocess, "run") as run, patch.object(gain.time, "sleep"):
            run.side_effect = [
                Mock(), Mock(stdout="Volume: front-left: 65536 / 100% / 0.00 dB"),
                Mock(), Mock(stdout="Volume: front-left: 51264 / 78% / -6.40 dB"),
            ]
            gain.set_sink_gain("stereo_front", -6.4)
            self.assertEqual(run.call_count, 4)
            run.side_effect = None
            run.return_value.stdout = "Volume: front-left: 65536 / 100% / 0.00 dB"
            with patch.object(gain.time, "monotonic", side_effect=[0, 6]):
                with self.assertRaisesRegex(RuntimeError, "did not take effect"):
                    gain.set_sink_gain("stereo_front", -6.4)
            run.side_effect = subprocess.TimeoutExpired(["pactl"], 10)
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                gain.set_sink_gain("stereo_front", 0)

    def test_measurements_start_concurrently_and_clean_up_on_failure(self):
        processes = []

        def launch(command, stdout, stderr):
            process = Mock(returncode=0)
            process.poll.return_value = 0
            process.wait.side_effect = lambda **kwargs: self.assertEqual(len(processes), 2)
            stdout.write(PROGRESS)
            stderr.write(SUMMARY)
            processes.append(process)
            self.assertIn("ebur128=peak=true:framelog=verbose", command[command.index("-af") + 1])
            self.assertIn("-xerror", command)
            return process

        streams = [{"name": name, "stream": "https://example.com/radio"} for name in ("a", "b")]
        with patch.object(levels.subprocess, "Popen", side_effect=launch):
            self.assertEqual(len(levels.measure_streams(streams, 900, "ffmpeg")), 2)
        processes.clear()

        def timeout_once(command, stdout, stderr):
            process = launch(command, stdout, stderr)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired(command, 960), 0]
            return process

        with patch.object(levels.subprocess, "Popen", side_effect=timeout_once):
            with self.assertRaises(subprocess.TimeoutExpired):
                levels.measure_streams(streams, 900, "ffmpeg")
        for process in processes:
            process.terminate.assert_called_once()

    def test_cli_report_and_new_config_preserve_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.json"
            destination = Path(directory) / "balanced.json"
            original = '{"streams":[{"name":"a","stream":"https://example.com/a","gain_db":-5}],"other":42}'
            source.write_text(original)
            measurement = levels.Measurement("a", -18.2, 4.3, -2.5, 900, [])
            with patch.object(levels.shutil, "which", return_value="ffmpeg"):
                with patch.object(levels, "measure_streams", return_value=[measurement]):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        levels.main([str(source), "--output-config", str(destination)])
                    report = json.loads(stdout.getvalue())
                    self.assertEqual(report["streams"][0]["gain_db"], 0)
                    self.assertEqual(json.loads(destination.read_text())["other"], 42)
                    self.assertEqual(source.read_text(), original)
                    with self.assertRaises(SystemExit):
                        levels.main([str(source), "--output-config", str(source)])
                    with self.assertRaises(SystemExit):
                        levels.main([str(source), "--output-config", str(destination)])

    def test_cli_resolves_tunein_entries_without_stream_url(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.json"
            source.write_text(json.dumps({"streams": [
                {"name": "a", "source_provider": "tunein", "source_station_id": "s8007",
                 "source_station_name": "ORF Hitradio OE3"},
            ]}))
            measurement = levels.Measurement("a", -18.2, 4.3, -2.5, 900, [])
            with patch.object(levels.shutil, "which", return_value="ffmpeg"), \
                    patch.object(levels.TuneInCatalog, "resolve", return_value="https://example.com/oe3") as resolve, \
                    patch.object(levels, "measure_streams", return_value=[measurement]) as measure, \
                    patch("sys.stdout", new_callable=io.StringIO):
                levels.main([str(source)])
            resolve.assert_called_once_with("s8007")
            self.assertEqual(measure.call_args.args[0], [{"name": "a", "stream": "https://example.com/oe3"}])

    def test_cli_invalid_inputs_and_missing_ffmpeg_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.json"
            for config in ({"streams": []}, {"streams": [{"name": "a", "stream": "file:///tmp/a"}]},
                           {"streams": [{"name": "a", "stream": "https://example.com"}] * 2}):
                source.write_text(json.dumps(config))
                with patch.object(levels.shutil, "which", return_value="ffmpeg"):
                    with self.assertRaises(SystemExit) as error:
                        levels.main([str(source)])
                    self.assertEqual(error.exception.code, 1)
            with patch.object(levels.shutil, "which", return_value=None), self.assertRaises(SystemExit):
                levels.main([str(source)])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_ffmpeg_equal_reference_tones_with_known_gain(self):
        results = []
        for name, attenuation in (("unity", 0), ("quiet", -6)):
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info",
                 "-progress", "pipe:1", "-f", "lavfi",
                 "-i", "sine=frequency=997:duration=10:sample_rate=48000",
                 "-af", f"aformat=channel_layouts=stereo,volume={attenuation}dB,ebur128=peak=true:framelog=verbose",
                 "-f", "null", "-"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            results.append(levels.parse_measurement(name, result.stderr, result.stdout, 10))
        self.assertAlmostEqual(results[0].integrated_lufs - results[1].integrated_lufs, 6, delta=0.15)
        self.assertAlmostEqual(results[0].true_peak_dbtp - results[1].true_peak_dbtp, 6, delta=0.15)
        _, gains = levels.recommend_gains(results, -1)
        self.assertAlmostEqual(gains["unity"], -6, delta=0.15)
        self.assertEqual(gains["quiet"], 0)
