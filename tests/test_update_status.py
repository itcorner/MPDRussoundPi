from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mpdrussoundpi.common import imageregistry
from web import update_status
from web.update_status import UpdateChecker
from web.russound_server import RussoundHTTPServer

OURS = "a" * 40
THEIRS = "b" * 40
NOW = 1_800_000_000.0


def environ(**changes):
    values = {"RUSSOUND_BUILD_SOURCE": "ci", "RUSSOUND_BUILD_IMAGE": "itcornernet/mpdrussoundpi-web",
              "RUSSOUND_BUILD_VERSION": "dev-" + OURS, "RUSSOUND_BUILD_REVISION": OURS,
              "RUSSOUND_BUILD_TIME": "2027-01-15T08:00:00Z"}
    values.update(changes)
    return {key: value for key, value in values.items() if value is not None}


def published(version, revision, created="2027-01-16T08:00:00Z", source=imageregistry.SOURCE):
    return {"index_digest": "sha256:" + "1" * 64, "platform_digest": "sha256:" + "2" * 64, "config_digest": "sha256:" + "3" * 64,
            "labels": {"org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision,
                       "org.opencontainers.image.source": source},
            "created": created}


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.clock = [NOW]
        self.calls = []

    def checker(self, answer, **changes):
        def lookup(registry, repository, reference, platform):
            self.calls.append((repository, reference, platform))
            if isinstance(answer, Exception):
                raise answer
            return answer
        patcher = patch.object(update_status, "platform_config", side_effect=lookup)
        patcher.start()
        self.addCleanup(patcher.stop)
        return UpdateChecker(environ(**changes), registry_factory=object, clock=lambda: self.clock[0], machine="aarch64")

    def test_builds_that_cannot_be_compared_are_unsupported_and_never_query(self):
        for changes in ({"RUSSOUND_BUILD_SOURCE": "local"}, {"RUSSOUND_BUILD_SOURCE": None},
                        {"RUSSOUND_BUILD_IMAGE": "someone/else"}, {"RUSSOUND_BUILD_VERSION": "latest"}):
            checker = self.checker(published("dev-" + THEIRS, THEIRS), **changes)
            checker.check_once()
            with self.subTest(changes=changes):
                self.assertEqual(checker.snapshot()["state"], "unsupported")
                self.assertEqual(checker.request_check(), (False, 0))
        self.assertEqual(self.calls, [])
        off = self.checker(published("dev-" + THEIRS, THEIRS), RUSSOUND_UPDATE_CHECK="off")
        self.assertEqual(off.snapshot()["state"], "disabled")

    def test_development_image_compares_with_the_dev_tag(self):
        checker = self.checker(published("dev-" + THEIRS, THEIRS))
        self.assertEqual(checker.snapshot()["state"], "pending")
        checker.check_once()
        snapshot = checker.snapshot()
        self.assertEqual(self.calls, [("itcornernet/mpdrussoundpi-web", "dev", "linux/arm64")])
        self.assertEqual(snapshot["state"], "update_available")
        self.assertEqual(snapshot["result"]["available"]["version"], "dev-" + THEIRS)
        self.assertEqual(snapshot["result"]["available"]["link"],
                         f"https://hub.docker.com/r/itcornernet/mpdrussoundpi-web/tags?name=dev-{THEIRS}")

    def test_same_or_older_dev_tag_is_up_to_date(self):
        same = self.checker(published("dev-" + OURS, OURS))
        same.check_once()
        self.assertEqual(same.snapshot()["state"], "up_to_date")
        older = self.checker(published("dev-" + THEIRS, THEIRS, created="2027-01-14T08:00:00Z"))
        older.check_once()
        self.assertEqual(older.snapshot()["state"], "up_to_date", "a lagging moving tag is not an update")

    def test_releases_compare_versions_with_latest(self):
        for ours, theirs, expected in (("1.2.0", "1.3.0", "update_available"), ("1.2.0", "1.2.0", "up_to_date"),
                                       ("1.10.0", "1.9.0", "up_to_date"), ("1.2.0-rc.2", "1.2.0", "update_available"),
                                       ("1.2.0-rc.1", "1.1.9", "up_to_date")):
            self.calls.clear()
            checker = self.checker(published(theirs, THEIRS), RUSSOUND_BUILD_VERSION=ours)
            checker.check_once()
            with self.subTest(ours=ours, theirs=theirs):
                self.assertEqual(self.calls[0][1], "latest")
                self.assertEqual(checker.snapshot()["state"], expected)

    def test_failures_keep_the_last_result_and_never_claim_up_to_date(self):
        failing = self.checker(imageregistry.RegistryError("Registry metadata request failed: HTTP 503"))
        failing.check_once()
        snapshot = failing.snapshot()
        self.assertEqual((snapshot["state"], snapshot["check_failed"], snapshot["result"]), ("check_failed", True, None))
        self.assertEqual(snapshot["error"], update_status.FAILED_MESSAGE)
        self.assertNotIn("503", json.dumps(snapshot))
        checker = self.checker(published("dev-" + THEIRS, THEIRS))
        checker.check_once()
        with patch.object(update_status, "platform_config", side_effect=OSError("down")):
            self.clock[0] += 3600
            checker.check_once()
        snapshot = checker.snapshot()
        self.assertEqual((snapshot["state"], snapshot["check_failed"], snapshot["stale"]), ("update_available", True, True))

    def test_unexpected_labels_are_a_failure(self):
        for answer in (published("dev-" + THEIRS, THEIRS, source="https://example.invalid"),
                       published("dev-" + THEIRS, OURS), published("1.0.0", THEIRS)):
            checker = self.checker(answer)
            checker.check_once()
            with self.subTest(answer=answer["labels"]):
                self.assertEqual(checker.snapshot()["state"], "check_failed")

    def test_manual_checks_are_rate_limited_and_old_results_turn_stale(self):
        checker = self.checker(published("dev-" + OURS, OURS))
        self.assertEqual(checker.request_check(), (True, 0))
        checker.check_once()
        accepted, wait = checker.request_check()
        self.assertFalse(accepted)
        self.assertGreater(wait, 0)
        self.assertFalse(checker.snapshot()["manual_check"]["available"])
        self.clock[0] += update_status.MANUAL_MIN_INTERVAL_SECONDS + 1
        self.assertEqual(checker.request_check(), (True, 0))
        self.assertFalse(checker.snapshot()["stale"])
        self.clock[0] += update_status.STALE_AFTER_SECONDS + 1
        self.assertTrue(checker.snapshot()["stale"])


class RegistryResourceTests(unittest.TestCase):
    def test_only_project_tags_and_digests_are_requested(self):
        allowed = ["manifests/dev", "manifests/dev-" + OURS, "manifests/latest", "manifests/1.2", "manifests/1.2.3",
                   "manifests/1.2.3-rc.4", "manifests/sha256:" + "f" * 64, "blobs/sha256:" + "f" * 64]
        for resource in allowed:
            self.assertTrue(imageregistry.MANIFEST_RESOURCE.fullmatch(resource), resource)
        for resource in ("manifests/edge", "manifests/../x", "manifests/1.2.3-beta", "blobs/sha256:xyz", "tags/list"):
            self.assertIsNone(imageregistry.MANIFEST_RESOURCE.fullmatch(resource), resource)


class EndpointTests(unittest.TestCase):
    def server(self, answer):
        patcher = patch.object(update_status, "platform_config", side_effect=lambda *args: answer)
        patcher.start()
        self.addCleanup(patcher.stop)
        checker = UpdateChecker(environ(), registry_factory=object, machine="x86_64")
        server = RussoundHTTPServer(None, None, update_checker=checker)
        self.addCleanup(server.server_close)
        return server, checker

    def test_overview_reports_cached_results_and_flags_an_available_update(self):
        server, checker = self.server(published("dev-" + THEIRS, THEIRS))
        client = server.app.test_client()
        body = client.get("/api/status/overview").json
        self.assertNotIn("updates", [section["id"] for section in body["sections"]],
                         "update details live in the Administrator panel; the overview keeps only the flag")
        self.assertFalse(body["update_available"])
        checker.check_once()
        body = client.get("/api/status/overview").json
        self.assertTrue(body["update_available"])
        self.assertEqual(client.get("/api/status/updates").json["state"], "update_available")

    def test_manual_check_is_accepted_then_rate_limited_and_requires_same_origin(self):
        server, checker = self.server(published("dev-" + OURS, OURS))
        client = server.app.test_client()
        first = client.post("/api/status/updates/check", json={})
        self.assertEqual(first.status_code, 202)
        checker.check_once()
        second = client.post("/api/status/updates/check", json={})
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second.headers)
        cross = client.post("/api/status/updates/check", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(cross.status_code, 403)

    def test_default_server_never_contacts_a_registry(self):
        with patch.object(update_status, "platform_config", side_effect=AssertionError("no network in tests")):
            server = RussoundHTTPServer(None, None)
            try:
                body = server.app.test_client().get("/api/status/updates").json
                self.assertEqual(body["state"], "unsupported")
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
