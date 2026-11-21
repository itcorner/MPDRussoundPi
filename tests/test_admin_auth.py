from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web import admin_auth
from web.admin_auth import AdminAuth, hash_password, verify_password
from web.russound_server import RussoundHTTPServer

PASSWORD = "correct horse battery staple"
FAST = {"n": 2 ** 14, "r": 8, "p": 1}


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.password_file = self.root / "russound-admin.scrypt"
        self.password_file.write_text(hash_password(PASSWORD, **FAST) + "\n")
        self.lockout_file = self.root / "admin-lockout.json"
        self.audit_file = self.root / "admin-audit.log"
        self.clock = [1_800_000_000.0]

    def auth(self):
        return AdminAuth(self.password_file, self.lockout_file, clock=lambda: self.clock[0], audit_file=self.audit_file)


class HashTests(unittest.TestCase):
    def test_round_trip_and_malformed_hashes(self):
        encoded = hash_password(PASSWORD, **FAST)
        self.assertTrue(encoded.startswith("scrypt$16384$8$1$"))
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password(PASSWORD + "x", encoded))
        for bad in ("", "plain", "scrypt$1$8$1$AA==$AA==", "bcrypt$16384$8$1$AA==$AA==", encoded.replace("$8$", "$99$")):
            self.assertFalse(verify_password(PASSWORD, bad), bad)
        self.assertNotEqual(hash_password(PASSWORD, **FAST), encoded, "every hash has its own salt")


class LockoutAndSessionTests(Fixture):
    def test_login_creates_a_session_with_csrf(self):
        auth = self.auth()
        result, session_id, _ = auth.login(PASSWORD, "192.0.2.5")
        self.assertEqual(result, "ok")
        described = auth.describe(session_id)
        self.assertTrue(described["authenticated"])
        self.assertEqual(auth.authorize(session_id, described["csrf_token"]), None)
        self.assertEqual(auth.authorize(session_id, None), "csrf")
        self.assertEqual(auth.authorize(session_id, "wrong"), "csrf")
        self.assertEqual(auth.authorize("forged", described["csrf_token"]), "unauthenticated")

    def test_five_failures_lock_for_fifteen_minutes_and_attempts_extend_it(self):
        auth = self.auth()
        for _ in range(4):
            self.assertEqual(auth.login("wrong", "a")[0], "invalid")
        self.assertEqual(auth.login("wrong", "b")[0], "locked", "the budget is global, not per client")
        self.assertEqual(auth.login(PASSWORD, "c")[0], "locked", "even the right password is refused while locked")
        self.clock[0] += admin_auth.LOCK_SECONDS - 1
        self.assertEqual(auth.login(PASSWORD, "c")[0], "locked", "the attempt during the lock extended it")
        self.clock[0] += admin_auth.LOCK_SECONDS + 1
        self.assertEqual(auth.login(PASSWORD, "c")[0], "ok")
        self.assertEqual(auth.locked_for(), 0)

    def test_lockout_survives_a_restart_and_clock_rollback(self):
        auth = self.auth()
        for _ in range(5):
            auth.login("wrong", "a")
        restarted = self.auth()
        self.assertGreater(restarted.locked_for(), 0)
        self.clock[0] -= 3600  # clock went backwards
        self.assertEqual(restarted.login(PASSWORD, "a")[0], "locked")

    def test_a_local_password_reset_revokes_sessions_and_clears_the_lockout(self):
        auth = self.auth()
        _, session_id, _ = auth.login(PASSWORD, "a")
        for _ in range(5):
            auth.login("wrong", "b")
        self.password_file.write_text(hash_password("a brand new passphrase", **FAST) + "\n")
        os.utime(self.password_file, ns=(1, 1))
        self.assertIsNone(auth.session(session_id))
        self.assertEqual(auth.login("a brand new passphrase", "c")[0], "ok")

    def test_idle_and_absolute_expiry(self):
        auth = self.auth()
        _, session_id, _ = auth.login(PASSWORD, "a")
        self.clock[0] += admin_auth.IDLE_SECONDS + 1
        self.assertIsNone(auth.session(session_id))
        _, session_id, _ = auth.login(PASSWORD, "a")
        for _ in range(int(admin_auth.ABSOLUTE_SECONDS / (admin_auth.IDLE_SECONDS - 60)) + 2):
            self.clock[0] += admin_auth.IDLE_SECONDS - 60
            if auth.session(session_id) is None:
                break
        self.assertIsNone(auth.session(session_id), "activity cannot extend a session beyond eight hours")

    def test_disruptive_actions_need_a_recent_password(self):
        auth = self.auth()
        _, session_id, _ = auth.login(PASSWORD, "a")
        csrf = auth.describe(session_id)["csrf_token"]
        self.assertIsNone(auth.authorize(session_id, csrf, recent=True))
        self.clock[0] += admin_auth.REAUTH_SECONDS + 1
        self.assertEqual(auth.authorize(session_id, csrf, recent=True), "reauthenticate")
        self.assertEqual(auth.reauthenticate(session_id, "wrong", "a")[0], "invalid")
        self.assertEqual(auth.reauthenticate(session_id, PASSWORD, "a")[0], "ok")
        self.assertIsNone(auth.authorize(session_id, csrf, recent=True))
        self.assertEqual(auth.describe(session_id)["csrf_token"], csrf, "reauthentication keeps the session")

    def test_per_client_throttle(self):
        auth = self.auth()
        for _ in range(admin_auth.CLIENT_MAX_ATTEMPTS):
            self.assertEqual(auth.login(PASSWORD, "a")[0], "ok")
        result, _, retry = auth.login(PASSWORD, "a")
        self.assertEqual(result, "throttled")
        self.assertGreater(retry, 0)
        self.assertEqual(auth.login(PASSWORD, "b")[0], "ok")

    def test_unconfigured_and_audit_without_secrets(self):
        missing = AdminAuth(self.root / "absent", None)
        self.assertEqual(missing.login(PASSWORD, "a")[0], "not_configured")
        self.assertFalse(missing.describe(None)["configured"])
        auth = self.auth()
        _, session_id, _ = auth.login(PASSWORD, "a")
        auth.login("wrong", "a")
        auth.logout(session_id, "a")
        events = [json.loads(line)["event"] for line in self.audit_file.read_text().splitlines()]
        self.assertEqual(events, ["login", "login_failed", "logout"])
        self.assertNotIn(PASSWORD, self.audit_file.read_text())
        self.assertNotIn(session_id, self.audit_file.read_text())


class CommandTests(Fixture):
    def test_set_writes_a_private_hash_file(self):
        target = self.root / "new.scrypt"
        with patch("getpass.getpass", side_effect=[PASSWORD, PASSWORD]), patch.object(admin_auth, "SCRYPT_N", 2 ** 14), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(admin_auth.main(["set", "--file", str(target)]), 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o400)
        self.assertTrue(verify_password(PASSWORD, target.read_text()))

    def test_short_or_mismatching_passwords_change_nothing(self):
        target = self.root / "none.scrypt"
        for answers in (["short", "short"], [PASSWORD, PASSWORD + "!"]):
            with patch("getpass.getpass", side_effect=answers), self.assertRaises(SystemExit):
                admin_auth.main(["set", "--file", str(target)])
        self.assertFalse(target.exists())


class EndpointTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = RussoundHTTPServer(None, None, admin=self.auth())
        self.addCleanup(self.server.server_close)
        self.client = self.server.app.test_client()

    def login(self, password=PASSWORD):
        return self.client.post("/api/admin/login", json={"password": password})

    def test_login_sets_a_strict_cookie_and_logout_needs_the_csrf_token(self):
        self.assertFalse(self.client.get("/api/admin/session").json["authenticated"])
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/api/"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Secure", cookie, "plain HTTP for now (plan decision 6)")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        csrf = self.client.get("/api/admin/session").json["csrf_token"]
        self.assertEqual(self.client.post("/api/admin/logout", json={}).status_code, 403)
        out = self.client.post("/api/admin/logout", json={}, headers={admin_auth.CSRF_HEADER: csrf})
        self.assertEqual(out.status_code, 200)
        self.assertFalse(self.client.get("/api/admin/session").json["authenticated"])

    def test_failures_are_generic_then_locked(self):
        first = self.login("wrong")
        self.assertEqual((first.status_code, first.json["error"]["message"]), (401, "Login failed."))
        for _ in range(4):
            last = self.login("wrong")
        self.assertEqual(last.status_code, 429)
        self.assertIn("Retry-After", last.headers)

    def test_cross_site_login_is_rejected(self):
        response = self.client.post("/api/admin/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_privileged_routes_use_admin_required(self):
        @self.server.app.post("/api/test/privileged")
        def privileged():
            denied = self.server.admin_required(recent=True)
            return denied if denied is not None else {"done": True}

        self.assertEqual(self.client.post("/api/test/privileged", json={}).status_code, 401)
        self.login()
        csrf = self.client.get("/api/admin/session").json["csrf_token"]
        self.assertEqual(self.client.post("/api/test/privileged", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/test/privileged", json={}, headers={admin_auth.CSRF_HEADER: csrf}).json,
                         {"done": True})
        self.clock[0] += admin_auth.REAUTH_SECONDS + 1
        stale = self.client.post("/api/test/privileged", json={}, headers={admin_auth.CSRF_HEADER: csrf})
        self.assertEqual((stale.status_code, stale.json["error"]["code"]), (403, "reauthenticate"))
        again = self.client.post("/api/admin/reauthenticate", json={"password": PASSWORD}, headers={admin_auth.CSRF_HEADER: csrf})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.client.post("/api/test/privileged", json={}, headers={admin_auth.CSRF_HEADER: csrf}).status_code, 200)

    def test_password_never_reaches_logs_or_frontend_events(self):
        logger = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            self.login()
            self.login("wrong-" + PASSWORD)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertNotIn(PASSWORD, stream.getvalue())
        self.assertEqual(self.client.get("/api/status/history").json["recent_events"], [])

    def test_unconfigured_installation(self):
        server = RussoundHTTPServer(None, None, admin=AdminAuth(self.root / "absent", None))
        try:
            response = server.app.test_client().post("/api/admin/login", json={"password": PASSWORD})
            self.assertEqual(response.status_code, 503)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
