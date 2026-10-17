import json
from pathlib import Path
import shlex
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "ansible/playbooks"
TEMPLATES = ROOT / "ansible/templates"


def playbook(name):
    return yaml.safe_load((PLAYBOOKS / name).read_text())[0]


def all_tasks(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from all_tasks(task.get(section, []))


def render(text, **variables):
    environment = Environment(undefined=StrictUndefined)
    environment.filters["bool"] = bool
    environment.filters["to_json"] = json.dumps
    return environment.from_string(text).render(**variables)


class DeploymentWorkflowTests(unittest.TestCase):
    def test_repository_deployments_do_not_force_dirty_checkouts(self):
        for name in ("setup-musicpi.yml", "deploy-russound-web.yml", "deploy-russound-docker.yml"):
            with self.subTest(playbook=name):
                tasks = list(all_tasks(playbook(name)["tasks"]))
                git_tasks = [task.get("git", task.get("ansible.builtin.git")) for task in tasks
                             if "git" in task or "ansible.builtin.git" in task]
                self.assertTrue(git_tasks)
                for config in git_tasks:
                    self.assertFalse(config.get("force", False))

    def test_external_gateway_mode_preserves_host_gateway_and_skips_serial_container(self):
        play = playbook("deploy-russound-docker.yml")
        stop = next(task for task in play["tasks"] if task["name"] == "Disable the host ser2net service")
        self.assertIn("russound_manage_serial_gateway | bool", stop["when"])
        start = next(task for task in play["tasks"] if task["name"] == "Build and start the containers")
        command = start["ansible.builtin.command"]["cmd"]
        self.assertEqual(
            shlex.split(render(command, russound_manage_serial_gateway=False))[-3:],
            ["--build", "--no-deps", "russound-web"],
        )
        self.assertNotIn("--no-deps", render(command, russound_manage_serial_gateway=True))

    def test_docker_creates_all_writable_mounts_and_private_secret_directory(self):
        tasks = playbook("deploy-russound-docker.yml")["tasks"]
        mounts = next(task for task in tasks if task["name"] == "Ensure the bind-mounted directories exist")
        self.assertEqual(set(mounts["loop"]), {"config", "data", "logs"})
        secrets = next(task for task in tasks if task["name"].startswith("Create the private playback"))
        config = secrets["ansible.builtin.file"]
        self.assertEqual(config["mode"], "0700")
        self.assertEqual(config["owner"], "{{ deploy_user }}")

    def test_managed_docker_env_keeps_playback_agent_list(self):
        agents = [{
            "agent_id": "musicpi", "url": "https://192.168.1.105:8443",
            "token_file": "/run/secrets/musicpi-playback-token",
            "tls_ca_file": "/run/secrets/musicpi-proxy.crt",
        }]
        defaults = playbook("deploy-russound-docker.yml")["vars"]
        self.assertEqual(defaults["russound_playback_agents"], [])
        text = render(
            (TEMPLATES / "russound-docker.env.j2").read_text(),
            russound_publish_port=8000, russound_serial_device="/dev/ttyUSB0",
            russound_serial_options="19200n81", russound_ser2net_bind="0.0.0.0",
            russound_serial_gid=20, russound_backend_host="192.168.1.105",
            russound_backend_port=6666, russound_uid=1000, russound_gid=1000,
            russound_debug=False, russound_playback_agents=agents,
        )
        value = next(line for line in text.splitlines() if line.startswith("RUSSOUND_PLAYBACK_AGENTS="))
        self.assertEqual(json.loads(shlex.split(value.split("=", 1)[1])[0]), agents)

    def test_native_web_uses_persistent_paths_and_isolated_environment(self):
        play = playbook("deploy-russound-web.yml")
        self.assertNotIn("russound-venv", play["vars"]["venv_dir"])
        tasks = play["tasks"]
        config = next(task for task in tasks if task["name"].startswith("Create persistent web"))
        self.assertIn("{{ russound_state_dir }}", config["loop"])
        for name in ("Create Python virtual environment", "Install Python dependencies"):
            task = next(task for task in tasks if task["name"] == name)
            self.assertEqual(task["become_user"], "{{ deploy_user }}")
        unit = render(
            (TEMPLATES / "russound-web.service.j2").read_text(),
            app_dir="/home/musicpi/MPDRussoundPi",
            venv_dir="/home/musicpi/.local/share/russound-web/venv",
            russound_config_dir="/home/musicpi/.config/russound-web",
            russound_state_dir="/home/musicpi/.local/state/russound-web",
            russound_host="0.0.0.0", russound_port=8000, deploy_user="musicpi",
            russound_playback_agents=[],
        )
        self.assertIn("-m web.russound_server", unit)
        self.assertIn("--state /home/musicpi/.local/state/russound-web/russound_state.json", unit)
        self.assertNotIn("/tmp/", unit)
        self.assertNotIn("--config /home/musicpi/MPDRussoundPi/", unit)
        for task in tasks:
            if task["name"] in (
                "Install the native serial gateway when requested",
                "Copy ser2net configuration file",
                "Enable and start ser2net",
            ):
                self.assertEqual(task["when"], "russound_manage_serial_gateway | bool")

    def test_staged_audio_deployment_cannot_start_or_enable_playback(self):
        play = playbook("setup-musicpi.yml")
        tasks = list(all_tasks(play["tasks"]))
        stop = next(task for task in tasks if task["name"].startswith("Stop configured MPD services"))
        self.assertIn("--stop", stop["ansible.builtin.command"]["argv"])
        self.assertEqual(stop["when"], "not (mpd_russoundpi_start_playback | bool)")
        activation_tasks = []
        for task in tasks:
            config = task.get("ansible.builtin.systemd_service", {})
            if "enabled" in config and config.get("name") in ("mpd_meta_control.service", "mpd_playback_supervisor.service"):
                activation_tasks.append(config["name"])
                self.assertEqual(render(config["enabled"], mpd_russoundpi_start_playback=False), "False")
                self.assertEqual(render(config["state"], mpd_russoundpi_start_playback=False), "stopped")
                self.assertEqual(render(config["enabled"], mpd_russoundpi_start_playback=True), "True")
                self.assertEqual(render(config["state"], mpd_russoundpi_start_playback=True), "started")
            if task["name"] in (
                "Restart configured MPD instances",
                "Verify restored playback, routes and gains before enabling supervision",
            ):
                self.assertEqual(task["when"], "mpd_russoundpi_start_playback | bool")
        self.assertCountEqual(activation_tasks, ["mpd_meta_control.service", "mpd_playback_supervisor.service"])
        upgrade = next(task for task in tasks if task.get("include_tasks", {}).get("file") == "../tasks/update.yml")
        self.assertEqual(upgrade["tags"], ["never", "update"])
        self.assertEqual(upgrade["include_tasks"]["apply"]["tags"], "update")


if __name__ == "__main__":
    unittest.main()
