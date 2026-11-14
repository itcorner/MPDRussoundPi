from pathlib import Path
import subprocess
import sys
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "ansible/playbooks/stage-russound-web.yml"


class NativeWebStagingTests(unittest.TestCase):
    def setUp(self):
        self.play = yaml.safe_load(PLAYBOOK.read_text())[0]

    def test_entry_point_is_revision_pinned_and_requires_explicit_stage_tag(self):
        self.assertEqual(self.play["hosts"], "musicpi")
        self.assertTrue(self.play["become"])
        inputs = next(task for task in self.play["pre_tasks"]
                      if "Validate native web staging inputs" in task["name"])
        self.assertEqual(inputs["ansible.builtin.import_role"]["defaults_from"], "web_stage")
        assertion = next(task for task in self.play["pre_tasks"]
                         if "Require Linux" in task["name"])["ansible.builtin.assert"]["that"]
        self.assertIn("russound_web_revision is match('^[a-f0-9]{40,64}$')", assertion)
        self.assertIn("not ansible_check_mode", assertion)
        self.assertIn("'stage' in ansible_run_tags", assertion)
        self.assertIn("ansible_run_tags | difference(['all', 'stage']) | length == 0", assertion)
        git = next(task["ansible.builtin.git"] for task in self.play["tasks"]
                   if "ansible.builtin.git" in task)
        self.assertEqual(git["version"], "{{ russound_web_revision }}")
        self.assertFalse(git["update"])
        self.assertFalse(git["force"])
        reserve = next(task["ansible.builtin.command"]["argv"] for task in self.play["tasks"]
                       if "Atomically reserve" in task["name"])
        self.assertIn("/usr/bin/mkdir", reserve)
        self.assertIn("-m", reserve)
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_inputs/defaults/web_stage.yml").read_text()
        )
        self.assertIn("candidates", defaults["russound_web_candidate_root"])

    def test_staging_uses_separate_environment_and_never_activates_or_mutates_live_data(self):
        for task in self.play["tasks"]:
            self.assertIn("stage", task["tags"] if isinstance(task["tags"], list) else [task["tags"]])
            for module in ("ansible.builtin.apt", "ansible.builtin.systemd", "ansible.builtin.systemd_service",
                           "ansible.builtin.template", "ansible.builtin.copy"):
                self.assertNotIn(module, task)
        venv = next(task["ansible.builtin.command"]["argv"][-1]
                    for task in self.play["tasks"]
                    if "Create an isolated native web candidate virtual environment" in task["name"])
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_inputs/defaults/web_stage.yml").read_text()
        )
        self.assertIn("candidates", defaults["russound_web_candidate_root"])
        requirements = next(task["ansible.builtin.pip"]["requirements"]
                            for task in self.play["tasks"] if "ansible.builtin.pip" in task)
        self.assertIn("source/web/requirements.txt", requirements)
        text = PLAYBOOK.read_text()
        for forbidden in ("russound-web.service", "/etc/systemd", "ser2net", "state: restarted"):
            self.assertNotIn(forbidden, text)

    def test_existing_config_validation_is_private_and_compiles_without_mutating_checkout(self):
        validation = next(task for task in self.play["tasks"]
                          if "Validate existing native web configuration" in task["name"])
        self.assertTrue(validation["no_log"])
        self.assertFalse(validation["changed_when"])
        self.assertTrue(validation["when"])
        code = validation["ansible.builtin.command"]["argv"][2]
        compile(code, "<native-web-config-validation>", "exec")
        self.assertIn("validate_russound_config", code)
        compile_task = next(task for task in self.play["tasks"]
                            if "Compile candidate web modules" in task["name"])
        compile_code = compile_task["ansible.builtin.command"]["argv"][2]
        compile(compile_code, "<native-web-compile>", "exec")
        self.assertEqual(compile_task["environment"]["PYTHONDONTWRITEBYTECODE"], "1")
        compiled = subprocess.run(
            [sys.executable, "-c", compile_code, str(ROOT / "web")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        validate = subprocess.run(
            [sys.executable, "-c", code, str(ROOT), str(ROOT / "web/config_example.json")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertEqual(validate.stdout.strip(), "valid")

    def test_preflight_refuses_reuse_symlinks_and_unowned_persistent_config(self):
        candidate = next(task for task in self.play["pre_tasks"]
                         if "Inspect the exact web candidate path" in task["name"])
        self.assertFalse(candidate["ansible.builtin.stat"]["follow"])
        checks = next(task["ansible.builtin.assert"]["that"] for task in self.play["pre_tasks"]
                      if "Refuse candidate reuse" in task["name"])
        self.assertTrue(any("russound_web_candidate.stat.exists" in check for check in checks))
        self.assertTrue(any("russound_web_candidate.stat.islnk" in check for check in checks))
        self.assertTrue(any("russound_web_config.stat.uid" in check for check in checks))


if __name__ == "__main__":
    unittest.main()
