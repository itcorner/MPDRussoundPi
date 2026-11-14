import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from web.playback_status import PlaybackAgentConfig


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"
DOCKER = shutil.which("docker")


@unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
class DeploymentRenderIntegrationTests(unittest.TestCase):
    def test_existing_docker_preflight_rejects_blank_host_and_image_references(self):
        source = yaml.safe_load((ROOT / "ansible/playbooks/deploy-russound-docker.yml").read_text())[0]
        tasks = [task for task in source["tasks"] if task["name"] in {
            "Require an explicit external endpoint when not managing the serial gateway",
            "Require explicit published image references",
        }]
        self.assertEqual(len(tasks), 2)
        cases = (
            ({"russound_manage_serial_gateway": False, "russound_backend_host": ""}, False),
            ({"russound_manage_serial_gateway": False, "russound_backend_host": "  "}, False),
            ({"russound_manage_serial_gateway": False, "russound_backend_host": " audio.example "}, False),
            ({"russound_manage_serial_gateway": False, "russound_backend_host": 7}, False),
            ({"russound_manage_serial_gateway": False, "russound_backend_host": "audio.example"}, True),
            ({"russound_use_published_images": True, "russound_web_image": "  ",
              "russound_ser2net_image": "example/serial:dev"}, False),
            ({"russound_use_published_images": True, "russound_web_image": "example/web:dev",
              "russound_ser2net_image": "  "}, False),
            ({"russound_use_published_images": True, "russound_web_image": "example/web:dev",
              "russound_ser2net_image": "example/serial:dev"}, True),
            ({"russound_use_published_images": True, "russound_manage_serial_gateway": False,
              "russound_backend_host": "audio.example", "russound_web_image": "example/web:dev",
              "russound_ser2net_image": ""}, True),
        )
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / "preflight.yml"
            for overrides, succeeds in cases:
                with self.subTest(overrides=overrides):
                    playbook.write_text(yaml.safe_dump([{
                        "hosts": "localhost", "connection": "local", "gather_facts": False,
                        "vars": {**source["vars"], **overrides, "ansible_python_interpreter": sys.executable},
                        "tasks": tasks,
                    }]))
                    result = subprocess.run([str(ANSIBLE), "-i", "localhost,", str(playbook)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)

    def test_ansible_templates_and_compose_preserve_agent_configuration(self):
        if not DOCKER:
            self.skipTest("local Docker Compose is required")
        variables = {
            "app_dir": "/app", "venv_dir": "/venv", "russound_config_dir": "/config",
            "russound_state_dir": "/state", "russound_host": "0.0.0.0",
            "russound_port": 8000, "deploy_user": "musicpi",
            "russound_publish_port": 8000, "russound_serial_device": "/dev/ttyUSB0",
            "russound_serial_options": "19200n81", "russound_ser2net_bind": "0.0.0.0",
            "russound_serial_gid": 20, "russound_backend_host": "russound-ser2net",
            "russound_backend_port": 6666, "russound_uid": 1000, "russound_gid": 1000,
            "russound_debug": False,
        }
        agent = {"agent_id": "musicpi", "url": "https://audio.example", "token_file": "/run/secrets/token"}
        special_agent = {**agent, "token_file": "/run/secrets/\\'token-${HOME}-%-é"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            playbook = root / "render.yml"
            compose = ROOT / "docker-compose.yml"
            for index, value in enumerate(([], [agent], [special_agent], {}, "[]")):
                with self.subTest(value=value):
                    target_env = root / f"web-{index}.env"
                    target_unit = root / f"web-{index}.service"
                    playbook.write_text(yaml.safe_dump([{
                        "hosts": "localhost", "connection": "local", "gather_facts": False,
                        "vars": {**variables, "russound_playback_agents": value,
                                 "ansible_python_interpreter": sys.executable},
                        "tasks": [
                            {"name": "Render isolated fixture", "ansible.builtin.template": {
                                "src": str(ROOT / "ansible/templates" / source), "dest": str(destination)
                            }}
                            for source, destination in (
                                ("russound-docker.env.j2", target_env),
                                ("russound-web.service.j2", target_unit),
                            )
                        ],
                    }]))
                    subprocess.run([str(ANSIBLE), "-i", "localhost,", str(playbook)],
                                   check=True, capture_output=True, text=True)
                    environment = {key: item for key, item in os.environ.items()
                                   if not key.startswith(("RUSSOUND_", "COMPOSE_"))}
                    result = subprocess.run(
                        [DOCKER, "compose", "--env-file", str(target_env), "-f", str(compose),
                         "config", "--format", "json"],
                        check=True, capture_output=True, text=True, env=environment,
                    )
                    rendered = json.loads(result.stdout)["services"]["russound-web"]["environment"]["RUSSOUND_PLAYBACK_AGENTS"]
                    # Compose config escapes literal dollars in its re-serializable output.
                    compose_value = rendered.replace("$$", "$")
                    line = next(line for line in target_unit.read_text().splitlines()
                                if line.startswith('Environment="RUSSOUND_PLAYBACK_AGENTS='))
                    # Approximate systemd quoting; actual systemd execution requires Linux.
                    unit_value = shlex.split(line.removeprefix("Environment="))[0].split("=", 1)[1]
                    unit_value = unit_value.replace("%%", "%")
                    for runtime_value in (compose_value, unit_value):
                        with patch.dict(os.environ, {"RUSSOUND_PLAYBACK_AGENTS": runtime_value}, clear=True):
                            if value == []:
                                self.assertIsNone(PlaybackAgentConfig.from_environment())
                            elif value in ([agent], [special_agent]):
                                configs = PlaybackAgentConfig.from_environment()
                                self.assertIsNotNone(configs)
                                assert configs is not None
                                self.assertEqual(configs[0].agent_id, "musicpi")
                                self.assertEqual(configs[0].token_file, value[0]["token_file"])
                            else:
                                with self.assertRaises(ValueError):
                                    PlaybackAgentConfig.from_environment()
