import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    with patch("logging.basicConfig"):
        audiosetup = importlib.import_module("audiosetup")


class AudioSetupTests(unittest.TestCase):
    def test_configured_surround_51_routes_all_three_pairs(self):
        config = json.loads((SCRIPTS_DIR / "config.json").read_text())
        devices = config["outputmappers"]["audio_devices"]
        self.assertEqual(len(devices), 1)
        device = audiosetup.config2audiodevice(devices[0])
        self.assertEqual(device.card_profile, "analog-surround-51")
        self.assertEqual(
            [item.master_channel_map for item in device.sinks],
            ["[ FL FR ]", "[ FC LFE ]", "[ RL RR ]"],
        )
        generated = device.get_pipewire_sink_config()
        self.assertEqual(generated.count(
            f'target.object = "alsa_output.{device.name}.analog-surround-51"'
        ), 3)
        self.assertNotIn("analog-surround-71", generated)
        self.assertIn('device.profile = "output:analog-surround-51"',
                      device.get_wireplumber_card_config())

    def test_loopback_maps_channels_without_global_layout_conversion(self):
        for output_map in ("[ FL FR ]", "[ FC LFE ]", "[ RL RR ]"):
            with self.subTest(output_map=output_map):
                device = audiosetup.config2audiodevice({
                    "name": "test", "card-profile": "analog-surround-71",
                    "sinks": [{"name": "test", "master_channel_map": output_map,
                               "channel_map": "[ FL FR ]", "remix": "no"}],
                })
                config = device.sinks[0].get_pipewire_config("master")
                before_capture, streams = config.split("capture.props = {", 1)
                capture, playback = streams.split("playback.props = {", 1)
                self.assertNotIn("audio.position", before_capture)
                self.assertIn("audio.position = [ FL FR ]", capture)
                self.assertIn(f"audio.position = {output_map}", playback)
                self.assertIn('target.object = "master"', playback)
                self.assertIn("stream.dont-remix = true", playback)

    def test_setup_lock_rejects_contention_and_keeps_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with audiosetup.setup_lock():
                    inode = (Path(directory) / "audio_setup.lock").stat().st_ino
                    with self.assertRaises(SystemExit) as error:
                        with audiosetup.setup_lock():
                            self.fail("A second setup acquired the lock")
                    self.assertEqual(error.exception.code, 1)
                with audiosetup.setup_lock():
                    self.assertEqual(
                        (Path(directory) / "audio_setup.lock").stat().st_ino, inode
                    )

    def test_setup_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with self.assertRaisesRegex(ValueError, "installation failed"):
                    with audiosetup.setup_lock():
                        raise ValueError("installation failed")
                with audiosetup.setup_lock():
                    pass

    def test_setup_lock_releases_after_exit_or_interrupt(self):
        for error in (SystemExit(1), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                        with self.assertRaises(type(error)):
                            with audiosetup.setup_lock():
                                raise error
                        with audiosetup.setup_lock():
                            pass

    def test_setup_lock_rejects_missing_runtime_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "XDG_RUNTIME_DIR"):
                with audiosetup.setup_lock():
                    self.fail("Missing runtime directory was accepted")

    def test_setup_lock_rejects_public_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with self.assertRaisesRegex(RuntimeError, "mode 0700"):
                    with audiosetup.setup_lock():
                        self.fail("Public runtime directory was accepted")

    def test_setup_lock_releases_after_process_is_killed(self):
        code = (
            "from unittest.mock import patch\n"
            "with patch('logging.basicConfig'):\n"
            "    import audiosetup\n"
            "with audiosetup.setup_lock():\n"
            "    print('locked', flush=True)\n"
            "    import sys\n"
            "    sys.stdin.read()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "XDG_RUNTIME_DIR": directory,
                "PYTHONPATH": str(SCRIPTS_DIR),
            }
            with subprocess.Popen(
                [sys.executable, "-c", code], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            ) as process:
                try:
                    self.assertIsNotNone(process.stdout)
                    assert process.stdout is not None
                    self.assertEqual(process.stdout.readline().strip(), "locked")
                    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                        with self.assertRaises(SystemExit):
                            with audiosetup.setup_lock():
                                self.fail("Child's lock was not held")
                        process.kill()
                        process.wait(timeout=5)
                        with audiosetup.setup_lock():
                            pass
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_wireplumber_config_uses_alsa_monitor_rules(self):
        device = audiosetup.AudioDevice(
            name="usb-0d8c_USB_Sound_Device-00",
            card_profile="analog-surround-71",
            sinks=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "wireplumber.conf.d" / "card-modes.conf"
            with patch.object(audiosetup, "WIREPLUMBER_CONFIG", str(config_path)):
                with patch.object(audiosetup.subprocess, "run") as run:
                    audiosetup.create_wireplumber_config({device.name: device})

            config = config_path.read_text()
            self.assertIn("monitor.alsa.rules = [\n", config)
            self.assertNotIn("context.rules", config)
            self.assertIn(
                'device.name = "alsa_card.usb-0d8c_USB_Sound_Device-00"', config
            )
            self.assertIn('device.profile = "output:analog-surround-71"', config)
            run.assert_not_called()
