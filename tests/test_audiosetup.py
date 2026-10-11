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
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for name, filename in (
            ("PULSE_CONFIG", "default.pa"), ("PULSE_FRAGMENT", "managed.pa"),
            ("PIPEWIRE_CONFIG", "remapped-sinks.conf"), ("WIREPLUMBER_CONFIG", "card-modes.conf"),
            ("PULSE_SYSTEM_CONFIG", "system-default.pa"),
        ):
            patcher = patch.object(audiosetup, name, str(self.directory / filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        Path(audiosetup.PULSE_SYSTEM_CONFIG).write_text("load-module module-udev-detect\n")

    def test_cleanup_cli_is_wired_without_config_or_running_audio(self):
        Path(audiosetup.PIPEWIRE_CONFIG).write_text(audiosetup.MANAGED_HEADER + "context.modules = []\n")
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}):
            with patch.object(audiosetup, "detect_audio_processor") as detect:
                with patch.object(audiosetup.subprocess, "run") as run:
                    audiosetup.main(["--cleanup"])
        self.assertFalse(Path(audiosetup.PIPEWIRE_CONFIG).exists())
        detect.assert_not_called()
        run.assert_called_once_with(
            ["systemctl", "--user", "try-restart", "pipewire", "wireplumber", "pipewire-pulse"],
            check=True,
        )

    def test_pipewire_install_cleanup_reinstall_preserves_unrelated_files(self):
        unrelated = self.directory / "custom.conf"
        unrelated.write_text("custom settings")
        mpd = self.directory / "mpd_custom.conf"
        mpd.write_text("custom MPD")
        with patch.object(audiosetup.subprocess, "run") as run:
            for _ in range(2):
                audiosetup.create_pipewire_config({})
                audiosetup.create_wireplumber_config({})
                audiosetup.cleanup_configs("pipewire")
                audiosetup.cleanup_configs("pipewire")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(unrelated.read_text(), "custom settings")
        self.assertEqual(mpd.read_text(), "custom MPD")

    def test_cleanup_preflights_every_file_before_deleting_anything(self):
        Path(audiosetup.PIPEWIRE_CONFIG).write_text(audiosetup.MANAGED_HEADER)
        Path(audiosetup.WIREPLUMBER_CONFIG).write_text("unrelated configuration")
        with patch.object(audiosetup.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unowned"):
                audiosetup.cleanup_configs()
        self.assertTrue(Path(audiosetup.PIPEWIRE_CONFIG).exists())
        self.assertEqual(Path(audiosetup.WIREPLUMBER_CONFIG).read_text(), "unrelated configuration")
        run.assert_not_called()

    def test_symlinks_are_never_followed_or_removed(self):
        target = self.directory / "unrelated"
        target.write_text(audiosetup.MANAGED_HEADER)
        Path(audiosetup.PIPEWIRE_CONFIG).symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            audiosetup.cleanup_configs("pipewire")
        self.assertTrue(Path(audiosetup.PIPEWIRE_CONFIG).is_symlink())
        self.assertTrue(target.exists())

    def test_pulse_install_and_cleanup_preserve_original_bytes_and_do_not_duplicate(self):
        original = b"# Custom settings\r\nload-module module-null-sink sink_name=custom"
        path = Path(audiosetup.PULSE_CONFIG)
        path.write_bytes(original)
        for _ in range(2):
            audiosetup.create_pulse_config({})
        self.assertEqual(path.read_bytes(), original + audiosetup.pulse_include().encode())
        with patch.object(audiosetup.subprocess, "run") as run:
            audiosetup.cleanup_configs("pulseaudio")
            audiosetup.cleanup_configs("pulseaudio")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(Path(audiosetup.PULSE_FRAGMENT).exists())
        run.assert_called_once_with(["systemctl", "--user", "try-restart", "pulseaudio"], check=True)

    def test_new_pulse_wrapper_is_removed_but_later_custom_additions_are_kept(self):
        with patch.object(audiosetup.subprocess, "run"):
            audiosetup.create_pulse_config({})
            self.assertIn(".include /etc/pulse/default.pa", Path(audiosetup.PULSE_CONFIG).read_text())
            audiosetup.cleanup_configs("pulseaudio")
            self.assertFalse(Path(audiosetup.PULSE_CONFIG).exists())
            audiosetup.create_pulse_config({})
            with open(audiosetup.PULSE_CONFIG, "a") as output:
                output.write("# new user setting\n")
            audiosetup.cleanup_configs("pulseaudio")
        self.assertEqual(Path(audiosetup.PULSE_CONFIG).read_text(), audiosetup.PULSE_WRAPPER + "# new user setting\n")

    def test_legacy_pulse_default_is_preserved_and_requires_explicit_migration(self):
        path = Path(audiosetup.PULSE_CONFIG)
        original = audiosetup.MANAGED_HEADER + "# may contain user changes\n"
        path.write_text(original)
        with patch.object(audiosetup.subprocess, "run") as run:
            for action in (lambda: audiosetup.create_pulse_config({}), audiosetup.cleanup_configs):
                with self.assertRaisesRegex(ValueError, "Legacy generated"):
                    action()
        self.assertEqual(path.read_text(), original)
        run.assert_not_called()

    def test_malformed_pulse_include_is_not_removed(self):
        path = Path(audiosetup.PULSE_CONFIG)
        path.write_text("# BEGIN MPDRussoundPi managed include\ncustom\n")
        with self.assertRaisesRegex(ValueError, "malformed"):
            audiosetup.cleanup_configs("pulseaudio")
        self.assertTrue(path.exists())

    def test_selected_backend_does_not_touch_other_backend_or_unowned_default(self):
        Path(audiosetup.PIPEWIRE_CONFIG).write_text(audiosetup.MANAGED_HEADER)
        Path(audiosetup.PULSE_CONFIG).write_text(audiosetup.MANAGED_HEADER + "legacy")
        with patch.object(audiosetup.subprocess, "run"):
            audiosetup.cleanup_configs("pipewire")
        self.assertTrue(Path(audiosetup.PULSE_CONFIG).exists())

    def test_cleanup_restart_failure_is_nonzero_and_releases_lock(self):
        Path(audiosetup.PIPEWIRE_CONFIG).write_text(audiosetup.MANAGED_HEADER)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}):
            with patch.object(audiosetup.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "systemctl")):
                with self.assertRaises(SystemExit) as error:
                    audiosetup.main(["--cleanup", "--audio-processor", "pipewire"])
            self.assertEqual(error.exception.code, 1)
            with audiosetup.setup_lock():
                pass

    def test_install_and_cleanup_are_exclusive_and_install_requires_config(self):
        for argv in (["--install", "--cleanup"], ["--install"], []):
            with self.assertRaises(SystemExit) as error:
                audiosetup.main(argv)
            self.assertEqual(error.exception.code, 2)

    def test_cleanup_lock_contention_prevents_deletion(self):
        path = Path(audiosetup.PIPEWIRE_CONFIG)
        path.write_text(audiosetup.MANAGED_HEADER)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}):
            with audiosetup.setup_lock():
                with self.assertRaises(SystemExit):
                    audiosetup.main(["--cleanup"])
        self.assertTrue(path.exists())

    def test_cli_pipewire_install_cleanup_reinstall_checks_restart_and_readiness(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}):
            with patch.object(audiosetup, "detect_audio_processor", return_value={"pipewire": True, "pulseaudio": False}):
                with patch.object(audiosetup, "wait_for_audio") as wait:
                    with patch.object(audiosetup.subprocess, "run") as run:
                        for _ in range(2):
                            audiosetup.main(["--install", str(SCRIPTS_DIR / "config.json")])
                            audiosetup.main(["--cleanup", "--audio-processor", "pipewire"])
        self.assertEqual(wait.call_count, 2)
        self.assertEqual([call.args[0][2] for call in run.call_args_list],
                         ["restart", "try-restart", "restart", "try-restart"])
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_install_refuses_unowned_pipewire_file_before_writing_wireplumber(self):
        Path(audiosetup.PIPEWIRE_CONFIG).write_text("custom modules")
        with patch.object(audiosetup, "detect_audio_processor", return_value={"pipewire": True, "pulseaudio": False}):
            with self.assertRaisesRegex(ValueError, "unowned"):
                audiosetup.install(str(SCRIPTS_DIR / "config.json"), "pipewire")
        self.assertFalse(Path(audiosetup.WIREPLUMBER_CONFIG).exists())

    def test_cold_pulse_install_requires_system_defaults(self):
        Path(audiosetup.PULSE_SYSTEM_CONFIG).unlink()
        with self.assertRaisesRegex(ValueError, "System PulseAudio"):
            audiosetup.create_pulse_config({})
        self.assertFalse(Path(audiosetup.PULSE_CONFIG).exists())
        self.assertFalse(Path(audiosetup.PULSE_FRAGMENT).exists())

    def test_pulse_default_update_failure_preserves_original_and_removes_temp(self):
        path = Path(audiosetup.PULSE_CONFIG)
        path.write_bytes(b"# user configuration\n")
        path.chmod(0o640)
        before = sorted(entry.name for entry in self.directory.iterdir())
        with patch.object(audiosetup.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                audiosetup.write_pulse_default("# replacement\n")
        self.assertEqual(path.read_bytes(), b"# user configuration\n")
        self.assertEqual(before, sorted(entry.name for entry in self.directory.iterdir()))
        audiosetup.write_pulse_default("# replacement\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_pulse_install_checks_restart_and_readiness(self):
        with patch.object(audiosetup, "detect_audio_processor", return_value={"pipewire": False, "pulseaudio": True}):
            with patch.object(audiosetup.subprocess, "run") as run:
                with patch.object(audiosetup, "wait_for_audio") as wait:
                    audiosetup.install(str(SCRIPTS_DIR / "config.json"), "pulseaudio")
        run.assert_called_once_with(["systemctl", "--user", "restart", "pulseaudio"], check=True)
        wait.assert_called_once_with(str(SCRIPTS_DIR / "config.json"))

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
