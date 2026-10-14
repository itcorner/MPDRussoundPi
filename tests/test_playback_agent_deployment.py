from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "ansible/playbooks"
SERVICE = ROOT / "ansible/templates/playback-agent.service.j2"


class PlaybackAgentDeploymentTests(unittest.TestCase):
    """Static checks only: never run Ansible, systemctl, pip or target commands."""

    def setUp(self):
        self.deploy = yaml.safe_load((PLAYBOOKS / "deploy-playback-agent.yml").read_text())[0]
        self.remove = yaml.safe_load((PLAYBOOKS / "remove-playback-agent.yml").read_text())[0]

    def tasks(self, play, module):
        return [task for task in play["tasks"] if module in task]

    def test_separate_agent_play_has_no_audio_provisioning_or_repository_updates(self):
        self.assertEqual(self.deploy["hosts"], "musicpi")
        self.assertFalse(self.deploy["become"])
        self.assertFalse(self.deploy["vars"]["playback_agent_start"])
        for task in self.deploy["tasks"]:
            self.assertNotIn("include_tasks", task)
            self.assertNotIn("ansible.builtin.include_tasks", task)
            self.assertNotIn("ansible.builtin.git", task)
            self.assertNotIn("ansible.builtin.apt", task)
            self.assertNotIn("ansible.builtin.user", task)
        text = (PLAYBOOKS / "deploy-playback-agent.yml").read_text()
        for forbidden in ("audiosetup.py", "mpdsetup.py", "mpdsupervisor.py",
                          "mpd_meta_control.service", "pipewire.service", "ser2net"):
            self.assertNotIn(forbidden, text)

    def test_unit_is_loopback_only_independent_and_restart_bounded(self):
        variables = {
            "playback_agent_dir": "/home/musicpi/.local/share/mpdrussoundpi-agent",
            "playback_agent_venv": "/home/musicpi/.local/share/mpdrussoundpi-agent/venv",
            "playback_agent_config": "/home/musicpi/.config/mpdrussoundpi/config.json",
            "playback_agent_secret_dir": "/home/musicpi/.config/mpdrussoundpi-agent",
            "playback_agent_id": "musicpi",
            "playback_agent_port": 8081,
            "ansible_facts": {"user_uid": 1000},
        }
        unit = Environment(undefined=StrictUndefined).from_string(SERVICE.read_text()).render(**variables)
        self.assertIn("--host 127.0.0.1 --port 8081", unit)
        self.assertNotIn("--allow-remote", unit)
        self.assertIn("--token-file /home/musicpi/.config/mpdrussoundpi-agent/token", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("StartLimitBurst=5", unit)
        self.assertIn("TimeoutStopSec=90", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn("PULSE_SERVER=unix:/run/user/1000/pulse/native", unit)
        self.assertNotIn("ExecStartPre", unit)
        self.assertNotIn("Wants=", unit)
        self.assertNotIn("Requires=", unit)
        self.assertNotIn("User=", unit)

    def test_service_operations_target_only_agent_and_start_is_opt_in(self):
        services = self.tasks(self.deploy, "ansible.builtin.systemd_service")
        for task in services:
            config = task["ansible.builtin.systemd_service"]
            self.assertEqual(config["scope"], "user")
            if "name" in config:
                self.assertEqual(config["name"], "mpdrussoundpi-playback-agent.service")
                self.assertIn("playback_agent_start | bool", task["when"])
        for task in self.tasks(self.deploy, "ansible.builtin.uri"):
            self.assertIn("playback_agent_start | bool", task["when"])
            self.assertIn("not ansible_check_mode", task["when"])

    def test_credentials_are_private_and_never_in_service_or_logs(self):
        secret = next(task for task in self.tasks(self.deploy, "ansible.builtin.copy")
                      if task["ansible.builtin.copy"]["dest"].endswith("/token"))
        self.assertTrue(secret["no_log"])
        self.assertFalse(secret["diff"])
        self.assertEqual(secret["ansible.builtin.copy"]["mode"], "0600")
        for task in self.tasks(self.deploy, "ansible.builtin.uri"):
            config = task["ansible.builtin.uri"]
            self.assertFalse(config["use_proxy"])
            if "headers" in config:
                self.assertTrue(task["no_log"])
        self.assertNotIn("Authorization", SERVICE.read_text())
        assertions = self.tasks(self.deploy, "ansible.builtin.assert")
        self.assertTrue(any(task.get("no_log") for task in assertions))

    def test_only_agent_sources_and_isolated_dependencies_are_installed(self):
        copies = self.tasks(self.deploy, "ansible.builtin.copy")
        entries = next(task for task in copies if "loop" in task and isinstance(task["loop"], list))
        self.assertEqual(entries["loop"], ["playbackagent.py", "requirements-agent.txt"])
        dependencies = self.tasks(self.deploy, "ansible.builtin.pip")
        self.assertEqual(len(dependencies), 1)
        self.assertEqual(dependencies[0]["ansible.builtin.pip"]["virtualenv"], "{{ playback_agent_venv }}")
        self.assertIn("requirements-agent.txt", dependencies[0]["ansible.builtin.pip"]["requirements"])
        for task in copies:
            self.assertNotEqual(task["ansible.builtin.copy"]["dest"], "{{ playback_agent_config }}")
        check = next(task for task in self.tasks(self.deploy, "ansible.builtin.command")
                     if "Validate migrated inventory" in task["name"])
        code = check["ansible.builtin.command"]["argv"][2]
        compile(code, "<deployment-config-validation>", "exec")
        self.assertNotIn("refresh", code)
        self.assertIn("collector.close()", code)
        self.assertFalse(check["changed_when"])

    def test_removal_preserves_code_token_configuration_and_lingering(self):
        tasks = self.tasks(self.remove, "ansible.builtin.file")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["ansible.builtin.file"], {
            "path": "{{ playback_agent_unit_dir }}/mpdrussoundpi-playback-agent.service",
            "state": "absent",
        })
        for task in self.tasks(self.remove, "ansible.builtin.systemd_service"):
            config = task["ansible.builtin.systemd_service"]
            if "name" in config:
                self.assertEqual(config["name"], "mpdrussoundpi-playback-agent.service")
                self.assertFalse(config["enabled"])
                self.assertEqual(config["state"], "stopped")
        self.assertFalse(self.tasks(self.remove, "ansible.builtin.command"))


if __name__ == "__main__":
    unittest.main()
