"""Guards the single-module-name layout (see the package layout plan in docs/plans/)."""

import ast
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import unittest.mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mpdrussoundpi" / "russound"
LIBRARY = PACKAGE / "lib"
ENTRY_SCRIPTS = sorted(path.stem for path in PACKAGE.glob("*.py") if path.name != "__init__.py")
LIBRARY_MODULES = sorted(path.stem for path in LIBRARY.glob("*.py") if path.name != "__init__.py")
COMMON = ROOT / "mpdrussoundpi" / "common"
COMMON_MODULES = sorted(path.stem for path in COMMON.glob("*.py") if path.name != "__init__.py")
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
        modules += [f"mpdrussoundpi.common.{name}" for name in COMMON_MODULES]
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
        self.assertEqual(int(result.stdout), len(LIBRARY_MODULES) + len(COMMON_MODULES) + len(ENTRY_SCRIPTS))

    def test_package_code_never_edits_sys_path(self):
        # Scripts run as console commands or `python -m`; nothing may patch the import path.
        offenders = [f"{path.relative_to(ROOT)}:{number}"
                     for path in (ROOT / "mpdrussoundpi").rglob("*.py") if "__pycache__" not in path.parts
                     for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                     if "sys.path" in line]
        self.assertEqual(offenders, [])

    def test_entry_script_runs_as_a_module_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-P", "-m", "mpdrussoundpi.russound.mpdctrl", "--help"],
                cwd=directory, capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: mpdctrl.py", result.stdout)

    def test_egress_install_layout_from_the_playbook_runs(self):
        playbook = yaml.safe_load((ROOT / "ansible/playbooks/deploy-playback-egress.yml").read_text())
        tasks = {task["name"]: task for task in playbook[0]["tasks"]}
        copy = tasks["Copy the root-owned filter and shared address policy"]
        self.assertEqual(copy["ansible.builtin.copy"]["src"], "{{ playback_egress_source }}/{{ item.src }}")
        self.assertEqual(copy["ansible.builtin.copy"]["dest"], "{{ playback_egress_install }}/mpdrussoundpi/{{ item.dest }}")
        with tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as elsewhere:
            for item in copy["loop"]:  # playback_egress_source is mpdrussoundpi/russound
                target = Path(install) / "mpdrussoundpi" / item["dest"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile((PACKAGE / item["src"]).resolve(), target)
            # Run exactly like the unit and the MPD drop-in: module form, install root on PYTHONPATH.
            result = subprocess.run(
                [sys.executable, "-P", "-m", "mpdrussoundpi.russound.playbackegress", "--help"],
                cwd=elsewhere, capture_output=True, text=True, timeout=60,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": install},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("playbackegress", result.stdout)
        unit = (ROOT / "ansible/templates/playback-egress.service.j2").read_text()
        self.assertIn("Environment=PYTHONPATH={{ playback_egress_install }}\n"
                      "ExecStart=/usr/bin/python3 -P -m mpdrussoundpi.russound.playbackegress ", unit)
        dropin = tasks["Scope MPD to the protected slice and gate process startup"]["ansible.builtin.copy"]["content"]
        self.assertIn("ExecStartPre=/usr/bin/env PYTHONPATH={{ playback_egress_install }} "
                      "/usr/bin/python3 -P -m mpdrussoundpi.russound.playbackegress --wait", dropin)

    def test_egress_ownership_preflight_layout_runs(self):
        playbook = yaml.safe_load((ROOT / "ansible/playbooks/deploy-playback-egress.yml").read_text())
        block = next(task for task in playbook[0]["tasks"] if "block" in task)["block"]
        tasks = {task["name"]: task for task in block}
        staged = tasks["Stage shared ownership validator without changing playback sources"]["ansible.builtin.copy"]
        self.assertEqual(staged["dest"], "{{ playback_egress_preflight.path }}/mpdrussoundpi/russound/lib/")
        argv = tasks["Validate configured outputs and effective owned unit paths"]["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[1:3], ["-m", "mpdrussoundpi.russound.lib.mpdownership"])
        shared = tasks["Stage the shared modules the ownership validator imports"]["ansible.builtin.copy"]
        self.assertEqual(shared["src"], "{{ playback_egress_source }}/../common/")
        self.assertEqual(shared["dest"], "{{ playback_egress_preflight.path }}/mpdrussoundpi/common/")
        with tempfile.TemporaryDirectory() as preflight:
            shutil.copytree(LIBRARY, Path(preflight) / "mpdrussoundpi/russound/lib",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(COMMON, Path(preflight) / "mpdrussoundpi/common",
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = run_python(["-m", argv[2], "--help"], cwd=preflight, isolated=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)


    def test_web_imports_only_the_shared_common_package(self):
        offenders = []
        for path in (ROOT / "web").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0
                         else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                offenders += [f"{path.relative_to(ROOT)}:{node.lineno} {name}" for name in names
                              if name.startswith("mpdrussoundpi") and not name.startswith("mpdrussoundpi.common")]
        self.assertEqual(offenders, [], "The web image ships only mpdrussoundpi.common")

    def test_deployment_files_use_canonical_names_without_path_edits(self):
        offenders = []
        for path in [*(ROOT / "ansible").rglob("*.yml"), *(ROOT / "ansible").rglob("*.j2")]:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"(from|import|-m,?)\s+lib\.|sys\.path\.insert", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_pyproject_matches_the_repository(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        packages = set(project["tool"]["setuptools"]["packages"])
        on_disk = {".".join(path.parent.relative_to(ROOT).parts)
                   for top in ("web", "mpdrussoundpi") for path in (ROOT / top).rglob("__init__.py")
                   if "__pycache__" not in path.parts}
        self.assertEqual(packages, on_disk)
        for package, patterns in project["tool"]["setuptools"]["package-data"].items():
            for pattern in patterns:
                self.assertTrue(list((ROOT / package.replace(".", "/")).glob(pattern)), f"{package}: {pattern}")
        for extra, spec in project["tool"]["setuptools"]["dynamic"]["optional-dependencies"].items():
            for name in spec["file"]:
                self.assertTrue((ROOT / name).is_file(), f"{extra}: {name}")
        for command, target in project["project"]["scripts"].items():
            module, function = target.split(":")
            with self.subTest(command=command):
                with unittest.mock.patch.dict(sys.modules, {"mpd": unittest.mock.MagicMock()}):
                    self.assertTrue(callable(getattr(importlib.import_module(module), function)))
        generated = {"mpd-supervisor", "audio-ready", "playback-audit"}
        self.assertLessEqual(generated, set(project["project"]["scripts"]), "Commands used by generated units")


if __name__ == "__main__":
    unittest.main()
