from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml
from deploymentfixtures import playbook, boolean


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "ansible/playbooks"
SERVICE = ROOT / "ansible/templates/playback-agent.service.j2"
PROXY_PLAYBOOK = PLAYBOOKS / "deploy-playback-agent-proxy.yml"
PROXY_CADDYFILE = ROOT / "ansible/templates/playback-agent.Caddyfile.j2"
PROXY_SERVICE = ROOT / "ansible/templates/playback-agent-proxy.service.j2"


class PlaybackAgentDeploymentTests(unittest.TestCase):
    """Static checks only: never run Ansible, systemctl, pip or target commands."""

    def setUp(self):
        self.deploy = playbook("deploy-playback-agent.yml")
        self.remove = yaml.safe_load((PLAYBOOKS / "remove-playback-agent.yml").read_text())[0]

    def tasks(self, play, module):
        return [task for task in play["tasks"] if module in task]

    def test_agent_play_only_stages_a_revision_pinned_candidate(self):
        self.assertEqual(self.deploy["hosts"], "musicpi")
        self.assertFalse(self.deploy["become"])
        for task in self.deploy["tasks"]:
            self.assertNotIn("ansible.builtin.systemd_service", task)
            self.assertNotIn("ansible.builtin.uri", task)
            self.assertNotIn("ansible.builtin.template", task)
            self.assertIn("stage", task["tags"] if isinstance(task["tags"], list) else [task["tags"]])
        git = next(task["ansible.builtin.git"] for task in self.deploy["tasks"]
                   if "ansible.builtin.git" in task)
        self.assertEqual(git["version"], "{{ playback_agent_revision }}")
        self.assertFalse(git["update"])
        self.assertFalse(git["force"])
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_inputs/defaults/agent.yml").read_text()
        )
        self.assertIn("agent-candidates", defaults["playback_agent_candidate_root"])
        reserve = next(task["ansible.builtin.command"]["argv"] for task in self.deploy["tasks"]
                       if "Atomically reserve" in task["name"])
        self.assertIn("/usr/bin/mkdir", reserve)
        pip = next(task["ansible.builtin.pip"] for task in self.deploy["tasks"] if "ansible.builtin.pip" in task)
        # The candidate's own checkout, installed editable with only the agent extra.
        self.assertEqual(pip["name"], "{{ playback_agent_candidate_root }}/{{ playback_agent_revision }}/source[agent]")
        self.assertTrue(pip["editable"])
        command = next(task["ansible.builtin.command"]["argv"] for task in self.deploy["tasks"]
                       if task["name"] == "Verify the candidate's agent command starts")
        self.assertEqual(command, ["{{ playback_agent_candidate_root }}/{{ playback_agent_revision }}/venv/bin/playback-agent",
                                   "--help"])
        mode = next(task["ansible.builtin.assert"]["that"] for task in self.deploy["pre_tasks"]
                    if "Require explicit revision-pinned" in task["name"])
        self.assertIn("playback_agent_mode == 'stage'", mode)
        self.assertIn("'stage' in ansible_run_tags", mode)
        self.assertIn("not ansible_check_mode", mode)
        self.assertIn("playback_agent_revision is match('^[a-f0-9]{40,64}$')", mode)
        text = (PLAYBOOKS / "deploy-playback-agent.yml").read_text()
        for forbidden in ("playback_agent_token_source", "token_installed", "loginctl",
                          "systemd_service", "playback_agent.service"):
            self.assertNotIn(forbidden, text)

    def test_agent_candidate_validates_existing_config_without_state_changes(self):
        config_check = next(task for task in self.deploy["tasks"]
                            if "Validate existing playback configuration" in task["name"])
        self.assertTrue(config_check["no_log"])
        self.assertFalse(config_check["changed_when"])
        self.assertTrue(config_check["when"])
        code = config_check["ansible.builtin.command"]["argv"][2]
        compile(code, "<candidate-config-validation>", "exec")
        self.assertIn("MPDBackend", code)
        self.assertNotIn("refresh", code)
        self.assertEqual(config_check["environment"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertFalse(self.tasks(self.deploy, "ansible.builtin.copy"))
        self.assertFalse(self.tasks(self.deploy, "ansible.builtin.systemd_service"))

    def test_candidate_preflight_rejects_reuse_and_untrusted_paths(self):
        assertions = [task for task in self.deploy["pre_tasks"]
                      if "Refuse an existing candidate" in task["name"]]
        self.assertEqual(len(assertions), 1)
        checks = assertions[0]["ansible.builtin.assert"]["that"]
        self.assertTrue(any("playback_agent_candidate.stat.exists" in check for check in checks))
        self.assertTrue(any("playback_agent_operational_config.stat.isreg" in check for check in checks))

    def test_separate_agent_play_has_no_audio_provisioning_or_repository_updates(self):
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
        environment = Environment(undefined=StrictUndefined)
        environment.filters["bool"] = boolean
        unit = environment.from_string(SERVICE.read_text()).render(**variables)
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

    def test_tls_proxy_is_separate_loopback_only_and_start_is_opt_in(self):
        play = playbook("deploy-playback-agent-proxy.yml")
        self.assertEqual(play["hosts"], "musicpi")
        self.assertTrue(play["become"])
        self.assertFalse(play["vars"]["playback_proxy_start"])
        environment = Environment(undefined=StrictUndefined)
        environment.filters["bool"] = boolean
        caddyfile = environment.from_string(
            PROXY_CADDYFILE.read_text()
        ).render(
            playback_proxy_hostname="musicpi.home.arpa",
            playback_proxy_port=8443,
            playback_proxy_certificate="/etc/caddy/certs/agent.crt",
            playback_proxy_private_key="/etc/caddy/certs/agent.key",
            playback_proxy_allowed_cidr="192.168.1.20/32",
            playback_agent_port=8081,
        )
        self.assertIn("admin 127.0.0.1:2020", caddyfile)
        self.assertIn("protocols h1 h2", caddyfile)
        self.assertIn("musicpi.home.arpa:8443", caddyfile)
        self.assertIn("remote_ip 192.168.1.20/32", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:8081", caddyfile)
        self.assertIn("/v1/outputs/*", caddyfile)
        self.assertIn("/v1/favorites", caddyfile)
        self.assertIn("/v1/host", caddyfile)
        self.assertIn("respond 403", caddyfile)
        self.assertNotIn("--allow-remote", caddyfile)
        named_services = [
            task
            for task in self.tasks(play, "ansible.builtin.systemd_service")
            if "name" in task["ansible.builtin.systemd_service"]
        ]
        self.assertEqual(len(named_services), 1)
        for task in named_services:
            config = task["ansible.builtin.systemd_service"]
            self.assertEqual(config["name"], "mpdrussoundpi-playback-agent-proxy.service")
            self.assertIn("playback_proxy_start | bool", task["when"])
        proxy_unit = Environment(undefined=StrictUndefined).from_string(
            PROXY_SERVICE.read_text()
        ).render(
            playback_proxy_caddy_binary="/usr/bin/caddy",
            playback_proxy_caddyfile="/etc/caddy/mpdrussoundpi-playback-agent.Caddyfile",
        )
        self.assertIn("User=caddy", proxy_unit)
        self.assertIn("Group=caddy", proxy_unit)
        self.assertIn("NoNewPrivileges=true", proxy_unit)
        self.assertIn("ProtectSystem=strict", proxy_unit)
        self.assertIn("ReadWritePaths=/var/lib/caddy/playback-agent", proxy_unit)
        self.assertNotIn("CAP_NET_BIND_SERVICE", proxy_unit)

    def test_manual_operations_require_explicit_agent_and_proxy_opt_in(self):
        defaults = yaml.safe_load(
            (ROOT / "ansible/roles/deployment_inputs/defaults/agent.yml").read_text()
        )
        self.assertFalse(defaults["playback_agent_enable_operations"])
        self.assertTrue(defaults["playback_agent_enable_tunein_selection"])
        self.assertEqual(defaults["playback_agent_mode"], "stage")
        proxy = playbook("deploy-playback-agent-proxy.yml")
        self.assertFalse(proxy["vars"]["playback_proxy_enable_operations"])
        environment = Environment(undefined=StrictUndefined, trim_blocks=True)
        environment.filters["bool"] = boolean
        service = environment.from_string(SERVICE.read_text())
        common = dict(
            playback_agent_dir="/home/musicpi/agent", playback_agent_venv="/home/musicpi/agent/venv",
            playback_agent_config="/home/musicpi/config.json", playback_agent_secret_dir="/home/musicpi/secrets",
            playback_agent_id="musicpi", playback_agent_port=8081,
            playback_agent_state_dir="/home/musicpi/agent-state", ansible_facts={"user_uid": 1000},
        )
        read_only_unit = service.render(**common)
        self.assertNotIn("--operations-db", read_only_unit)
        self.assertNotIn("tunein-selection", read_only_unit)
        unit = service.render(**common, playback_agent_enable_operations=True)
        self.assertIn("--operations-db /home/musicpi/agent-state/operations.sqlite3", unit)
        self.assertNotIn("tunein-selection", unit)
        tunein_disabled_unit = service.render(
            **common, playback_agent_enable_operations=True, playback_agent_enable_tunein_selection=False,
        )
        self.assertIn("--disable-tunein-selection", tunein_disabled_unit)
        self.assertIn("\nEnvironment=PYTHONUNBUFFERED", unit)
        caddy = environment.from_string(PROXY_CADDYFILE.read_text())
        variables = dict(
            playback_proxy_hostname="192.168.1.105", playback_proxy_port=8443,
            playback_proxy_certificate="/cert", playback_proxy_private_key="/key",
            playback_proxy_allowed_cidr="192.168.1.0/24", playback_agent_port=8081,
        )
        readonly = caddy.render(**variables)
        self.assertIn("method GET HEAD", readonly)
        self.assertNotIn("method POST", readonly)
        # Host facts are read-only: the route sits in the GET/HEAD matcher, never with the write routes.
        read_matcher = readonly[readonly.index("@read_api"):readonly.index("handle @read_api")]
        self.assertIn("/v1/host", read_matcher)
        self.assertNotIn("/v1/host", caddy.render(**variables, playback_proxy_enable_operations=True)
                         .split("@write_api")[1])
        self.assertNotIn("/v1/favorites/operations", readonly)
        enabled = caddy.render(**variables, playback_proxy_enable_operations=True)
        self.assertIn("/v1/outputs/*/operations", enabled)
        self.assertIn("/v1/operations/*", enabled)
        self.assertIn("/v1/outputs/*/operations /v1/favorites/operations", enabled)
        self.assertIn("method POST", enabled)

    def test_proxy_play_never_restarts_agent_or_audio_services(self):
        play = yaml.safe_load(PROXY_PLAYBOOK.read_text())[0]
        services = [
            task
            for task in self.tasks(play, "ansible.builtin.systemd_service")
            if "name" in task["ansible.builtin.systemd_service"]
        ]
        self.assertEqual(len(services), 1)
        for task in services:
            self.assertEqual(
                task["ansible.builtin.systemd_service"]["name"],
                "mpdrussoundpi-playback-agent-proxy.service",
            )
        content = PROXY_PLAYBOOK.read_text()
        for forbidden in (
            "mpdrussoundpi-playback-agent.service",
            "mpd.service",
            "pipewire.service",
            "ser2net",
            "ansible.builtin.apt",
        ):
            self.assertNotIn(forbidden, content)
        self.assertTrue(any(
            task.get("no_log")
            for task in self.tasks(play, "ansible.builtin.command")
            if "Generate a self-signed certificate" in task["name"]
        ))
        trust_copy = self.tasks(play, "ansible.builtin.fetch")
        self.assertEqual(len(trust_copy), 1)
        self.assertEqual(
            trust_copy[0]["ansible.builtin.fetch"]["src"],
            "{{ playback_proxy_certificate }}",
        )
        self.assertIn("musicpi-proxy.crt", trust_copy[0]["ansible.builtin.fetch"]["dest"])
        self.assertNotIn("private_key", trust_copy[0]["ansible.builtin.fetch"]["src"])
        self.assertFalse(any(
            "firewall" in task["name"].lower()
            for task in play["tasks"]
        ))


if __name__ == "__main__":
    unittest.main()
