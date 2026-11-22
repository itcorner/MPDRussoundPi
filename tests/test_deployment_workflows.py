import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

from deploymentfixtures import playbook, boolean


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "ansible/playbooks"
TEMPLATES = ROOT / "ansible/templates"
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"
RETAINED_PLAYS = {
    "bootstrap-deployment.yml", "deploy-dev.yml", "deploy-dev-build.yml",
    "upgrade-deployment.yml", "restore-deployment-tools.yml",
    "stage-native-deployment.yml", "setup-musicpi.yml",
    "deploy-playback-agent.yml", "remove-playback-agent.yml",
    "deploy-playback-agent-proxy.yml", "deploy-playback-egress.yml", "deploy-updater.yml",
    "deploy-web-tls.yml",
}
RETIRED_PATHS = (
    "docker-compose.yml", "docker-compose.dummy.yml",
    "ansible/playbooks/deploy-russound-docker.yml",
    "ansible/playbooks/deploy-russound-web.yml",
    "ansible/playbooks/stage-russound-web.yml",
    "ansible/roles/deployment_account",
    "ansible/roles/deployment_inputs/defaults/docker.yml",
    "ansible/roles/deployment_inputs/defaults/web.yml",
    "ansible/roles/deployment_inputs/defaults/web_stage.yml",
    "ansible/templates/russound-docker.env.j2",
    "ansible/templates/russound-registry-compose.yml.j2",
    "ansible/templates/russound-web.service.j2",
)


def all_tasks(tasks):
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from all_tasks(task.get(section, []))


def render(text, **variables):
    environment = Environment(undefined=StrictUndefined)
    environment.filters["bool"] = boolean
    environment.filters["to_json"] = json.dumps
    return environment.from_string(text).render(**variables)


class DeploymentWorkflowTests(unittest.TestCase):
    def test_retired_paths_are_absent_and_retained_entry_points_are_explicit(self):
        self.assertEqual({path.name for path in PLAYBOOKS.glob("*.yml")}, RETAINED_PLAYS)
        for name in RETIRED_PATHS:
            with self.subTest(path=name):
                # Ignore empty working-tree directories left after tracked-file removal.
                path = ROOT / name
                self.assertFalse(path.is_file())
                if path.is_dir():
                    self.assertFalse(any(item.is_file() for item in path.rglob("*")))

    def test_active_automation_does_not_reference_retired_entry_points(self):
        retired_names = {Path(name).name for name in RETIRED_PATHS
                         if name.startswith("ansible/") and name.endswith(".j2")}
        retired_names.update(Path(name).name for name in RETIRED_PATHS
                             if name.startswith("ansible/playbooks/"))
        for folder in ("ansible", ".github/workflows", "tool"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and path.suffix in (".yml", ".yaml", ".py", ".sh", ".mjs"):
                    text = path.read_text()
                    for name in retired_names:
                        with self.subTest(path=path.relative_to(ROOT), retired=name):
                            self.assertNotIn(name, text)

    @unittest.skipUnless(ANSIBLE.is_file(), "local Ansible is required")
    def test_all_retained_playbooks_pass_syntax_checks_without_target_contact(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.yml"
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"musicpi": {
                "ansible_host": "invalid.example",
                "ansible_connection": "ssh",
            }}}}))
            for name in sorted(RETAINED_PLAYS):
                with self.subTest(playbook=name):
                    result = subprocess.run(
                        [str(ANSIBLE), "-i", str(inventory), str(PLAYBOOKS / name), "--syntax-check"],
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_proxy_unit_exposes_reload_using_its_configured_admin_endpoint(self):
        variables = {
            "playback_proxy_caddy_binary": "/usr/bin/caddy",
            "playback_proxy_caddyfile": "/etc/caddy/agent.Caddyfile",
        }
        unit = render((TEMPLATES / "playback-agent-proxy.service.j2").read_text(), **variables)
        self.assertIn(
            "ExecReload=/usr/bin/caddy reload --config /etc/caddy/agent.Caddyfile "
            "--adapter caddyfile --address 127.0.0.1:2020",
            unit,
        )
        self.assertIn("admin 127.0.0.1:2020", (TEMPLATES / "playback-agent.Caddyfile.j2").read_text())

    def test_proxy_generates_ten_year_certificate_without_replacing_existing_pair(self):
        tasks = playbook("deploy-playback-agent-proxy.yml")["tasks"]
        generate = next(task for task in tasks if task["name"].startswith("Generate a self-signed certificate"))
        self.assertIn("'-days', '3650'", generate["ansible.builtin.command"]["argv"])
        self.assertIn("not playback_proxy_certificate_stat.stat.exists", generate["when"])
        self.assertEqual(generate["args"]["creates"], "{{ playback_proxy_certificate }}")

    def test_repository_deployments_do_not_force_dirty_checkouts(self):
        tasks = list(all_tasks(playbook("setup-musicpi.yml")["tasks"]))
        git_tasks = [task.get("git", task.get("ansible.builtin.git")) for task in tasks
                     if "git" in task or "ansible.builtin.git" in task]
        self.assertTrue(git_tasks)
        for config in git_tasks:
            self.assertFalse(config.get("force", False))

    def test_proxy_allows_additional_docker_network_without_losing_existing_source(self):
        template = (TEMPLATES / "playback-agent.Caddyfile.j2").read_text()
        result = render(template, playback_proxy_hostname="192.168.1.105", playback_proxy_port=8443,
                        playback_proxy_certificate="/cert", playback_proxy_private_key="/key",
                        playback_proxy_allowed_cidr="192.168.1.0/24",
                        playback_proxy_additional_allowed_cidrs=["172.18.0.0/16"],
                        playback_proxy_enable_operations=True, playback_agent_port=8081)
        self.assertIn("remote_ip 192.168.1.0/24 172.18.0.0/16", result)

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
