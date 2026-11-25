from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from tool import deployment_updater as updater_module
from tool.deployment_channel import ChannelError
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
                **({"cleanup": self.cleanup} if request["action"] == "deploy" and self.cleanup is not None else {})}


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

    def make(self, transaction=None, resolve=None, clock=None):
        self.transaction = transaction or FakeTransaction()
        return Updater(SPEC, self.evidence, self.state, resolve=resolve or (lambda gateway, platform: candidate()),
                       committed=lambda: self.installed, transaction=self.transaction, clock=clock or time.time)

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
        updater = self.make(transaction)
        operation = updater.handle({"op": "review", "actor": ACTOR})["operation"]
        review = self.wait_for(updater, "awaiting_approval")["review"]
        self.assertEqual(review["summary"]["cleanup"], "Removed after success; the backup keeps a copy.")
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        transaction.release.set()
        result = self.wait_for(updater, "succeeded")["result"]
        self.assertEqual(result["cleanup"], {"removed": 4, "skipped": 1, "freed_bytes": 650_000_000, "error": None})
        transaction.cleanup = {"removed": -3, "freed_bytes": "lots", "error": 5}
        self.assertEqual(updater_module._cleanup(transaction.cleanup),
                         {"removed": 0, "skipped": 0, "freed_bytes": 0, "error": None})
        self.assertIsNone(updater_module._cleanup(None))

    def test_nothing_to_do_when_the_newest_image_is_installed(self):
        updater = self.make(resolve=lambda gateway, platform: candidate(INSTALLED))
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
        updater.handle({"op": "approve", "operation": operation, "digest": review["digest"], "actor": ACTOR})
        self.assertEqual(self.wait_for(updater, "restored")["phase"], "restored")
        restore_request, recovery = self.transaction.requests[-1]
        self.assertEqual(restore_request, {"action": "recover", "deployment": "russound-dev", "operation": operation})
        self.assertEqual(recovery, {"recovery": self.evidence / operation, "recovery_checksum": "d" * 64})

    def test_a_failure_before_approval_offers_no_restore(self):
        def unresolvable(gateway, platform):
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
                                       "ansible.builtin.systemd"})
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
