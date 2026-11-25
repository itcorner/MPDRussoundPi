from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import jinja2
import yaml

from deploymentfixtures import ROOT, playbook

from tool import mdns_check

TEMPLATES = ROOT / "ansible/templates"
ROLE = ROOT / "ansible/roles/deployment_inputs"
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"


class FakeAvahi:
    """Answers the commands mdns_check runs; records what it was asked to do."""

    def __init__(self, state="active", fqdn="musicpi.local", address="192.168.1.105", dbus=True):
        self.state, self.fqdn, self.address, self.dbus = state, fqdn, address, dbus
        self.calls: list[list[str]] = []
        self.restart_code = 0

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if self.state == "active" else 3, self.state + "\n", "")
        if argv[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(argv, 0, "1000000\n", "")  # started 1 s after boot
        if argv[:2] == ["systemctl", "restart"]:
            return subprocess.CompletedProcess(argv, self.restart_code, "", "")
        if argv[0] == "busctl":
            self.assert_busctl(argv)
            if not self.dbus:
                return subprocess.CompletedProcess(argv, 1, "", "Timeout reached")
            if "GetHostNameFqdn" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "s", "data": [self.fqdn]}), "")
            if "ResolveHostName" in argv:
                if self.address is None:
                    return subprocess.CompletedProcess(argv, 1, "", "Timeout reached")
                data = [3, 0, argv[-3], 0, self.address, 0]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "iisisu", "data": data}), "")
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def assert_busctl(argv):
        # "--" keeps the -1 interface argument from being read as an option.
        assert argv.index("--") < argv.index("call"), argv

    def restarted(self):
        return ["systemctl", "restart", "avahi-daemon.service"] in self.calls


def uptime(seconds):
    return mock.patch("builtins.open", mock.mock_open(read_data=f"{seconds} 1.0\n"))


class MdnsCheckTests(unittest.TestCase):
    def test_healthy_avahi_is_left_alone(self):
        avahi = FakeAvahi()
        self.assertEqual(mdns_check.check(avahi, "musicpi.local"), {"ok": True, "name": "musicpi.local"})
        self.assertFalse(avahi.restarted())
        resolve = next(call for call in avahi.calls if "ResolveHostName" in call)
        self.assertEqual(resolve[-6:], ["iisiu", "-1", "0", "musicpi.local", "0", "0"], "IPv4 on any interface")

    def test_each_fault_restarts_avahi_once_it_has_settled(self):
        faults = {
            "avahi-daemon is failed": FakeAvahi(state="failed"),
            "avahi-daemon publishes musicpi-2.local instead of musicpi.local": FakeAvahi(fqdn="musicpi-2.local"),
            "avahi-daemon does not answer on D-Bus": FakeAvahi(dbus=False),
            "avahi-daemon does not resolve musicpi.local": FakeAvahi(address=None),
        }
        for reason, avahi in faults.items():
            with self.subTest(reason=reason), uptime(3600):
                outcome = mdns_check.check(avahi, "musicpi.local")
                self.assertEqual((outcome["reason"], outcome["action"]), (reason, "restart"))
                self.assertTrue(avahi.restarted())

    def test_a_recent_start_is_not_restarted_again(self):
        avahi = FakeAvahi(fqdn="musicpi-2.local")
        with uptime(120):
            outcome = mdns_check.check(avahi, "musicpi.local")
        self.assertEqual(outcome["action"], "none")
        self.assertIn("started 119s ago", outcome["detail"])
        self.assertFalse(avahi.restarted())

    def test_a_failed_restart_fails_the_unit(self):
        avahi = FakeAvahi(state="failed")
        avahi.restart_code = 1
        with uptime(3600), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mdns_check.main(["--name", "musicpi.local"], runner=avahi), 1)
        self.assertEqual(json.loads(output.getvalue())["action"], "restart-failed")

    def test_main_reports_one_json_line_and_succeeds_when_healthy(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mdns_check.main(["--name", "musicpi.local"], runner=FakeAvahi()), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "name": "musicpi.local"})

    def test_a_hanging_command_counts_as_a_failure(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("busctl", 8)):
            self.assertEqual(mdns_check.run(["busctl"], 8).returncode, 124)


def render(name, **values):
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), undefined=jinja2.StrictUndefined)
    return environment.get_template(name).render(**values)


class MdnsTemplateTests(unittest.TestCase):
    def test_avahi_uses_only_the_lan_interfaces_and_ipv4(self):
        text = render("avahi-daemon.conf.j2", mdns_interfaces=["wlan0"], mdns_ipv6=False)
        self.assertIn("allow-interfaces=wlan0\n", text)
        self.assertIn("use-ipv6=no\n", text)
        self.assertIn("publish-aaaa-on-ipv4=no\n", text, "use-ipv6=no alone still answers AAAA over IPv4")
        self.assertIn("use-ipv4=yes\n", text)
        self.assertIn("publish-aaaa-on-ipv4=yes\n", render("avahi-daemon.conf.j2", mdns_interfaces=["wlan0"], mdns_ipv6=True))
        self.assertIn("use-ipv6=yes\n", render("avahi-daemon.conf.j2", mdns_interfaces=["eth0", "wlan0"], mdns_ipv6=True))
        self.assertIn("allow-interfaces=eth0,wlan0\n",
                      render("avahi-daemon.conf.j2", mdns_interfaces=["eth0", "wlan0"], mdns_ipv6=False))

    def test_avahi_restarts_after_a_crash(self):
        text = render("avahi-restart.conf.j2")
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartSec=5", text)

    def test_check_runs_the_tool_on_a_timer(self):
        values = {"mdns_name": "musicpi.local", "mdns_check_tool": "/usr/local/lib/mpdrussoundpi/mdns_check.py",
                  "mdns_check_interval": "2min"}
        service = render("mdns-check.service.j2", **values)
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/mpdrussoundpi/mdns_check.py --name musicpi.local",
                      service)
        self.assertIn("Type=oneshot", service)
        timer = render("mdns-check.timer.j2", **values)
        for line in ("OnBootSec=5min", "OnUnitActiveSec=2min", "WantedBy=timers.target"):
            self.assertIn(line, timer)


class MdnsPlaybookTests(unittest.TestCase):
    def setUp(self):
        self.play = playbook("deploy-mdns.yml")
        self.tasks = {task["name"]: task for task in self.play["tasks"]}

    def test_defaults(self):
        values = self.play["vars"]
        self.assertEqual(values["mdns_interfaces"], ["eth0", "wlan0"])
        self.assertFalse(values["mdns_ipv6"])
        self.assertEqual(values["mdns_check_interval"], "2min")

    def test_only_present_interfaces_are_configured(self):
        configure = self.tasks["Install the Avahi configuration for the LAN interfaces only"]
        self.assertEqual(configure["vars"]["mdns_interfaces"], "{{ mdns_present_interfaces }}")
        self.assertTrue(configure["ansible.builtin.template"]["backup"], "the original stays next to it")
        self.assertIn("Refuse to restrict Avahi to interfaces this host does not have", self.tasks)
        self.assertIn("Refuse to configure an absent Avahi installation", self.tasks)

    def test_configuration_changes_restart_avahi_before_the_check(self):
        names = list(self.tasks)
        for name in ("Install the Avahi configuration for the LAN interfaces only",
                     "Restart Avahi automatically when it fails"):
            self.assertEqual(self.tasks[name]["notify"], "Restart Avahi")
        self.assertLess(names.index("Apply a changed Avahi configuration before checking it"),
                        names.index("Wait until Avahi publishes the name again"))
        wait = self.tasks["Wait until Avahi publishes the name again"]["ansible.builtin.command"]["argv"]
        self.assertEqual(wait[-2:], ["--settle", "86400"], "the play's own check never restarts Avahi")


@unittest.skipUnless(ANSIBLE.is_file(), "local Ansible required")
class MdnsAnsibleTests(unittest.TestCase):
    def test_syntax(self):
        result = subprocess.run([str(ANSIBLE), "--syntax-check", "-i", str(ROOT / "ansible/inventory.yaml"),
                                 str(ROOT / "ansible/playbooks/deploy-mdns.yml")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_inputs(self, **inputs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inventory.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "ansible_facts": {"hostname": "musicpi"}, **inputs}}}}))
            (root / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False, "tasks": [
                {"ansible.builtin.import_role": {"name": str(ROLE), "defaults_from": "mdns"}},
                {"ansible.builtin.assert": {"that": ["mdns_ipv6 is boolean"]}}]}]))
            return subprocess.run([str(ANSIBLE), "-i", str(root / "inventory.yml"), str(root / "play.yml")],
                                  capture_output=True, text=True)

    def test_inputs(self):
        result = self.run_inputs(mdns_ipv6="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for bad in ({"mdns_interfaces": "wlan0"}, {"mdns_interfaces": []}, {"mdns_interfaces": ["wlan0,eth0"]},
                    {"mdns_interfaces": ["a" * 16]}, {"mdns_check_interval": "2 minutes"}, {"mdns_ipv6": "maybe"}):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.run_inputs(**bad).returncode, 0)


if __name__ == "__main__":
    unittest.main()
