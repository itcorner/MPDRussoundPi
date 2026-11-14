import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from deploymentfixtures import ROOT, playbook


ROLE = ROOT / "ansible/roles/deployment_inputs"
SPEC = importlib.util.spec_from_file_location("deployment_input_filters", ROLE / "filter_plugins/inputs.py")
assert SPEC is not None and SPEC.loader is not None
filters = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(filters)
InputError = filters.AnsibleFilterError


class DeploymentInputFilterTests(unittest.TestCase):
    def test_strict_booleans_and_ports(self):
        for value in (False, "false", "FALSE", "no", "off", "0"):
            self.assertIs(filters.deployment_bool(value), False)
        for value in (True, "true", "YES", "on", "1"):
            self.assertIs(filters.deployment_bool(value), True)
        for value in (None, "", "maybe", " false ", [], {}, 0, 1, 0.0):
            with self.subTest(value=value), self.assertRaises(InputError):
                filters.deployment_bool(value)
        for value in (1, "1", 65535, "65535"):
            self.assertEqual(filters.deployment_port(value), int(value))
        for value in (True, False, 0, -1, 65536, 1.5, " 80", "80 ", "1e2", "８０", "000080"):
            with self.subTest(value=value), self.assertRaises(InputError):
                filters.deployment_port(value)

    def test_literal_resolvers_cidrs_and_hostnames(self):
        self.assertEqual(filters.deployment_addresses(["127.0.0.53", "::1"]), ["127.0.0.53", "::1"])
        filters.deployment_addresses(["192.168.1.0/24", "2001:db8::/32"], network=True)
        for value in ("1.1.1.1", {"resolver": "1.1.1.1"}, [], [True], ["dns.example"], ["1.2.3.999"], ["fe80::1%en0"]):
            with self.subTest(value=value), self.assertRaises(InputError):
                filters.deployment_addresses(value)
        for value in ("a", "a.example", "musicpi.home.arpa", "192.168.1.105"):
            self.assertEqual(filters.deployment_hostname(value), value)
        for value in ("", "a..b", "-a", "a-", "127.0.0.999", "a/b", "::1", " a", "a" * 64):
            with self.subTest(value=value), self.assertRaises(InputError):
                filters.deployment_hostname(value)

    def test_agent_lists_validate_without_secret_reads(self):
        agent = {"agent_id": "musicpi", "url": "https://audio.example:8443", "token_file": "/run/secrets/token"}
        self.assertEqual(filters.deployment_agents([]), "")
        self.assertEqual(json.loads(filters.deployment_agents([agent])), [agent])
        for value in ("[]", {}, [agent, agent], [{**agent, "url": "http://audio.example"}],
                      [{**agent, "token_file": "relative"}], [{**agent, "url": "https://audio.example:invalid"}]):
            with self.subTest(value=value), self.assertRaises(InputError):
                filters.deployment_agents(value)

    def test_component_defaults_are_below_inventory_and_audio_does_not_own_gateway(self):
        for name in ("deploy-russound-web.yml", "deploy-russound-docker.yml", "deploy-playback-agent.yml",
                     "deploy-playback-agent-proxy.yml", "deploy-playback-egress.yml", "setup-musicpi.yml",
                     "stage-russound-web.yml"):
            raw = yaml.safe_load((ROOT / "ansible/playbooks" / name).read_text())[0]
            self.assertNotIn("vars", raw, name)
            self.assertEqual(raw["pre_tasks"][0]["ansible.builtin.import_role"]["name"], "../roles/deployment_inputs")
        self.assertFalse(playbook("setup-musicpi.yml")["vars"]["mpd_russoundpi_manage_serial_gateway"])


@unittest.skipUnless((Path(sys.executable).parent / "ansible-playbook").is_file(), "local Ansible required")
class DeploymentInputRoleIntegrationTests(unittest.TestCase):
    def test_inventory_defaults_and_invalid_preflight_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.yml"
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "playback_agent_port": "18081", "playback_agent_enable_operations": "false",
            }}}}))
            path = root / "play.yml"
            path.write_text(yaml.safe_dump([{
                "hosts": "all", "gather_facts": False,
                "tasks": [{"ansible.builtin.import_role": {"name": str(ROLE), "defaults_from": "agent"}},
                          {"ansible.builtin.assert": {"that": [
                    "playback_agent_port == 18081",
                    "playback_agent_port is integer",
                    "playback_agent_enable_operations == false",
                    "playback_agent_enable_operations is boolean",
                ]}}],
            }]))
            executable = str(Path(sys.executable).parent / "ansible-playbook")
            for overrides, success in (({}, True), ({"playback_agent_port": "8081oops"}, False),
                                       ({"playback_agent_enable_operations": "maybe"}, False)):
                with self.subTest(overrides=overrides):
                    result = subprocess.run([executable, "-i", str(inventory), str(path),
                                             "-e", json.dumps(overrides)], capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                    self.assertNotIn("changed=1", result.stdout)

    def test_string_boolean_extra_vars_render_through_agent_template_as_booleans(self):
        executable = str(Path(sys.executable).parent / "ansible-playbook")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            play = root / "render.yml"
            unit = root / "agent.service"
            play.write_text(yaml.safe_dump([{
                "hosts": "all", "connection": "local", "gather_facts": False,
                "vars": {"ansible_facts": {"user_uid": 1000, "user_dir": "/tmp/musicpi"}},
                "tasks": [
                    {"ansible.builtin.import_role": {
                        "name": str(ROLE), "defaults_from": "agent",
                    }},
                    {"ansible.builtin.template": {
                        "src": str(ROOT / "ansible/templates/playback-agent.service.j2"),
                        "dest": str(unit),
                    }},
                ],
            }]))
            for value, enabled in (("false", False), ("true", True)):
                with self.subTest(value=value):
                    result = subprocess.run(
                        [executable, "-i", "localhost,", str(play), "-e",
                         json.dumps({"playback_agent_enable_operations": value})],
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual("--operations-db" in unit.read_text(), enabled)

    def test_proxy_address_shape_fails_in_shared_preflight(self):
        executable = str(Path(sys.executable).parent / "ansible-playbook")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.yml"
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "playback_proxy_hostname": "musicpi.home.arpa",
                "playback_proxy_allowed_cidr": "192.0.2.0/24",
                "playback_proxy_additional_allowed_cidrs": "192.0.2.10",
            }}}}))
            play = root / "proxy.yml"
            play.write_text(yaml.safe_dump([{
                "hosts": "all", "gather_facts": False,
                "tasks": [{"ansible.builtin.import_role": {
                    "name": str(ROLE), "defaults_from": "proxy",
                }}],
            }]))
            result = subprocess.run(
                [executable, "-i", str(inventory), str(play)],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Validate additional proxy CIDRs input type", result.stdout)
            self.assertNotIn("changed=1", result.stdout)

    def test_component_booleans_and_ports_are_normalized_before_tasks_run(self):
        executable = str(Path(sys.executable).parent / "ansible-playbook")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.yml"
            inventory.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "playback_proxy_hostname": "musicpi.home.arpa",
                "playback_proxy_allowed_cidr": "192.0.2.0/24",
                "playback_egress_resolvers": ["192.0.2.53"],
            }}}}))
            for component in ("docker", "web", "agent", "proxy", "egress", "audio", "web_stage"):
                with self.subTest(component=component):
                    defaults = yaml.safe_load(
                        (ROLE / "defaults" / f"{component}.yml").read_text())
                    inventory_inputs = {
                        **dict.fromkeys(defaults.get("deployment_boolean_inputs", []), "false"),
                        **dict.fromkeys(defaults.get("deployment_port_inputs", []), "18081"),
                    }
                    inventory_inputs.update({
                        "ansible_connection": "local",
                        "ansible_python_interpreter": sys.executable,
                        "playback_proxy_hostname": "musicpi.home.arpa",
                        "playback_proxy_allowed_cidr": "192.0.2.0/24",
                        "playback_egress_resolvers": ["192.0.2.53"],
                    })
                    inventory.write_text(yaml.safe_dump({"all": {"hosts": {
                        "localhost": inventory_inputs,
                    }}}))
                    assertions = [
                        f"lookup('ansible.builtin.vars', '{name}') is boolean and "
                        f"lookup('ansible.builtin.vars', '{name}') == false"
                        for name in defaults.get("deployment_boolean_inputs", [])
                    ] + [
                        f"lookup('ansible.builtin.vars', '{name}') is integer and "
                        f"lookup('ansible.builtin.vars', '{name}') == 18081"
                        for name in defaults.get("deployment_port_inputs", [])
                    ]
                    play = root / f"{component}.yml"
                    play.write_text(yaml.safe_dump([{
                        "hosts": "all", "gather_facts": False,
                        "tasks": [
                            {"ansible.builtin.import_role": {
                                "name": str(ROLE), "defaults_from": component,
                            }},
                            {"ansible.builtin.assert": {"that": assertions}},
                        ],
                    }]))
                    result = subprocess.run(
                        [executable, "-i", str(inventory), str(play)],
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("changed=1", result.stdout)
