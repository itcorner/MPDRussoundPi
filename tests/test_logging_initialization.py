import itertools
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "mpdrussoundpi/russound"


class LoggingInitializationTests(unittest.TestCase):
    def run_isolated(self, code, *arguments):
        with tempfile.TemporaryDirectory(prefix="russound-logging-") as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", code, str(SCRIPTS), *arguments],
                cwd=directory, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((Path(directory) / "russound_debugging.log").exists())
            return result

    def test_audio_imports_never_configure_root_logging_in_any_order(self):
        code = """
import importlib, logging, sys
sys.path.insert(0, sys.argv[1])
root = logging.getLogger()
before = (root.level, tuple(root.handlers))
for name in sys.argv[2:]:
    importlib.import_module(name)
    assert (root.level, tuple(root.handlers)) == before, name
"""
        for modules in itertools.permutations(("lib.audiodevice", "audiosetup", "mpdsetup")):
            with self.subTest(order=modules):
                self.run_isolated(code, *modules)

    def test_setup_commands_explicitly_choose_level_and_stderr_without_duplicates(self):
        code = """
from contextlib import nullcontext
import logging, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
logging.basicConfig(level=logging.CRITICAL)
if sys.argv[2] == 'audio':
    import audiosetup
    audiosetup.setup_lock = lambda: nullcontext()
    audiosetup.cleanup_configs = lambda processor: None
    audiosetup.main(['--cleanup', *sys.argv[3:]])
else:
    sys.argv = ['mpdsetup.py', 'unused.json', *sys.argv[3:]]
    runpy.run_path(str(Path(__import__('mpdsetup').__file__)), run_name='__main__')
logging.debug('diagnostic-marker')
logging.info('operational-marker')
logging.warning('warning-marker')
"""
        for command in ("audio", "mpd"):
            for debug in (False, True):
                with self.subTest(command=command, debug=debug):
                    result = self.run_isolated(code, command, *(["--debug"] if debug else []))
                    self.assertEqual(result.stderr.count("diagnostic-marker"), int(debug))
                    self.assertEqual(result.stderr.count("operational-marker"), 1)
                    self.assertEqual(result.stderr.count("warning-marker"), 1)
                    self.assertEqual(result.stdout, "")

    def test_help_does_not_create_logfiles_or_initialize_logging(self):
        code = """
import runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
script = Path(sys.argv[1]) / (sys.argv[2] + '.py')
sys.argv = [str(script), '--help']
runpy.run_path(str(script), run_name='__main__')
"""
        for command in ("audiosetup", "mpdsetup"):
            with self.subTest(command=command):
                result = self.run_isolated(code, command)
                self.assertIn("--debug", result.stdout)
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
