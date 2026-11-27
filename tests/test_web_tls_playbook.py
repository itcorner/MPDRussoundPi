from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

from deploymentfixtures import ROOT, playbook

TEMPLATES = ROOT / "ansible/templates"
ROLE = ROOT / "ansible/roles/deployment_inputs"
ANSIBLE = Path(sys.executable).parent / "ansible-playbook"

VALUES = {
    "web_tls_admin_port": 2021, "web_tls_dns_names": ["musicpi.local", "musicpi"], "web_tls_https_port": 443,
    "web_tls_http_port": 80, "web_tls_upstream_port": 8000, "web_tls_dir": "/etc/mpdrussoundpi-web-tls",
    "web_tls_updater_public_dir": "/run/mpdrussoundpi-updater-public",
    "web_tls_caddy_binary": "/usr/bin/caddy", "web_tls_caddyfile": "/etc/caddy/mpdrussoundpi-web.Caddyfile",
    "web_tls_tool": "/usr/local/lib/mpdrussoundpi/web_tls.py", "web_tls_unit": "mpdrussoundpi-web-proxy",
    "web_tls_ensure_args": ["ensure", "--dir", "/etc/mpdrussoundpi-web-tls", "--dns=musicpi.local"],
}


def render(name: str, **overrides) -> str:
    environment = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), undefined=jinja2.StrictUndefined)
    return environment.get_template(name).render(**{**VALUES, **overrides})


def tasks(play) -> dict[str, dict]:
    return {task["name"]: task for task in play["tasks"]}


class WebTlsTemplateTests(unittest.TestCase):
    def test_caddyfile_terminates_tls_and_redirects(self):
        text = render("web-proxy.Caddyfile.j2")
        self.assertIn("admin 127.0.0.1:2021", text, "not the playback proxy's admin port")
        self.assertIn("auto_https off", text)
        self.assertIn("grace_period 2s", text, "a restart must not wait for open event streams")
        self.assertIn("default_sni musicpi.local", text)
        self.assertIn("https://:443 {", text)
        self.assertIn("tls /etc/mpdrussoundpi-web-tls/server.crt /etc/mpdrussoundpi-web-tls/server.key", text)
        self.assertIn("reverse_proxy 127.0.0.1:8000 {", text)
        self.assertIn("flush_interval -1", text, "server-sent events must not be buffered")
        self.assertIn("http://:80 {", text)
        https_site, http_site = text.split("http://:80 {")
        for site in (https_site, http_site):
            self.assertIn("redir /ca /ca/ 308", site, "the trust page on both ports")
            self.assertIn("handle_path /ca/* {", site)
            self.assertIn("root * /etc/mpdrussoundpi-web-tls/public", site)
        self.assertNotIn("reverse_proxy", http_site, "port 80 never serves the application")
        self.assertIn("redir https://{host}{uri} 308", text)
        self.assertNotIn("ca.key", text)
        self.assertIn("redir https://{host}:8443{uri} 308", render("web-proxy.Caddyfile.j2", web_tls_https_port=8443))

    def test_caddyfile_serves_only_the_updaters_public_status_file(self):
        https_site, http_site = render("web-proxy.Caddyfile.j2").split("http://:80 {")
        route = https_site.split("handle /update-status.json {")[1].split("}")[0]
        self.assertIn("root * /run/mpdrussoundpi-updater-public", route)
        self.assertIn('header Cache-Control "no-store"', route)
        self.assertIn("file_server", route)
        self.assertNotIn("browse", route)
        self.assertLess(https_site.index("handle /update-status.json"), https_site.index("reverse_proxy"))
        self.assertNotIn("update-status", http_site, "port 80 only redirects")
        self.assertEqual(yaml.safe_load((ROLE / "defaults/web_tls.yml").read_text())["web_tls_updater_public_dir"],
                         "/run/mpdrussoundpi-updater-public")

    def test_proxy_unit_binds_low_ports_with_one_capability_only(self):
        text = render("web-proxy.service.j2")
        for line in ("User=caddy", "AmbientCapabilities=CAP_NET_BIND_SERVICE",
                     "CapabilityBoundingSet=CAP_NET_BIND_SERVICE", "NoNewPrivileges=true", "ProtectSystem=strict",
                     "ReadWritePaths=/var/lib/caddy/web-proxy", "--address 127.0.0.1:2021",
                     "--config /etc/caddy/mpdrussoundpi-web.Caddyfile"):
            self.assertIn(line, text)

    def test_renewal_uses_the_play_arguments_and_reloads_the_proxy(self):
        service = render("web-tls-renew.service.j2")
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/mpdrussoundpi/web_tls.py ensure --dir "
                      "/etc/mpdrussoundpi-web-tls --dns=musicpi.local --restart-unit mpdrussoundpi-web-proxy.service",
                      service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("ReadWritePaths=/etc/mpdrussoundpi-web-tls", service)
        timer = render("web-tls-renew.timer.j2")
        for line in ("OnCalendar=daily", "Persistent=true", "WantedBy=timers.target"):
            self.assertIn(line, timer)


class WebTlsPlaybookTests(unittest.TestCase):
    def setUp(self):
        self.play = playbook("deploy-web-tls.yml")
        self.tasks = tasks(self.play)

    def test_defaults_are_safe(self):
        values = self.play["vars"]
        self.assertFalse(values["web_tls_start"], "ports are taken only on request")
        self.assertEqual((values["web_tls_https_port"], values["web_tls_http_port"], values["web_tls_upstream_port"]),
                         (443, 80, 8000))
        self.assertEqual(values["web_tls_permitted_ips"], ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])
        self.assertEqual(values["web_tls_server_days"], 397)
        self.assertNotEqual(values["web_tls_admin_port"], 2020, "the playback proxy owns 2020")

    def test_the_play_and_the_timer_share_one_argument_list(self):
        ensure = self.tasks["Create the CA once and issue or renew the server certificate"]
        self.assertEqual(ensure["ansible.builtin.command"]["argv"],
                         "{{ ['/usr/bin/python3', web_tls_tool] + web_tls_ensure_args }}")
        args = self.tasks["Assemble the certificate tool arguments shared by this play and the renewal timer"]
        text = args["ansible.builtin.set_fact"]["web_tls_ensure_args"]
        for flag in ("--key-group', 'caddy'", "--dns=", "--ip=", "--permit-dns=", "--permit-ip=", "--https-url"):
            self.assertIn(flag, text)

    def test_ports_and_nginx_change_only_with_start(self):
        for name in ("List what nginx serves", "Start and enable the web proxy", "Enable the daily renewal check",
                     "Verify the web UI over HTTPS with the CA pinned"):
            self.assertIn("web_tls_start | bool", self.tasks[name]["when"], name)
        guard = self.tasks["Refuse to stop an nginx that serves more than Debian's default site"]
        self.assertIn("reject('equalto', '/etc/nginx/sites-enabled/default')", guard["ansible.builtin.assert"]["that"][0])
        self.assertIn("Refuse to compete with the package's own Caddy service for the ports", self.tasks)

    def test_only_the_public_certificate_leaves_the_pi(self):
        fetches = [task for task in self.play["tasks"] if "ansible.builtin.fetch" in task]
        self.assertEqual([task["ansible.builtin.fetch"]["src"] for task in fetches], ["{{ web_tls_dir }}/ca/ca.crt"])
        self.assertNotIn("ca.key", (ROOT / "ansible/playbooks/deploy-web-tls.yml").read_text())

    def test_the_https_check_pins_the_ca(self):
        check = self.tasks["Verify the web UI over HTTPS with the CA pinned"]["ansible.builtin.uri"]
        self.assertEqual((check["ca_path"], check["validate_certs"]), ("{{ web_tls_dir }}/ca/ca.crt", True))

    def test_rollback_runs_only_on_request_and_keeps_the_ca(self):
        rollback = [task for task in self.play["tasks"] if "rollback" in task.get("tags", [])]
        self.assertEqual(len(rollback), 5)
        for task in rollback:
            self.assertIn("never", task["tags"], task["name"])
        self.assertFalse(any("web_tls_dir }}/ca" in yaml.safe_dump(task) for task in rollback), "devices stay trusted")
        reenable = self.tasks["Re-enable the nginx default site"]
        self.assertEqual(reenable["when"], "web_tls_nginx_marker.stat.exists", "only if this play disabled it")


@unittest.skipUnless(ANSIBLE.is_file(), "local Ansible required")
class WebTlsAnsibleTests(unittest.TestCase):
    def test_syntax(self):
        result = subprocess.run([str(ANSIBLE), "--syntax-check", "-i", str(ROOT / "ansible/inventory.yaml"),
                                 str(ROOT / "ansible/playbooks/deploy-web-tls.yml")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_inputs(self, **inputs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inventory.yml").write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "ansible_host": "192.168.1.105", "web_tls_dns_names": ["musicpi.local", "musicpi"], **inputs}}}}))
            (root / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False, "tasks": [
                {"ansible.builtin.import_role": {"name": str(ROLE), "defaults_from": "web_tls"}},
                {"ansible.builtin.assert": {"that": ["web_tls_https_port is integer", "web_tls_start is boolean"]}}]}]))
            return subprocess.run([str(ANSIBLE), "-i", str(root / "inventory.yml"), str(root / "play.yml")],
                                  capture_output=True, text=True)

    def test_inputs(self):
        self.assertEqual(self.run_inputs(web_tls_https_port="443", web_tls_start="false").returncode, 0)
        for bad in ({"web_tls_dns_names": ["192.168.1.105"]}, {"web_tls_ip_addresses": ["musicpi.local"]},
                    {"web_tls_permitted_ips": ["not-a-network"]}, {"web_tls_http_port": 443},
                    {"web_tls_upstream_port": 443}, {"web_tls_server_days": 900}, {"web_tls_start": "maybe"}):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.run_inputs(**bad).returncode, 0)


if __name__ == "__main__":
    unittest.main()
