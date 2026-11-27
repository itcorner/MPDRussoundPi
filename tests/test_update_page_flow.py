"""Web endpoints for installing updates, end to end against the real updater socket server."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_admin_auth import FAST, PASSWORD
from test_deployment_updater import FakeTransaction, SPEC, candidate, INSTALLED, NEWER
from tool.deployment_updater import Updater, UpdaterServer
from test_system_updates import FakeHost, Fixture as SystemFixture
from web import admin_auth
from web.admin_auth import AdminAuth, hash_password
from web.russound_server import RussoundHTTPServer
from web.updater_client import UpdaterClient, sanitize_status, sanitize_system


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
                               resolve=lambda gateway, platform, channel="dev": candidate(), committed=lambda: candidate(INSTALLED),
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

    def test_the_channel_is_chosen_by_the_administrator_and_shown_to_everyone(self):
        self.assertEqual(self.client.get("/api/status/update-operation").json["channel"], "development")
        self.assertEqual(self.client.post("/api/admin/update/channel", json={"channel": "stable"}).status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post("/api/admin/update/channel", json={"channel": "stable"}).status_code, 403,
                         "CSRF token required")
        with patch.object(self.web._updates, "request_check", return_value=(True, 0)) as check:
            changed = self.client.post("/api/admin/update/channel", json={"channel": "stable"}, headers=headers)
        self.assertEqual(changed.status_code, 202)
        check.assert_called_once_with(force=True)
        self.assertEqual(self.client.get("/api/status/update-operation").json["channel"], "stable")
        self.assertEqual(self.web._chosen_channel(), "stable")
        refused = self.client.post("/api/admin/update/channel", json={"channel": "beta"}, headers=headers)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("stable or development", refused.json["error"]["message"])

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

    def test_actions_need_login_and_csrf_and_only_restore_a_recent_password(self):
        self.assertEqual(self.client.post("/api/admin/update/review", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/update").status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post("/api/admin/update/review", json={}).status_code, 403, "CSRF token required")
        self.clock[0] += admin_auth.REAUTH_SECONDS + 1
        stale = self.client.post("/api/admin/update/restore", json={"operation": "x"}, headers=headers)
        self.assertEqual((stale.status_code, stale.json["error"]["code"]), (403, "reauthenticate"),
                         "a restore asks for the password again")
        self.assertEqual(self.updater.status()["phase"], "idle", "nothing started without a recent password")
        review = self.client.post("/api/admin/update/review", json={}, headers=headers)
        self.assertEqual(review.status_code, 202, "installing an update does not ask again")

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

    def test_sanitize_passes_only_the_cleanup_counts(self):
        def cleanup(value):
            return sanitize_status({"phase": "succeeded", "result": {"cleanup": value}}, include_review=False)["cleanup"]
        self.assertEqual(cleanup({"removed": 4, "freed_bytes": 650_000_000, "skipped": 1, "error": "x"}),
                         {"removed": 4, "freed_bytes": 650_000_000})
        for bad in (None, "4", {"removed": -1, "freed_bytes": 0}, {"removed": 1, "freed_bytes": "a lot"},
                    {"removed": True, "freed_bytes": 0}):
            with self.subTest(bad=bad):
                self.assertIsNone(cleanup(bad))
        review = sanitize_status({"phase": "awaiting_approval", "review": {"operation": "o", "digest": "d", "summary": {
            "cleanup": "Old images are removed."}}}, include_review=True)["review"]
        self.assertEqual(review["cleanup"], "Old images are removed.")

    def test_sanitize_passes_only_the_retention_counts(self):
        def retention(value):
            return sanitize_status({"phase": "succeeded", "result": {"retention": value}},
                                   include_review=False)["retention"]
        self.assertEqual(retention({"operations": 2, "removed": 6, "freed_bytes": 580_000_000, "error": "x",
                                    "paths": ["/var/lib/x"]}), {"operations": 2, "freed_bytes": 580_000_000})
        for bad in (None, "2", {"operations": -1, "freed_bytes": 0}, {"operations": 1, "freed_bytes": "a lot"},
                    {"operations": True, "freed_bytes": 0}, {"removed": 6, "freed_bytes": 0}):
            with self.subTest(bad=bad):
                self.assertIsNone(retention(bad))
        self.assertIsNone(sanitize_status({"phase": "idle"}, include_review=False)["retention"])
        review = sanitize_status({"phase": "awaiting_approval", "review": {"operation": "o", "digest": "d", "summary": {
            "retention": "Older backups are removed."}}}, include_review=True)["review"]
        self.assertEqual(review["retention"], "Older backups are removed.")

    def test_sanitize_passes_the_progress_only_while_running(self):
        good = {"step": 3, "steps": 5, "elapsed_seconds": 75, "step_elapsed_seconds": 10,
                "typical_seconds": [5, 60, 190, 20, 10]}
        def progress(value, phase="running"):
            return sanitize_status({"phase": phase, "progress": value}, include_review=False)["progress"]
        self.assertEqual(progress({**good, "operation": "dropped"}), good)
        self.assertIsNone(progress(good, "succeeded"))
        self.assertIsNone(progress({**good, "typical_seconds": [1, 2]})["typical_seconds"])
        self.assertIsNone(progress({**good, "typical_seconds": [1, 2, "3", 4, 5]})["typical_seconds"])
        for bad in (None, "3", {**good, "step": 6}, {**good, "step": 0}, {**good, "elapsed_seconds": -1},
                    {**good, "step": True}, {**good, "steps": None}):
            with self.subTest(bad=bad):
                self.assertIsNone(progress(bad))

    def test_sanitize_passes_the_restorable_backups(self):
        good = {"operation": "e98f54a1-625a-428f-b4d5-574d1b357557", "taken_at": "2026-11-23T00:04:18.238842+00:00",
                "revision": "a" * 40, "size_bytes": 108130296, "reason": "before the update that failed"}
        backups = sanitize_status({"phase": "failed", "backups": [good]}, include_review=False)["backups"]
        self.assertEqual(backups, [good])
        odd = sanitize_status({"phase": "failed", "backups": [
            {**good, "taken_at": "yesterday", "revision": "nope", "size_bytes": -1, "extra": "x"},
            {"taken_at": good["taken_at"]}, "text"] + [good] * 9}, include_review=False)["backups"]
        self.assertEqual(odd[0], {"operation": good["operation"], "taken_at": None, "revision": None,
                                  "size_bytes": None, "reason": good["reason"]})
        self.assertEqual(len(odd), 3, "entries without an operation are dropped and at most five are read")
        self.assertEqual(sanitize_status({"phase": "idle", "backups": "x"}, include_review=False)["backups"], [])


if __name__ == "__main__":
    unittest.main()


class SystemPageFlowTests(SystemFixture):
    """The system endpoints against the real updater socket with a SystemUpdates on fake host commands."""

    def setUp(self):
        super().setUp()
        password = self.root / "admin.scrypt"
        password.write_text(hash_password(PASSWORD, **FAST) + "\n")
        self.auth = AdminAuth(password, None)
        updater = Updater(SPEC, self.evidence, self.root / "state.json", resolve=lambda gateway, platform, channel="dev": candidate(),
                          committed=lambda: candidate(INSTALLED), transaction=FakeTransaction())
        updater.system = self.make()
        self.system = updater.system
        short = tempfile.mkdtemp(prefix="sys", dir="/tmp")
        self.addCleanup(shutil.rmtree, short, True)
        server = UpdaterServer(Path(short) / "u.sock", updater, None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.web = RussoundHTTPServer(None, None, admin=self.auth, updater=UpdaterClient(Path(short) / "u.sock"))
        self.addCleanup(self.web.server_close)
        self.client = self.web.app.test_client()

    def test_counts_are_public_and_details_need_the_login(self):
        self.system.recompute()
        public = self.client.get("/api/status/system-updates").json
        self.assertEqual((public["check"]["upgrades"], public["check"]["security"]), (4, 1))
        self.assertNotIn("packages", public["check"])
        self.assertNotIn("review", public)
        self.assertEqual(self.client.get("/api/admin/system-updates").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/system-updates/review", json={"kind": "install"}).status_code, 401)
        self.client.post("/api/admin/login", json={"password": PASSWORD})
        headers = {admin_auth.CSRF_HEADER: self.client.get("/api/admin/session").json["csrf_token"]}
        details = self.client.get("/api/admin/system-updates").json
        self.assertIn("openssh-server", [item["name"] for item in details["check"]["packages"] if item["security"]])
        self.assertEqual(self.client.post("/api/admin/system-updates/reboot", json={}, headers=headers).status_code, 404)
        self.assertEqual(self.client.post("/api/admin/system-updates/review", json={"kind": "install"}).status_code, 403,
                         "CSRF token required")
        started = self.client.post("/api/admin/system-updates/review", json={"kind": "install"}, headers=headers)
        self.assertEqual(started.status_code, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            review = self.client.get("/api/admin/system-updates").json.get("review")
            if review:
                break
            time.sleep(0.02)
        self.assertEqual((review["action"], review["upgrades"], review["docker"]), ("install", 4, True))
        with self.system._lock:
            self.assertIsNotNone(self.system.state["review"]["expires_at"], "the administrator's page started the window")
        restart = self.client.post("/api/admin/system-updates/review", json={"kind": "restart"}, headers=headers)
        self.assertEqual(restart.status_code, 409, "one system operation at a time")
        cancelled = self.client.post("/api/admin/system-updates/cancel", json={"operation": review["operation"]},
                                     headers=headers)
        self.assertEqual(cancelled.status_code, 202)
        self.assertFalse(self.host.started.is_set())

    def test_the_overview_icon_includes_pending_system_updates(self):
        self.system.recompute()
        self.assertTrue(self.web._system_updates_pending())
        self.host.simulation = ""
        self.system.recompute()
        self.assertFalse(self.web._system_updates_pending())

    def test_a_long_restart_reason_is_kept_whole(self):
        reason = ("updated since the last start: firmware-atheros, firmware-brcm80211, firmware-libertas, "
                  "firmware-mediatek, firmware-realtek")
        details = sanitize_system({"check": {"restart": {"needed": True, "reasons": [reason]}}}, include_details=True)
        self.assertEqual(details["check"]["restart_reasons"], [reason])

    def test_sanitize_system_drops_unknown_fields_and_hostile_text(self):
        hostile = {"phase": "rm -rf", "kind": "x", "secret": 1, "error": "bad\x1b[31m",
                   "check": {"state": "weird", "upgrades": -1, "security": "3", "packages": [{"name": "<b>", "x": 1}],
                             "restart": {"needed": "yes"}}}
        public = sanitize_system(hostile, include_details=False)
        self.assertEqual((public["phase"], public["kind"], public["check"]["state"]), ("idle", None, "pending"))
        self.assertEqual((public["check"]["upgrades"], public["check"]["security"]), (None, None))
        self.assertFalse(public["check"]["restart_needed"])
        self.assertNotIn("secret", public)
        details = sanitize_system(hostile, include_details=True)
        self.assertEqual(details["check"]["packages"], [{"name": "<b>", "from": None, "to": None, "security": False}])

