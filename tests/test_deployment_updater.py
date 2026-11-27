from __future__ import annotations

import json
import shutil
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from tool import deployment_updater as updater_module
from tool.deployment_channel import ChannelError
from tool.deployment_state import StateError
from tool.deployment_updater import Updater, UpdaterError, UpdaterServer, request

INSTALLED = "a" * 40
NEWER = "b" * 40
ACTOR = {"session": "3f467261", "client": "192.0.2.5"}
SPEC = {"deployment": "russound-dev", "gateway": "managed", "platform": "linux/arm64"}


def candidate(revision=NEWER):
    return {"kind": "development-image-candidate", "revision": revision,
            "images": {"web": {"reference": "itcornernet/mpdrussoundpi-web@sha256:" + "1" * 64},
                       "ser2net": {"reference": "itcornernet/mpdrussoundpi-ser2net@sha256:" + "2" * 64}}}


class FakeTransaction:
    """Stands in for the operator's controller/serve pair and calls approval like it does."""

    def __init__(self, outcome="complete", backup=True):
        self.outcome, self.backup = outcome, backup
        self.cleanup = None
        self.retention = None
        self.requests, self.approvals = [], []
        self.release = threading.Event()

    def __call__(self, request, approve, evidence, **recovery):
        self.requests.append((request, recovery))
        operation = request["operation"]
        plan = {"operation": operation, "selection": request.get("selection") or {},
                "scope": {"services": ["russound-ser2net", "russound-web"],
                          "layout_change": "adds the read-only updater socket mount to the web service"}}
        digest = "sha256:" + "c" * 64
        answer = approve({"event": "review", "plan": plan, "digest": digest})
        self.approvals.append(answer)
        if answer != digest:
            raise ChannelError("Operation was declined; target must not interrupt services")
        if self.backup and request["action"] == "deploy":
            identity = evidence / (operation + ".backup-identity.json")
            identity.write_text(json.dumps({"operation": operation, "manifest_sha256": "d" * 64}))
            identity.chmod(0o600)
            (evidence / operation).mkdir(mode=0o700)
            manifest = evidence / operation / "manifest.json"
            manifest.write_text(json.dumps({"operation": operation, "created_at": "2026-11-23T00:04:18.238842+00:00",
                                            "total_bytes": 108130296, "entries": []}))
            manifest.chmod(0o600)
        self.release.wait(5)
        if self.outcome == "fail":
            raise ChannelError("Target operation failed")
        return {"event": "recovered" if request["action"] == "recover" else "complete",
                "required": {"containers": "passed", "http": "passed"},
                **({"cleanup": self.cleanup} if request["action"] == "deploy" and self.cleanup is not None else {}),
                **({"retention": self.retention} if request["action"] == "deploy" and self.retention is not None else {})}


def unreadable_target():
    raise StateError("Cannot inspect deployment state; treat the outcome as unknown")


def target_record(pending=None, reserved=None):
    """The Pi's deployment record as StateStore().status() returns it, reduced to what the updater reads."""
    return {"schema_version": 1, "blocked": bool(pending or reserved),
            "reservation": {"schema_version": 1, "deployment": "russound-dev", "operation": reserved} if reserved else None,
            "deployments": {"russound-dev": {"committed": None, "pending": pending}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.state = self.root / "state.json"
        self.installed = candidate(INSTALLED)
        self.clock = [1_800_000_000.0]
        self.target = unreadable_target

    def make(self, transaction=None, resolve=None, clock=None):
        self.transaction = transaction or FakeTransaction()
        return Updater(SPEC, self.evidence, self.state, resolve=resolve or (lambda gateway, platform, channel="dev": candidate()),
                       committed=lambda: self.installed, transaction=self.transaction, clock=clock or time.time,
                       target=lambda: self.target())

    def wait_for(self, updater, *phases, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if updater.status()["phase"] in phases:
                return updater.status()
            time.sleep(0.01)
        self.fail(f"phase {updater.status()['phase']} never became {phases}")


class UpdateFlowTests(Fixture):
    def test_review_approve_and_succeed(self):
        updater = self.make()
        operation = updater.handle({"op": "review", "actor": ACTOR})["operation"]
        status = self.wait_for(updater, "awaiting_approval")
        review = status["review"]
        self.assertEqual(review["operation"], operation)
        self.assertEqual(review["summary"]["revision"], NEWER)
        self.assertIn("not disaster recovery", review["summary"]["backup"])
        self.assertIn("layout_change", review["summary"])
        self.assertTrue(status["busy"])
        with self.assertRaisesRegex(UpdaterError, "in progress"):
            updater.handle({"op": "review", "actor": ACTOR})
        with self.assertRaisesRegex(UpdaterError, "does not match"):
            updater.handle({"op": "approve", "operation": operation, "digest": "sha256:" + "0" * 64, "actor": ACTOR})
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        self.wait_for(updater, "running")
        record = json.loads(self.state.read_text())
        self.assertEqual((record["phase"], record["approved_by"]), ("running", ACTOR),
                         "the dispatch boundary is on disk before the transaction continues")
        self.transaction.release.set()
        status = self.wait_for(updater, "succeeded")
        self.assertEqual(status["result"]["checks"], {"containers": "passed", "http": "passed"})
        request_sent, _ = self.transaction.requests[0]
        self.assertEqual((request_sent["action"], request_sent["update"], request_sent["selection"]["revision"]),
                         ("deploy", True, NEWER))

    def test_the_review_mentions_and_the_result_reports_the_image_cleanup(self):
        transaction = FakeTransaction()
        transaction.cleanup = {"removed": 4, "skipped": 1, "freed_bytes": 650_000_000, "error": None,
                               "unexpected": "dropped"}
        transaction.retention = {"operations": 2, "removed": 6, "skipped": 0, "freed_bytes": 580_000_000,
                                 "error": None, "paths": ["dropped"]}
        updater = self.make(transaction)
        operation = updater.handle({"op": "review", "actor": ACTOR})["operation"]
        review = self.wait_for(updater, "awaiting_approval")["review"]
        self.assertEqual(review["summary"]["cleanup"], "Removed after success; the backup keeps a copy.")
        self.assertEqual(review["summary"]["retention"],
                         "Kept for this and the previous update; older ones are removed after success.")
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        transaction.release.set()
        result = self.wait_for(updater, "succeeded")["result"]
        self.assertEqual(result["cleanup"], {"removed": 4, "skipped": 1, "freed_bytes": 650_000_000, "error": None})
        self.assertEqual(result["retention"], {"operations": 2, "removed": 6, "skipped": 0,
                                               "freed_bytes": 580_000_000, "error": None})
        self.assertEqual(updater_module._retention({"operations": -1, "freed_bytes": "lots", "error": 5}),
                         {"operations": 0, "removed": 0, "skipped": 0, "freed_bytes": 0, "error": None})
        self.assertIsNone(updater_module._retention(None))
        transaction.cleanup = {"removed": -3, "freed_bytes": "lots", "error": 5}
        self.assertEqual(updater_module._cleanup(transaction.cleanup),
                         {"removed": 0, "skipped": 0, "freed_bytes": 0, "error": None})
        self.assertIsNone(updater_module._cleanup(None))

    def test_nothing_to_do_when_the_newest_image_is_installed(self):
        updater = self.make(resolve=lambda gateway, platform, channel="dev": candidate(INSTALLED))
        updater.handle({"op": "review", "actor": ACTOR})
        self.wait_for(updater, "up_to_date")
        self.assertEqual(self.transaction.requests, [])

    def test_cancel_and_expiry_decline_before_any_interruption(self):
        updater = self.make()
        operation = updater.handle({"op": "review", "actor": ACTOR})["operation"]
        self.wait_for(updater, "awaiting_approval")
        updater.handle({"op": "cancel", "operation": operation})
        status = self.wait_for(updater, "cancelled")
        self.assertEqual(status["error"], "Cancelled.")
        self.assertEqual(self.transaction.approvals, [None])
        clock = [1_800_000_000.0]
        expiring = self.make(clock=lambda: clock[0])
        expiring.handle({"op": "review", "actor": ACTOR})
        self.wait_for(expiring, "awaiting_approval")
        clock[0] += updater_module.REVIEW_SECONDS + 1
        self.assertEqual(self.wait_for(expiring, "cancelled")["error"], "The review expired.")

    def test_a_failure_after_approval_offers_restore_from_the_on_pi_backup(self):
        updater = self.make(FakeTransaction(outcome="fail"))
        operation = updater.handle({"op": "review", "actor": ACTOR})["operation"]
        review = self.wait_for(updater, "awaiting_approval")["review"]
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        self.transaction.release.set()
        status = self.wait_for(updater, "failed")
        self.assertTrue(status["restore_available"])
        self.assertEqual(status["backups"], [{
            "operation": operation, "taken_at": "2026-11-23T00:04:18.238842+00:00",
            "revision": self.installed["revision"], "size_bytes": 108130296,
            "reason": "before the update that failed"}], "when it was taken, its version and size")
        self.transaction.outcome = "complete"
        updater.handle({"op": "restore", "operation": operation, "actor": ACTOR})
        review = self.wait_for(updater, "restore_awaiting_approval")["review"]
        self.assertEqual(review["summary"]["action"], "restore")
        self.assertIsNone(review["summary"]["cleanup"], "a restore never cleans up")
        self.assertIsNone(review["summary"]["retention"], "a restore never removes backups")
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        self.assertEqual(self.wait_for(updater, "restored")["phase"], "restored")
        restore_request, recovery = self.transaction.requests[-1]
        self.assertEqual(restore_request, {"action": "recover", "deployment": "russound-dev", "operation": operation})
        self.assertEqual(recovery, {"recovery": self.evidence / operation, "recovery_checksum": "d" * 64})

    def test_a_failure_before_approval_offers_no_restore(self):
        def unresolvable(gateway, platform, channel="dev"):
            raise ValueError("Registry metadata request failed")
        updater = self.make(resolve=unresolvable)
        updater.handle({"op": "review", "actor": ACTOR})
        status = self.wait_for(updater, "failed_before_interruption")
        self.assertFalse(status["restore_available"])
        self.assertEqual(status["backups"], [])
        with self.assertRaisesRegex(UpdaterError, "No restore"):
            updater.handle({"op": "restore", "operation": str(uuid4()), "actor": ACTOR})

    def test_a_restart_after_the_backup_still_offers_restore_from_the_page(self):
        first = self.make()
        interrupted = self.transaction

        def end_old_run():  # the old process's run ends only now, before its directory is removed
            interrupted.release.set()
            first._thread.join(5)
        self.addCleanup(end_old_run)
        operation = first.handle({"op": "review", "actor": ACTOR})["operation"]
        review = self.wait_for(first, "awaiting_approval")["review"]
        first.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        deadline = time.monotonic() + 5
        while not (self.evidence / (operation + ".backup-identity.json")).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(first.status()["phase"], "running")
        # The service restarts here: the new process only sees the durable record and the evidence.
        restarted = self.make()
        status = restarted.status()
        self.assertEqual(status["phase"], "outcome_unknown")
        self.assertIn("outcome is unknown", status["error"])
        self.assertIn("Restore the backup taken before it", status["error"])
        self.assertTrue(status["restore_available"], "the backup taken before the restart is offered")
        self.assertEqual(status["backups"][0]["operation"], operation)
        self.assertEqual(status["backups"][0]["size_bytes"], 108130296)
        restarted.handle({"op": "restore", "operation": operation, "actor": ACTOR})
        review = self.wait_for(restarted, "restore_awaiting_approval")["review"]
        self.assertEqual(review["summary"]["action"], "restore")
        restarted.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        self.transaction.release.set()
        self.assertEqual(self.wait_for(restarted, "restored")["phase"], "restored")
        self.assertEqual(self.transaction.requests[-1][0],
                         {"action": "recover", "deployment": "russound-dev", "operation": operation})
        self.assertEqual(self.transaction.requests[-1][1],
                         {"recovery": self.evidence / operation, "recovery_checksum": "d" * 64})

    def test_a_restart_before_the_backup_or_during_a_restore_offers_no_new_restore(self):
        operation = str(uuid4())
        self.state.write_text(json.dumps({"phase": "running", "kind": "update", "operation": operation}))
        status = self.make().status()
        self.assertEqual(status["phase"], "outcome_unknown")
        self.assertFalse(status["restore_available"], "no verified backup: recovery needs the SSH operator")
        self.assertIn("Check the deployment on the Pi before trying again", status["error"])
        with self.assertRaisesRegex(UpdaterError, "No restore"):
            self.make().handle({"op": "restore", "operation": operation, "actor": ACTOR})
        kept = {"operation": operation, "manifest_sha256": "d" * 64, "taken_at": None, "size_bytes": None,
                "revision": None}
        self.state.write_text(json.dumps({"phase": "restoring", "kind": "restore", "operation": operation,
                                          "restore": kept}))
        self.assertEqual(self.make().status()["phase"], "outcome_unknown")
        self.assertEqual(json.loads(self.state.read_text())["restore"], kept, "an interrupted restore keeps its backup")

    def interrupted_after_backup_identity(self):
        """A durable record of a running update whose controller already kept the backup identity."""
        operation = str(uuid4())
        self.state.write_text(json.dumps({"phase": "running", "kind": "update", "operation": operation,
                                          "candidate_revision": NEWER}))
        identity = self.evidence / (operation + ".backup-identity.json")
        identity.write_text(json.dumps({"operation": operation, "manifest_sha256": "d" * 64}))
        identity.chmod(0o600)
        (self.evidence / operation).mkdir(mode=0o700)
        manifest = self.evidence / operation / "manifest.json"
        manifest.write_text(json.dumps({"operation": operation, "created_at": "2026-11-25T22:07:42+00:00",
                                        "total_bytes": 1, "entries": []}))
        manifest.chmod(0o600)
        return operation

    def test_a_restart_before_the_target_recorded_the_backup_points_to_resume_original(self):
        operation = self.interrupted_after_backup_identity()
        self.target = lambda: target_record({"operation": operation, "phase": "inputs-verified", "backup": None},
                                            reserved=operation)
        updater = self.make()
        status = updater.status()
        self.assertEqual(status["phase"], "outcome_unknown")
        self.assertFalse(status["restore_available"], "nothing was applied: the backup is the wrong recovery")
        self.assertIn("resume-original", status["error"])
        with self.assertRaisesRegex(UpdaterError, "No restore"):
            updater.handle({"op": "restore", "operation": operation, "actor": ACTOR})

    def test_a_restart_after_the_target_recorded_the_backup_keeps_the_restore(self):
        operation = self.interrupted_after_backup_identity()
        self.target = lambda: target_record({"operation": operation, "phase": "backup-complete",
                                             "backup": operation}, reserved=operation)
        status = self.make().status()
        self.assertEqual(status["phase"], "outcome_unknown")
        self.assertTrue(status["restore_available"])
        self.assertIn("Restore the backup taken before it", status["error"])

    def test_an_unknown_outcome_settles_once_the_pi_has_no_unfinished_operation(self):
        operation = self.interrupted_after_backup_identity()
        self.target = lambda: target_record({"operation": operation, "phase": "inputs-verified", "backup": None},
                                            reserved=operation)
        updater = self.make()
        self.assertEqual(updater.status()["phase"], "outcome_unknown")
        # The SSH operator's resume-original (or recover) resolves the target record meanwhile.
        self.target = lambda: target_record()
        status = updater.status()
        self.assertEqual(status["phase"], "failed", "the page offers updates again")
        self.assertFalse(status["restore_available"])
        self.assertEqual(status["backups"], [])
        self.assertIn("no unfinished operation", status["error"])
        self.assertEqual(status["candidate_revision"], NEWER, "the interrupted operation stays on record")
        self.assertEqual(json.loads(self.state.read_text())["phase"], "failed", "settled durably")
        with self.assertRaisesRegex(UpdaterError, "No restore"):
            updater.handle({"op": "restore", "operation": operation, "actor": ACTOR})
        updater.handle({"op": "review", "actor": ACTOR})
        self.assertEqual(self.wait_for(updater, "awaiting_approval")["phase"], "awaiting_approval")

    def test_an_unknown_outcome_stays_while_the_pi_record_is_unfinished_or_unreadable(self):
        operation = self.interrupted_after_backup_identity()
        for target in (unreadable_target, lambda: target_record(reserved=operation),
                       lambda: target_record({"operation": str(uuid4()), "phase": "prepared", "backup": None}),
                       lambda: {"blocked": False}):
            self.target = target
            with self.subTest(target=target):
                status = self.make().status()
                self.assertEqual(status["phase"], "outcome_unknown")
                self.assertTrue(status["restore_available"], "nothing concluded from an unclear record")

    def test_restart_never_resumes_or_claims_success(self):
        for phase, expected in (("awaiting_approval", "cancelled"), ("resolving", "cancelled"),
                                ("running", "outcome_unknown"), ("restoring", "outcome_unknown")):
            self.state.write_text(json.dumps({"phase": phase, "operation": str(uuid4()),
                                              "review": {"operation": "x", "digest": "y", "expires_at": 0}}))
            restarted = self.make()
            with self.subTest(phase=phase):
                status = restarted.status()
                self.assertEqual(status["phase"], expected)
                self.assertIsNone(status["review"])
                self.assertFalse(status["busy"])

    def test_requests_are_validated(self):
        updater = self.make()
        for bad in (None, {}, {"op": 5}, {"op": "reboot"}, {"op": "review"},
                    {"op": "review", "actor": {"session": "x" * 40, "client": "a"}}):
            with self.subTest(bad=bad), self.assertRaises(UpdaterError):
                updater.handle(bad)
        with self.assertRaisesRegex(UpdaterError, "no pending review"):
            updater.handle({"op": "approve", "operation": "x", "digest": "y", "actor": ACTOR})


class DataSchemaTests(Fixture):
    """docs/plans/general_open_persisted-data-schema-version.md: refuse an update whose image cannot read the data."""

    def make_with_data(self, config=None, state=None, schemas=None):
        data = self.root / "data"
        (data / "config").mkdir(parents=True)
        (data / "data").mkdir()
        if config is not None:
            (data / "config" / "russound_config.json").write_text(json.dumps(config))
        if state is not None:
            (data / "data" / "russound_state.json").write_text(json.dumps(state))
        (data / "data" / "admin-lockout.json").write_text("not json")  # broken files are the application's
        self.asked = []

        def target_schemas(found):
            self.asked.append(found["revision"])
            if isinstance(schemas, Exception):
                raise schemas
            return schemas

        self.transaction = FakeTransaction()
        return Updater({**SPEC, "data_dir": str(data)}, self.evidence, self.state,
                       resolve=lambda gateway, platform, channel="dev": candidate(), committed=lambda: self.installed,
                       transaction=self.transaction, target=lambda: self.target(), target_schemas=target_schemas)

    def test_an_image_that_cannot_read_newer_data_is_refused_before_anything_stops(self):
        updater = self.make_with_data(config={"schema_version": 2, "zones": []}, state={"zones": []})
        updater.handle({"op": "review", "actor": ACTOR})
        status = self.wait_for(updater, "failed_before_interruption")
        self.assertIn("cannot read the data on this Pi: the settings (format 2; that version reads up to 1)",
                      status["error"])
        self.assertIn("Nothing was interrupted", status["error"])
        self.assertEqual(self.transaction.requests, [], "no transaction started")
        self.assertEqual(self.asked, [NEWER])

    def test_a_compatible_image_and_an_image_without_the_label_proceed(self):
        for schemas in ({"config": (1, 2), "state": (1, 1), "lockout": (1, 1)}, None):
            with self.subTest(schemas=schemas):
                self.state.unlink(missing_ok=True)
                updater = self.make_with_data(config={"schema_version": 1}, state={"zones": []}, schemas=schemas)
                updater.handle({"op": "review", "actor": ACTOR})
                self.wait_for(updater, "awaiting_approval")
                updater.handle({"op": "cancel", "operation": updater.status()["operation"]})
                self.wait_for(updater, "cancelled")
                shutil.rmtree(self.root / "data")

    def test_an_unreadable_label_refuses_and_no_data_needs_no_check(self):
        updater = self.make_with_data(config={"zones": []}, schemas=OSError("registry down"))
        updater.handle({"op": "review", "actor": ACTOR})
        self.assertIn("could not be checked", self.wait_for(updater, "failed_before_interruption")["error"])
        shutil.rmtree(self.root / "data")
        self.state.unlink()
        empty = self.make_with_data(schemas=OSError("never asked"))
        (self.root / "data" / "data" / "admin-lockout.json").unlink()
        empty.handle({"op": "review", "actor": ACTOR})
        self.wait_for(empty, "awaiting_approval")
        self.assertEqual(self.asked, [], "a fresh installation has nothing to check")


def release(version, revision):
    return {**candidate(revision), "kind": "release-image-candidate", "channel": "stable", "version": version}


class ChannelTests(Fixture):
    """docs/plans/general_new_release-channels.md: the stored channel and when a release is offered."""

    def make_channel(self, offered, contains=None):
        self.resolved, self.compared = [], []

        def resolve(gateway, platform, channel="dev"):
            self.resolved.append(channel)
            return offered[channel]

        def check(release_revision, installed_revision):
            self.compared.append((release_revision, installed_revision))
            if isinstance(contains, Exception):
                raise contains
            return contains

        self.transaction = FakeTransaction()
        return Updater(SPEC, self.evidence, self.state, resolve=resolve, committed=lambda: self.installed,
                       transaction=self.transaction, target=lambda: self.target(), contains=check)

    def test_without_a_choice_the_channel_follows_the_installed_version(self):
        updater = self.make_channel({})
        self.assertEqual(updater.status()["channel"], "development")
        self.installed = release("0.9.0", INSTALLED)
        self.assertEqual(updater.status()["channel"], "stable")
        self.assertEqual(updater.status()["installed_version"], "0.9.0")

    def test_choosing_a_channel_is_stored_audited_and_refused_while_busy(self):
        updater = self.make_channel({"stable": release("0.9.0", NEWER)}, contains=True)
        self.assertEqual(updater.handle({"op": "channel", "channel": "stable", "actor": ACTOR}),
                         {"accepted": True, "channel": "stable"})
        stored = json.loads((self.root / "channel.json").read_text())
        self.assertEqual((stored["channel"], stored["changed_by"]), ("stable", ACTOR))
        self.assertEqual(updater.status()["channel"], "stable")
        for bad in ("beta", None, "dev"):
            with self.subTest(bad=bad), self.assertRaisesRegex(UpdaterError, "stable or development"):
                updater.handle({"op": "channel", "channel": bad, "actor": ACTOR})
        with self.assertRaisesRegex(UpdaterError, "administrator"):
            updater.handle({"op": "channel", "channel": "development"})
        updater.handle({"op": "review", "actor": ACTOR})
        self.wait_for(updater, "awaiting_approval")
        with self.assertRaisesRegex(UpdaterError, "in progress"):
            updater.handle({"op": "channel", "channel": "development", "actor": ACTOR})

    def test_stable_waits_until_a_release_contains_the_installed_development_build(self):
        updater = self.make_channel({"stable": release("0.9.0", NEWER)}, contains=False)
        updater.handle({"op": "channel", "channel": "stable", "actor": ACTOR})
        updater.handle({"op": "review", "actor": ACTOR})
        status = self.wait_for(updater, "waiting_for_release")
        self.assertEqual((status["candidate_version"], status["error"]), ("0.9.0", None))
        self.assertEqual(self.resolved, ["stable"])
        self.assertEqual(self.compared, [(NEWER, INSTALLED)])
        self.assertEqual(self.transaction.requests, [], "nothing goes backwards")
        updater.handle({"op": "channel", "channel": "development", "actor": ACTOR})
        self.assertEqual(updater.status()["phase"], "idle", "the result was about the other channel")

    def test_a_release_that_contains_the_build_is_offered_and_an_unknown_history_offers_nothing(self):
        updater = self.make_channel({"stable": release("0.9.0", NEWER)}, contains=True)
        updater.handle({"op": "channel", "channel": "stable", "actor": ACTOR})
        updater.handle({"op": "review", "actor": ACTOR})
        self.wait_for(updater, "awaiting_approval")
        self.assertEqual(self.transaction.requests[0][0]["selection"]["version"], "0.9.0")
        updater.handle({"op": "cancel", "operation": updater.status()["operation"]})
        self.wait_for(updater, "cancelled")
        unknown = self.make_channel({"stable": release("0.9.0", NEWER)}, contains=ValueError("GitHub down"))
        unknown.handle({"op": "review", "actor": ACTOR})
        self.assertIn("could not be checked on GitHub", self.wait_for(unknown, "failed_before_interruption")["error"])

    def test_releases_only_move_forward_and_development_is_always_offered(self):
        self.installed = release("0.9.1", INSTALLED)
        updater = self.make_channel({"stable": release("0.9.0", NEWER), "dev": candidate()})
        updater.handle({"op": "review", "actor": ACTOR})
        self.wait_for(updater, "up_to_date")
        self.assertEqual(self.compared, [], "release over release compares versions")
        updater.handle({"op": "channel", "channel": "development", "actor": ACTOR})
        updater.handle({"op": "review", "actor": ACTOR})
        self.wait_for(updater, "awaiting_approval")
        self.assertEqual(self.resolved, ["stable", "dev"])


class SystemRoutingTests(Fixture):
    class FakeSystem:
        def __init__(self):
            self.active, self.requests = False, []

        def busy(self):
            return self.active

        def packages_busy(self):
            return self.active

        def handle(self, request, actor):
            from tool.system_updates import SystemRefused
            self.requests.append((request["op"], actor() if request["op"] == "system_review" else None))
            if request["op"] == "system_check":
                raise SystemRefused("try again in 5 s")
            return {"phase": "idle"}

    def test_system_requests_are_routed_and_refusals_become_safe_errors(self):
        updater = self.make()
        with self.assertRaisesRegex(UpdaterError, "not set up"):
            updater.handle({"op": "system_status"})
        updater.system = self.FakeSystem()
        self.assertEqual(updater.handle({"op": "system_status"}), {"phase": "idle"})
        updater.handle({"op": "system_review", "kind": "install", "actor": ACTOR})
        self.assertEqual(updater.system.requests[-1], ("system_review", ACTOR))
        with self.assertRaisesRegex(UpdaterError, "try again"):
            updater.handle({"op": "system_check"})
        with self.assertRaisesRegex(UpdaterError, "administrator"):
            updater.handle({"op": "system_review", "kind": "install"})

    def test_an_image_update_never_starts_during_a_system_operation_and_vice_versa(self):
        updater = self.make()
        updater.system = self.FakeSystem()
        updater.system.active = True
        with self.assertRaisesRegex(UpdaterError, "system update"):
            updater.review(ACTOR)
        self.assertEqual(updater.status()["phase"], "idle")
        updater.system.active = False
        self.assertFalse(updater.busy())
        updater.review(ACTOR)
        self.wait_for(updater, "awaiting_approval")
        self.assertTrue(updater.busy(), "the system part asks this before it starts")

    def test_deployment_clear(self):
        updater = self.make()
        self.assertFalse(updater.deployment_clear(), "unreadable state is not clear")
        self.target = lambda: target_record()
        self.assertTrue(updater.deployment_clear())
        self.target = lambda: target_record(pending={"operation": "x", "phase": "prepared"})
        self.assertFalse(updater.deployment_clear())


class SocketTests(Fixture):
    def test_one_json_request_per_connection_and_restricted_permissions(self):
        updater = self.make()
        short = tempfile.mkdtemp(prefix="upd", dir="/tmp")
        self.addCleanup(lambda: os.path.exists(short) and __import__("shutil").rmtree(short))
        path = Path(short) / "u.sock"
        server = UpdaterServer(path, updater, None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(path.stat().st_mode & 0o777, 0o660)
        status = request(path, {"op": "status"})
        self.assertEqual((status["ok"], status["phase"], status["installed_revision"]), (True, "idle", INSTALLED))
        refused = request(path, {"op": "reboot"})
        self.assertEqual(refused, {"ok": False, "error": "Unknown request"})
        import socket as socket_module
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as raw:
            raw.connect(str(path))
            raw.sendall(b"not json\n")
            self.assertEqual(json.loads(raw.recv(4096)), {"ok": False, "error": "Invalid request"})


class PlayTests(unittest.TestCase):
    def test_install_play_never_touches_the_application_containers(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        play = yaml.safe_load((root / "ansible/playbooks/deploy-updater.yml").read_text())[0]
        tasks = play["pre_tasks"] + play["tasks"] + play["handlers"]
        modules = {key for task in tasks for key in task if key.startswith(("ansible.", "community."))}
        self.assertLessEqual(modules, {"ansible.builtin.assert", "ansible.builtin.set_fact", "ansible.builtin.stat",
                                       "ansible.builtin.file", "ansible.builtin.copy", "ansible.builtin.template",
                                       "ansible.builtin.systemd", "ansible.builtin.apt"})
        apt = [task["ansible.builtin.apt"] for task in tasks if "ansible.builtin.apt" in task]
        self.assertEqual([item["name"] for item in apt], ["unattended-upgrades"], "no other package is installed")
        config = next(task["ansible.builtin.copy"]["content"] for task in tasks
                      if task.get("ansible.builtin.copy", {}).get("dest", "").startswith("/etc/apt/apt.conf.d/"))
        self.assertIn("#clear Unattended-Upgrade::Origins-Pattern;", config)
        self.assertIn('"origin=Debian,codename=${distro_codename}-security,label=Debian-Security";', config)
        self.assertEqual(config.count("origin="), 1, "only the security origin")
        self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', config)
        systemd = [task["ansible.builtin.systemd"]["name"] for task in tasks if "ansible.builtin.systemd" in task]
        self.assertEqual(set(systemd), {"mpdrussoundpi-updater.service"})
        directories = {task["ansible.builtin.file"]["path"]: task["ansible.builtin.file"] for task in tasks
                       if "ansible.builtin.file" in task}
        self.assertEqual(directories["{{ updater_root }}/socket"]["mode"], "0750")
        self.assertEqual(directories["{{ updater_root }}/evidence"]["mode"], "0700")
        self.assertIn("exactly one", json.dumps(play["pre_tasks"]))


class ConfigTests(Fixture):
    def test_configuration_shape(self):
        path = self.root / "updater.json"
        path.write_text(json.dumps({"schema_version": 1, "spec": "/etc/x.json", "client_gid": 1000}))
        path.chmod(0o600)
        self.assertEqual(updater_module.load_config(path)["client_gid"], 1000)
        path.write_text(json.dumps({"schema_version": 1, "spec": "/etc/x.json", "client_gid": "users"}))
        with self.assertRaises(UpdaterError):
            updater_module.load_config(path)


if __name__ == "__main__":
    unittest.main()
