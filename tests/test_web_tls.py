from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tool import web_tls
from tool.web_tls import TlsError


def ensure(directory: Path, *extra: str, dns=("musicpi.local", "musicpi"), ips=("192.168.1.105",)) -> dict:
    argv = ["ensure", "--dir", str(directory)]
    for name in dns:
        argv += ["--dns", name]
    for address in ips:
        argv += ["--ip", address]
    return web_tls.ensure(_parse(argv + list(extra)))


def _parse(argv):
    """The real command-line parser, without running the command."""
    captured = {}

    def fake(args):
        captured["args"] = args
        return {}

    with mock.patch.object(web_tls, "ensure", fake), mock.patch("builtins.print"):
        web_tls.main(argv)
    return captured["args"]


def openssl_ok(*args: str) -> bool:
    return subprocess.run(["openssl", *args], capture_output=True).returncode == 0


@unittest.skipUnless(shutil.which("openssl"), "openssl required")
class WebTlsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name) / "tls"

    def test_first_run_creates_a_constrained_ca_and_a_verifiable_server_certificate(self):
        report = ensure(self.dir)
        self.assertTrue(report["ca_created"])
        self.assertEqual(report["reason"], "no server certificate")
        self.assertEqual(report["server"], {"dns": ["musicpi", "musicpi.local"], "ips": ["192.168.1.105"],
                                            "not_after": report["server"]["not_after"]})
        ca, cert = self.dir / "ca/ca.crt", self.dir / "server.crt"
        text = subprocess.run(["openssl", "x509", "-in", str(ca), "-noout", "-text"],
                              capture_output=True, text=True).stdout
        self.assertIn("Name Constraints: critical", text)
        for permitted in ("DNS:musicpi.local", "IP:10.0.0.0/255.0.0.0", "IP:172.16.0.0/255.240.0.0",
                          "IP:192.168.0.0/255.255.0.0"):
            self.assertIn(permitted, text)
        self.assertIn("CA:TRUE, pathlen:0", text)
        self.assertTrue(openssl_ok("verify", "-x509_strict", "-purpose", "sslserver", "-CAfile", str(ca), str(cert)))
        self.assertTrue(openssl_ok("x509", "-in", str(cert), "-noout", "-checkend", str(396 * 86400)))
        self.assertFalse(openssl_ok("x509", "-in", str(cert), "-noout", "-checkend", str(398 * 86400)))
        self.assertEqual(os.stat(self.dir / "ca/ca.key").st_mode & 0o777, 0o400)
        self.assertEqual(os.stat(self.dir / "ca").st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.dir / "server.key").st_mode & 0o777, 0o640)
        self.assertEqual(report["ca"]["sha256_fingerprint"], web_tls.status(self.dir)["ca"]["sha256_fingerprint"])

    def test_second_run_changes_nothing_and_the_ca_is_never_replaced(self):
        ensure(self.dir)
        ca_before, server_before = (self.dir / "ca/ca.crt").read_bytes(), (self.dir / "server.crt").read_bytes()
        report = ensure(self.dir)
        self.assertFalse(report["ca_created"] or report["server_issued"])
        self.assertEqual((self.dir / "server.crt").read_bytes(), server_before)
        renamed = ensure(self.dir, dns=("musicpi.local",), ips=("192.168.1.106",))
        self.assertEqual(renamed["reason"], "names changed")
        self.assertEqual((self.dir / "ca/ca.crt").read_bytes(), ca_before, "an address change keeps the CA")

    def test_renews_inside_the_threshold_with_a_new_key(self):
        ensure(self.dir)
        key_before = (self.dir / "server.key").read_bytes()
        report = ensure(self.dir, "--renew-days", "398", "--days", "400")
        self.assertEqual(report["reason"], "expires within 398 days")
        self.assertNotEqual((self.dir / "server.key").read_bytes(), key_before)

    def test_names_outside_the_constraints_are_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(TlsError, "outside the CA's permitted ranges"):
            ensure(self.dir, ips=("203.0.113.5",))
        self.assertFalse((self.dir / "ca").exists())
        ensure(self.dir)
        with self.assertRaisesRegex(TlsError, "does not verify"):
            ensure(self.dir, dns=("evil.example",))
        self.assertEqual(web_tls.status(self.dir)["server"]["dns"], ["musicpi", "musicpi.local"],
                         "the working certificate stays in place")

    def test_the_constraints_hold_for_a_certificate_signed_outside_this_tool(self):
        ensure(self.dir)
        scratch = self.dir.parent
        subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                        "-keyout", str(scratch / "evil.key"), "-out", str(scratch / "evil.csr"), "-subj", "/CN=x"],
                       check=True, capture_output=True)
        (scratch / "ext").write_text("subjectAltName=DNS:bank.example\nextendedKeyUsage=serverAuth\n")
        subprocess.run(["openssl", "x509", "-req", "-in", str(scratch / "evil.csr"), "-CA", str(self.dir / "ca/ca.crt"),
                        "-CAkey", str(self.dir / "ca/ca.key"), "-days", "30", "-extfile", str(scratch / "ext"),
                        "-out", str(scratch / "evil.crt")], check=True, capture_output=True)
        self.assertFalse(openssl_ok("verify", "-CAfile", str(self.dir / "ca/ca.crt"), str(scratch / "evil.crt")))

    def test_half_a_ca_is_never_completed(self):
        ensure(self.dir)
        (self.dir / "ca/ca.key").chmod(0o600)
        (self.dir / "ca/ca.key").unlink()
        with self.assertRaisesRegex(TlsError, "Only half of the CA"):
            ensure(self.dir)

    def test_public_download_page_profile_and_certificate(self):
        report = ensure(self.dir)
        public = self.dir / "public"
        self.assertEqual((public / "mpdrussoundpi-ca.crt").read_text(), (self.dir / "ca/ca.crt").read_text())
        page = (public / "index.html").read_text()
        self.assertIn(report["ca"]["sha256_fingerprint"], page)
        self.assertIn('href="https://musicpi.local/"', page)
        self.assertNotIn("PRIVATE KEY", page)
        profile = plistlib.loads((public / "mpdrussoundpi-ca.mobileconfig").read_bytes())
        payload = profile["PayloadContent"][0]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        der = subprocess.run(["openssl", "x509", "-in", str(self.dir / "ca/ca.crt"), "-outform", "DER"],
                             capture_output=True, check=True).stdout
        self.assertEqual(payload["PayloadContent"], der)
        self.assertEqual(sorted(path.name for path in public.iterdir()),
                         ["index.html", "mpdrussoundpi-ca.crt", "mpdrussoundpi-ca.mobileconfig", "status.json"])
        facts = json.loads((public / "status.json").read_text())
        self.assertEqual(facts, {"schema_version": 1, "https_url": "https://musicpi.local/",
                                 "server_not_after": report["server"]["not_after"],
                                 "server_dns": ["musicpi", "musicpi.local"], "server_ips": ["192.168.1.105"],
                                 "ca_sha256_fingerprint": report["ca"]["sha256_fingerprint"]})

    def test_the_proxy_restarts_only_when_a_certificate_was_issued(self):
        real = subprocess.run

        def run(argv, **kwargs):
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, 0)
            return real(argv, **kwargs)

        with mock.patch.object(web_tls.subprocess, "run", side_effect=run) as spy:
            ensure(self.dir, "--restart-unit", "web-proxy.service")
            ensure(self.dir, "--restart-unit", "web-proxy.service")
        restarts = [call.args[0] for call in spy.call_args_list if call.args[0][0] == "systemctl"]
        # Not a reload: Caddy 2.6 ignores a reload with unchanged configuration (found on the Pi).
        self.assertEqual(restarts, [["systemctl", "restart", "web-proxy.service"]])

    def test_input_validation(self):
        for value in ("bad name", "-x.local", "1.2.3"):
            with self.subTest(value=value), self.assertRaises(TlsError):
                web_tls.dns_name(value)
        with self.assertRaises(TlsError):
            web_tls.ip_network("192.168.1.5/16")
        self.assertTrue(web_tls.dns_permitted("a.musicpi.local", "musicpi.local"))
        self.assertFalse(web_tls.dns_permitted("evilmusicpi.local", "musicpi.local"))
        with self.assertRaisesRegex(TlsError, "825"):
            ensure(self.dir, "--days", "900")


if __name__ == "__main__":
    unittest.main()
