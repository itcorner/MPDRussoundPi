"""Web endpoints for installing updates, end to end against the real updater socket server."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from test_admin_auth import FAST, PASSWORD
from test_deployment_updater import FakeTransaction, SPEC, candidate, INSTALLED, NEWER
from tool.deployment_updater import Updater, UpdaterServer
from web import admin_auth
from web.admin_auth import AdminAuth, hash_password
from web.russound_server import RussoundHTTPServer
from web.updater_client import UpdaterClient, sanitize_status


class PageFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "evidence").mkdir(mode=0o700)
        password = self.root / "admin.scrypt"
        password.write_text(hash_password(PASSWORD, **FAST) + "\n")
        self.clock = [1_800_000_000.0]
        self.auth = AdminAuth(password, None, clock=lambda: self.clock[0])
        self.transaction = FakeTransaction()
        self.updater = Updater(SPEC, self.root / "evidence", self.root / "state.json",
                               resolve=lambda gateway, platform: candidate(), committed=lambda: candidate(INSTALLED),
                               transaction=self.transaction)
        short = tempfile.mkdtemp(prefix="upd", dir="/tmp")
        self.addCleanup(shutil.rmtree, short, True)
        self.socket = Path(short) / "u.sock"
        server = UpdaterServer(self.socket, self.updater, None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.web = RussoundHTTPServer(None, None, admin=self.auth, updater=UpdaterClient(self.socket))
        self.addCleanup(self.web.server_close)
        self.client = self.web.app.test_client()

    def login(self):
        self.assertEqual(self.client.post("/api/admin/login", json={"password": PASSWORD}).status_code, 200)
        return {admin_auth.CSRF_HEADER: self.client.get("/api/admin/session").json["csrf_token"]}

    def wait_phase(self, *phases):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            body = self.client.get("/api/status/update-operation").json
            if body["phase"] in phases:
                return body
            time.sleep(0.02)
        self.fail(f"phase never became {phases}")

    def test_review_confirm_and_finish(self):
        headers = self.login()
        started = self.client.post("/api/admin/update/review", json={}, headers=headers)
        self.assertEqual(started.status_code, 202)
        operation = started.json["operation"]
        public = self.wait_phase("awaiting_approval")
        self.assertNotIn("review", public, "the confirmation digest is never public")
        self.assertEqual(public["candidate_revision"], NEWER)
        pending = self.client.get("/api/admin/update").json["review"]
        self.assertEqual((pending["operation"], pending["revision"]), (operation, NEWER))
        self.assertIn("not disaster recovery", pending["backup"])
        wrong = self.client.post("/api/admin/update/approve", json={"operation": operation, "digest": "sha256:x"},
                                 headers=headers)
        self.assertEqual(wrong.status_code, 409)
        approved = self.client.post("/api/admin/update/approve",
                                    json={"operation": operation, "digest": pending["digest"]}, headers=headers)
        self.assertEqual(approved.status_code, 202)
        self.wait_phase("running")
        self.transaction.release.set()
        self.assertEqual(self.wait_phase("succeeded")["phase"], "succeeded")

    def test_actions_need_login_csrf_and_a_recent_password(self):
        self.assertEqual(self.client.post("/api/admin/update/review", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/update").status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post("/api/admin/update/review", json={}).status_code, 403, "CSRF token required")
        self.clock[0] += admin_auth.REAUTH_SECONDS + 1
        stale = self.client.post("/api/admin/update/review", json={}, headers=headers)
        self.assertEqual((stale.status_code, stale.json["error"]["code"]), (403, "reauthenticate"))
        self.assertEqual(self.updater.status()["phase"], "idle", "nothing started without a recent password")

    def test_cancel_and_restore_paths(self):
        headers = self.login()
        operation = self.client.post("/api/admin/update/review", json={}, headers=headers).json["operation"]
        self.wait_phase("awaiting_approval")
        self.assertEqual(self.client.post("/api/admin/update/cancel", json={"operation": operation},
                                          headers=headers).status_code, 202)
        self.assertEqual(self.wait_phase("cancelled")["error"], "Cancelled.")
        refused = self.client.post("/api/admin/update/restore", json={"operation": operation}, headers=headers)
        self.assertEqual(refused.status_code, 409)

    def test_installations_without_the_updater(self):
        web = RussoundHTTPServer(None, None, admin=self.auth, updater=UpdaterClient(self.root / "absent.sock"))
        try:
            client = web.app.test_client()
            self.assertEqual(client.get("/api/status/update-operation").json, {"api_version": 1, "configured": False,
                                                                               "phase": "idle"})
            client.post("/api/admin/login", json={"password": PASSWORD})
            csrf = client.get("/api/admin/session").json["csrf_token"]
            response = client.post("/api/admin/update/review", json={}, headers={admin_auth.CSRF_HEADER: csrf})
            self.assertEqual(response.status_code, 503)
        finally:
            web.server_close()

    def test_sanitize_drops_unknown_fields_and_hostile_text(self):
        status = sanitize_status({"phase": "weird", "error": "bad\\x00\\x1b[31m", "secret": "x",
                                  "review": {"digest": "d", "summary": {}}}, include_review=False)
        self.assertEqual(status["phase"], "idle")
        self.assertNotIn("secret", status)
        self.assertNotIn("review", status)


if __name__ == "__main__":
    unittest.main()
