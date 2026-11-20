import importlib
from unittest.mock import mock_open
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound"
ownership = importlib.import_module("mpdrussoundpi.russound.lib.mpdownership")


class MPDOwnershipTests(unittest.TestCase):
    def test_owned_file_reads_only_the_marker_and_rejects_unsafe_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed.service"
            path.write_text(ownership.MARKER + ("x" * 100_000))
            opener = mock_open(read_data=ownership.MARKER.encode())
            with patch.object(Path, "open", opener):
                ownership.owned_file(path)
            opener().read.assert_called_once_with(len(ownership.MARKER.encode()))

            unsafe = Path(directory) / "unsafe"
            unsafe.mkdir()
            unsafe.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "Unsafe MPD directory"):
                ownership.owned_directory(unsafe)

    def test_exact_configured_units_and_legacy_overrides_fail_before_mutation(self):
        config = SCRIPTS / "config.json"
        names = ownership.configured_units(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit_root, config_root = root / "units", root / "config"
            unit_root.mkdir()
            config_root.mkdir()
            for name in names:
                (unit_root / name).write_text(ownership.MARKER + "[Unit]\n")
                (config_root / (Path(name).stem + ".conf")).write_text(ownership.MARKER)
            unrelated = unit_root / "mpd_unrelated.service"
            unrelated.write_text("[Unit]\nDescription=Do not touch\n")

            def show(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0,
                    f"LoadState=loaded\nFragmentPath={unit_root / argv[-1]}\nDropInPaths=\n", "")

            with patch.object(ownership.subprocess, "run", side_effect=show):
                self.assertEqual(ownership.preflight(config, unit_root, config_root, controls=False), names)
                self.assertEqual(unrelated.read_text(), "[Unit]\nDescription=Do not touch\n")
                first = unit_root / names[0]
                first.write_text("[Unit]\n")
                with self.assertRaisesRegex(ValueError, "legacy"):
                    ownership.preflight(config, unit_root, config_root, controls=False)
                first.write_text(ownership.MARKER)
                dropin = unit_root / (names[0] + ".d")
                dropin.mkdir()
                extra = dropin / "override.conf"
                extra.write_text("[Service]\nExecStart=/other/program\n")
                with self.assertRaisesRegex(ValueError, "Unreviewed"):
                    ownership.preflight(config, unit_root, config_root, controls=False)
                extra.unlink()
                owned = dropin / "50-egress.conf"
                owned.write_text(ownership.EGRESS_MARKER)
                self.assertEqual(ownership.preflight(config, unit_root, config_root, controls=False), names)
                owned.unlink()
                first.unlink()
                with self.assertRaisesRegex(ValueError, "absent"):
                    ownership.preflight(config, unit_root, config_root, controls=False)
                first.symlink_to(unrelated)
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    ownership.preflight(config, unit_root, config_root, controls=False)

            for fragment, dropins in (("/etc/systemd/user/other.service", ""), (str(unit_root / names[0]), "/etc/override.conf")):
                (unit_root / names[0]).unlink()
                (unit_root / names[0]).write_text(ownership.MARKER)
                result = subprocess.CompletedProcess([], 0,
                    f"LoadState=loaded\nFragmentPath={fragment}\nDropInPaths={dropins}\n", "")
                with patch.object(ownership.subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(ValueError, "overridden|unreviewed"):
                        ownership.preflight(config, unit_root, config_root, controls=False)

    def test_new_install_requires_absent_or_owned_files_and_verified_systemd_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = subprocess.CompletedProcess([], 1, "LoadState=not-found\nFragmentPath=\nDropInPaths=\n", "")
            with patch.object(ownership.subprocess, "run", return_value=missing):
                ownership.preflight(SCRIPTS / "config.json", root / "units", root / "config", install=True)
            with patch.object(ownership.subprocess, "run", return_value=Mock(returncode=1, stdout="")):
                with self.assertRaisesRegex(ValueError, "Cannot verify"):
                    ownership.preflight(SCRIPTS / "config.json", root / "units", root / "config", install=True)

    def test_cli_action_conflicts_never_call_systemctl_or_nft(self):
        for script, args in (
            ("mpdsetup.py", ["--install", "--cleanup", "config.json"]),
            ("mpdsetup.py", ["--start", "--stop", "config.json"]),
            ("mpdsetup.py", ["--restart", "--enable", "config.json"]),
            ("playbackegress.py", ["--wait", "--user", "musicpi"]),
            ("playbackegress.py", ["--wait", "--resolver", "1.1.1.1"]),
        ):
            with self.subTest(script=script, args=args):
                with patch.object(sys, "argv", [script, *args]), patch.dict(sys.modules, {"mpd": Mock()}):
                    with patch("subprocess.run") as run:
                        with self.assertRaises(SystemExit) as error:
                            runpy.run_path(str(SCRIPTS / script), run_name="__main__")
                        self.assertEqual(error.exception.code, 2)
                        run.assert_not_called()
