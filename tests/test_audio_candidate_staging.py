from pathlib import Path
import unittest

import yaml

from deploymentfixtures import ROOT, playbook


class AudioCandidateStagingTests(unittest.TestCase):
    def setUp(self):
        self.play = playbook("setup-musicpi.yml")
        self.stage_tasks = [task for task in self.play["tasks"] if "stage" in task.get("tags", [])]

    def test_candidate_source_and_environment_are_revision_pinned_and_separate(self):
        git = next(task for task in self.stage_tasks if "ansible.builtin.git" in task)
        self.assertIn("{{ mpd_russoundpi_candidate_root }}/{{ mpd_russoundpi_revision }}/source",
                      git["ansible.builtin.git"]["dest"])
        self.assertEqual(git["ansible.builtin.git"]["version"], "{{ mpd_russoundpi_revision }}")
        self.assertFalse(git["ansible.builtin.git"]["update"])
        self.assertFalse(git["ansible.builtin.git"]["force"])
        reserve = next(task for task in self.stage_tasks if "Atomically reserve" in task["name"])
        self.assertEqual(reserve["ansible.builtin.command"]["argv"][:4],
                         ["/usr/bin/mkdir", "-m", "0700", "--"])
        self.assertIn("{{ mpd_russoundpi_candidate_root }}/{{ mpd_russoundpi_revision }}",
                      reserve["ansible.builtin.command"]["argv"][4])
        self.assertLess(self.stage_tasks.index(reserve), self.stage_tasks.index(git))
        venv = next(task for task in self.stage_tasks if "isolated audio candidate virtual" in task["name"])
        self.assertIn("audio-candidates", playbook("setup-musicpi.yml")["vars"]["mpd_russoundpi_candidate_root"])
        validate = next(task for task in self.stage_tasks if "operational audio config" in task["name"])
        self.assertIn("parse_configuration", validate["ansible.builtin.command"]["argv"][2])

    def test_existing_candidate_storage_must_be_owned_and_private(self):
        preflight = self.play["pre_tasks"]
        candidate = next(task for task in preflight if task["name"] == "Inspect immutable audio candidate path")
        self.assertFalse(candidate["ansible.builtin.stat"]["follow"])
        root = next(task for task in preflight if task["name"] == "Inspect candidate storage root without following a symlink")
        self.assertFalse(root["ansible.builtin.stat"]["follow"])
        guard = next(task for task in preflight if task["name"].startswith("Refuse to reuse"))
        assertions = guard["ansible.builtin.assert"]["that"]
        self.assertTrue(any("mpd_audio_candidate_root.stat.uid" in value and
                            "mpd_audio_candidate_root.stat.mode" in value
                            for value in assertions))

    def test_stage_tag_cannot_manage_services_or_active_paths(self):
        names = [task["name"] for task in self.stage_tasks]
        self.assertTrue(any("bounded audio dependencies" in name for name in names))
        self.assertTrue(all(any(module in task for module in
                               ("ansible.builtin.file", "ansible.builtin.git", "ansible.builtin.command",
                                "ansible.builtin.pip", "ansible.builtin.debug"))
                            for task in self.stage_tasks))
        self.assertFalse(any("systemd" in str(task) or "apt" in str(task) for task in self.stage_tasks))
        self.assertNotIn("{{ mpd_russoundpi_dest }}", yaml.safe_dump(self.stage_tasks))
        self.assertNotIn("{{ venv_dir }}", yaml.safe_dump(self.stage_tasks))

    def test_activation_is_explicitly_blocked_until_native_transaction_exists(self):
        preflight = self.play["pre_tasks"]
        guard = next(task for task in preflight if task["name"].startswith("Require immutable candidate"))
        self.assertIn("mpd_russoundpi_mode != 'activate'", guard["ansible.builtin.assert"]["that"])
        self.assertTrue(any("'stage' in ansible_run_tags" in value
                            for value in guard["ansible.builtin.assert"]["that"]))
        self.assertIn("verified recovery backup", guard["ansible.builtin.assert"]["fail_msg"])
        self.assertEqual(playbook("setup-musicpi.yml")["vars"]["mpd_russoundpi_mode"], "stage")

    def test_audio_setup_ownership_defaults_to_no_native_gateway(self):
        defaults = ROOT / "ansible/roles/deployment_inputs/defaults/audio.yml"
        self.assertFalse(yaml.safe_load(defaults.read_text())["mpd_russoundpi_manage_serial_gateway"])


if __name__ == "__main__":
    unittest.main()
