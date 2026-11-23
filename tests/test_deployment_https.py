from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from tool import deployment_compose as compose
from tool import web_tls
from tool.deployment_compose import ComposeError


class Handler(BaseHTTPRequestHandler):
    status = 200
    seen: list[tuple[str, str]] = []

    def do_GET(self):  # noqa: N802 - http.server API
        Handler.seen.append((self.path, self.headers["Host"]))
        self.send_response(Handler.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "openssl required")
class HttpsRouteCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def serve(self, tls_dir: Path, dns: str) -> int:
        args = web_tls.main.__globals__["argparse"].Namespace(
            dir=str(tls_dir), dns=[dns], ip=["192.168.1.105"], permit_dns=None, permit_ip=None, days=397,
            renew_days=60, key_group=None, label="MPDRussoundPi", identifier="x", https_url=None, restart_unit=None)
        web_tls.ensure(args)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_dir / "server.crt", tls_dir / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def check(self, tls_dir: Path, ca_dir: Path, port: int, name: str = "musicpi.local") -> None:
        status = self.root / "status.json"
        status.write_text(json.dumps({"https_url": f"https://{name}:{port}/"}))
        with mock.patch.object(compose, "WEB_TLS_STATUS", str(status)), \
                mock.patch.object(compose, "WEB_TLS_CA", str(ca_dir / "ca" / "ca.crt")):
            compose.https_route_check()

    def test_passes_through_the_proxy_with_the_local_ca_and_the_published_name(self):
        port = self.serve(self.root / "tls", "musicpi.local")
        Handler.status, Handler.seen = 200, []
        self.check(self.root / "tls", self.root / "tls", port)
        self.assertEqual(Handler.seen, [("/api/rooms", f"musicpi.local:{port}")])

    def test_fails_for_another_ca_another_name_or_an_error_status(self):
        port = self.serve(self.root / "tls", "musicpi.local")
        web_tls.ensure(web_tls.main.__globals__["argparse"].Namespace(
            dir=str(self.root / "other"), dns=["musicpi.local"], ip=[], permit_dns=None, permit_ip=None, days=397,
            renew_days=60, key_group=None, label="x", identifier="x", https_url=None, restart_unit=None))
        with self.assertRaises(ssl.SSLError):
            self.check(self.root / "tls", self.root / "other", port)
        with self.assertRaises(ssl.SSLError):
            self.check(self.root / "tls", self.root / "tls", port, name="musicpi")
        Handler.status = 503
        try:
            with self.assertRaisesRegex(ComposeError, "did not return 200"):
                self.check(self.root / "tls", self.root / "tls", port)
        finally:
            Handler.status = 200


if __name__ == "__main__":
    unittest.main()
