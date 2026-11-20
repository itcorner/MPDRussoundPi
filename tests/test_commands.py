from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mpdrussoundpi.russound.lib.commands import installed_command


class InstalledCommandTests(unittest.TestCase):
    def test_returns_the_command_next_to_the_interpreter_without_leaving_the_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "venv/bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "mpd-supervisor").write_text("#!/bin/sh\n")
            # A venv interpreter is a symlink to the system Python; it must not be resolved.
            (bin_dir / "python").symlink_to(sys.executable)
            with patch.object(sys, "executable", str(bin_dir / "python")):
                self.assertEqual(installed_command("mpd-supervisor"), str(bin_dir / "mpd-supervisor"))

    def test_refuses_a_missing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "executable", str(Path(directory) / "python")):
                with self.assertRaisesRegex(RuntimeError, "mpd-supervisor is not installed"):
                    installed_command("mpd-supervisor")


if __name__ == "__main__":
    unittest.main()
