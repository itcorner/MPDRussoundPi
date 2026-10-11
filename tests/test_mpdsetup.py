import importlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "mpdrussoundpi" / "russound"
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    # Unit generation does not use an MPD protocol connection.
    with patch("logging.basicConfig"), patch.dict(sys.modules, {"mpd": Mock()}):
        mpdsetup = importlib.import_module("mpdsetup")
        stream = importlib.import_module("lib.stream")


class MPDSetupTests(unittest.TestCase):
    def run_cli(self, option, home, runtime, run, config="config.json"):
        with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
            with patch.object(sys, "argv", ["mpdsetup.py", option, config]):
                with patch.dict(os.environ, {"HOME": home, "XDG_RUNTIME_DIR": runtime}):
                    with patch("logging.basicConfig"), patch.dict(sys.modules, {"mpd": Mock()}):
                        with patch("subprocess.run", side_effect=run), patch("time.sleep"):
                            runpy.run_path(str(SCRIPTS_DIR / "mpdsetup.py"), run_name="__main__")

    def test_mpd_lock_contention_release_and_audio_independence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    inode = (Path(directory) / "mpd_setup.lock").stat().st_ino
                    with self.assertRaises(SystemExit):
                        with mpdsetup.setup_lock("mpd_setup.lock"):
                            self.fail("Contended MPD lock acquired")
                    with mpdsetup.setup_lock():
                        pass
                with self.assertRaises(ValueError):
                    with mpdsetup.setup_lock("mpd_setup.lock"):
                        raise ValueError("failure")
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    self.assertEqual((Path(directory) / "mpd_setup.lock").stat().st_ino, inode)

    def test_install_holds_lock_through_reload_failure(self):
        def reload(command, **kwargs):
            self.assertEqual(command, ["systemctl", "--user", "daemon-reload"])
            self.assertTrue(kwargs["check"])
            with self.assertRaises(SystemExit):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    self.fail("Reload was outside lock")
            raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as runtime:
            config = Path(home) / "config.json"
            config.write_text(json.dumps({"streams": []}))
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_cli("--install", home, runtime, reload, str(config))
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    pass

    def test_invalid_config_releases_install_lock(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as runtime:
            with patch.object(mpdsetup.readconfig, "read_and_parse_json", return_value=None):
                with self.assertRaises(SystemExit) as error:
                    self.run_cli("--install", home, runtime, Mock())
                self.assertEqual(error.exception.code, 1)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    pass

    def test_cleanup_rejects_concurrent_install_before_commands(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as runtime:
            run = Mock()
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    with self.assertRaises(SystemExit) as error:
                        self.run_cli("--cleanup", home, runtime, run)
                    self.assertEqual(error.exception.code, 1)
            run.assert_not_called()

    def test_cleanup_holds_lock_and_releases_on_exit(self):
        def command(*args, **kwargs):
            with self.assertRaises(SystemExit):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    self.fail("Cleanup was outside lock")
            return Mock(returncode=0)

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as runtime:
            with self.assertRaises(SystemExit) as error:
                self.run_cli("--cleanup", home, runtime, command)
            self.assertEqual(error.exception.code, 0)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                with mpdsetup.setup_lock("mpd_setup.lock"):
                    pass

    def test_meta_service_names_and_orders_configured_instances(self):
        instances = [
            mpdsetup.MPDInstance(
                name, "127.0.0.1", port, stream.Stream("https://example.com/radio", name)
            )
            for name, port in [("Radio 88.6", 6601), ("FM4 Radio", 6602), ("OE3 Radio", 6603)]
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mpdsetup, "SYSTEMD_USER_CONFIG_DIR", directory):
                mpdsetup.create_mpd_metaservice(instances)
            config = (Path(directory) / mpdsetup.SYSTEMD_MPD_METASERVICE).read_text()
        names = "mpd_radio_88.6.service mpd_fm4_radio.service mpd_oe3_radio.service"
        self.assertIn(f"Wants={names}\n", config)
        self.assertIn(f"After={names}\n", config)
        self.assertNotIn("*", config)
        self.assertNotIn("mpd_meta_control.service", config)
        self.assertIn("RemainAfterExit=yes", config)
        self.assertIn("WantedBy=default.target", config)

    def test_empty_meta_service_has_no_instance_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mpdsetup, "SYSTEMD_USER_CONFIG_DIR", directory):
                mpdsetup.create_mpd_metaservice([])
            config = (Path(directory) / mpdsetup.SYSTEMD_MPD_METASERVICE).read_text()
        self.assertNotIn("Wants=", config)
        self.assertNotIn("*", config)

    def test_instance_and_supervisor_units_include_headless_recovery(self):
        instance = mpdsetup.MPDInstance(
            "Radio 88.6", "127.0.0.1", 6601, stream.Stream("https://example.com", "Radio")
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mpdsetup, "SYSTEMD_USER_CONFIG_DIR", directory):
                mpdsetup.create_mpd_systemd_service(instance, "/home/user/audio config.json")
                mpdsetup.create_supervisor_service("/home/user/audio config.json")
                available = mpdsetup.get_available_systemd_mpd_services()
            unit = (Path(directory) / "mpd_radio_88.6.service").read_text()
            supervisor = (Path(directory) / mpdsetup.SYSTEMD_MPD_SUPERVISOR).read_text()
        for dependency in ("pipewire.service", "pipewire-pulse.service", "wireplumber.service"):
            self.assertIn(dependency, unit)
            self.assertIn(dependency, supervisor)
        self.assertIn("ExecStartPre=", unit)
        self.assertIn(mpdsetup.systemd_quote(str(Path("/home/user/audio config.json").resolve())), unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5", unit)
        self.assertIn("StartLimitBurst=10", unit)
        self.assertIn("mpdsupervisor.py", supervisor)
        self.assertEqual(available, ["mpd_radio_88.6.service"])

    def test_output_generator_uses_software_mixer_for_startup_gate(self):
        config = json.loads((SCRIPTS_DIR / "config.json").read_text())
        sinks = {
            entry["name"]: mpdsetup.sink.config2sink(entry)
            for device in config["outputmappers"]["audio_devices"]
            for entry in device["sinks"]
        }
        for entry in config["streams"]:
            instance = mpdsetup.config2mpd(entry, sinks)
            self.assertIsNotNone(instance)
            assert instance is not None
            self.assertIn('mixer_type "software"', instance.to_mpd_config())

    def test_systemd_arguments_escape_specifiers_variables_and_quotes(self):
        self.assertEqual(mpdsetup.systemd_quote('a%$"b'), '"a%%$$\\"b"')
        with self.assertRaises(ValueError):
            mpdsetup.systemd_quote("a\nb")
