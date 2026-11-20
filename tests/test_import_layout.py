"""Guards the single-module-name layout (docs/plans/general_new_package-layout.md)."""

import ast
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mpdrussoundpi" / "russound"
LIBRARY = PACKAGE / "lib"
ENTRY_SCRIPTS = sorted(path.stem for path in PACKAGE.glob("*.py") if path.name != "__init__.py")
LIBRARY_MODULES = sorted(path.stem for path in LIBRARY.glob("*.py") if path.name != "__init__.py")
PYTHON_SOURCES = [
    path for directory in ("mpdrussoundpi", "web", "tool", "tests")
    for path in (ROOT / directory).rglob("*.py") if "__pycache__" not in path.parts
]


def run_python(code_or_args, *, cwd, isolated=True):
    """Run an interpreter that sees only what the test puts on its path."""
    args = code_or_args if isinstance(code_or_args, list) else ["-c", code_or_args]
    return subprocess.run(
        [sys.executable, *(["-I"] if isolated else []), *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


class ImportLayoutTests(unittest.TestCase):
    def test_no_source_uses_the_top_level_lib_name(self):
        offenders = []
        for path in PYTHON_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "lib":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} from {node.module}")
                elif isinstance(node, ast.Import):
                    offenders += [f"{path.relative_to(ROOT)}:{node.lineno} import {alias.name}"
                                  for alias in node.names if alias.name.split(".")[0] == "lib"]
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and re.fullmatch(r"lib\.[a-z_]+(\.[A-Za-z_]+)*", node.value)):
                    # patch("lib.x.y"), import_module("lib.x") and sys.modules keys
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.value!r}")
        self.assertEqual(offenders, [], "Use mpdrussoundpi.russound.lib.* or a relative import inside lib/")

    def test_every_module_imports_by_its_canonical_name_only(self):
        modules = [f"mpdrussoundpi.russound.lib.{name}" for name in LIBRARY_MODULES]
        modules += [f"mpdrussoundpi.russound.{name}" for name in ENTRY_SCRIPTS]
        code = f"""
import importlib, sys, types
from unittest.mock import MagicMock
sys.path.insert(0, {str(ROOT)!r})
# Optional runtime dependencies are not needed to check import names.
for optional in ("mpd", "waitress", "waitress.server", "flask", "werkzeug", "werkzeug.exceptions"):
    try:
        importlib.import_module(optional)
    except ImportError:
        sys.modules[optional] = MagicMock()
for name in {modules!r}:
    importlib.import_module(name)
short = sorted(name for name in sys.modules if name == "lib" or name.startswith("lib."))
assert not short, short
print(len({modules!r}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = run_python(code, cwd=directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout), len(LIBRARY_MODULES) + len(ENTRY_SCRIPTS))

    def test_entry_script_runs_by_file_path_from_another_directory(self):
        # systemd starts the scripts as `python /path/to/script.py`.
        with tempfile.TemporaryDirectory() as directory:
            result = run_python([str(PACKAGE / "mpdctrl.py"), "--help"], cwd=directory, isolated=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: mpdctrl.py", result.stdout)

    def test_egress_install_layout_from_the_playbook_runs(self):
        playbook = yaml.safe_load((ROOT / "ansible/playbooks/deploy-playback-egress.yml").read_text())
        tasks = {task["name"]: task for task in playbook[0]["tasks"]}
        copy = tasks["Copy the root-owned filter and shared address policy"]
        self.assertEqual(copy["ansible.builtin.copy"]["dest"],
                         "{{ playback_egress_install }}/mpdrussoundpi/russound/{{ item }}")
        with tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as elsewhere:
            for item in copy["loop"]:
                target = Path(install) / "mpdrussoundpi/russound" / item
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PACKAGE / item, target)
            script = Path(install) / "mpdrussoundpi/russound/playbackegress.py"
            result = run_python([str(script), "--help"], cwd=elsewhere, isolated=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: playbackegress.py", result.stdout)

    def test_egress_ownership_preflight_layout_runs(self):
        playbook = yaml.safe_load((ROOT / "ansible/playbooks/deploy-playback-egress.yml").read_text())
        block = next(task for task in playbook[0]["tasks"] if "block" in task)["block"]
        tasks = {task["name"]: task for task in block}
        staged = tasks["Stage shared ownership validator without changing playback sources"]["ansible.builtin.copy"]
        self.assertEqual(staged["dest"], "{{ playback_egress_preflight.path }}/mpdrussoundpi/russound/lib/")
        argv = tasks["Validate configured outputs and effective owned unit paths"]["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[1:3], ["-m", "mpdrussoundpi.russound.lib.mpdownership"])
        with tempfile.TemporaryDirectory() as preflight:
            shutil.copytree(LIBRARY, Path(preflight) / "mpdrussoundpi/russound/lib",
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = run_python(["-m", argv[2], "--help"], cwd=preflight, isolated=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)


if __name__ == "__main__":
    unittest.main()
