from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool import deployment_retention as retention_module
from tool.deployment_retention import RetentionError, render, retention, summary

DEPLOYMENT = "russound-dev"


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.storage = {}
        for name in ("evidence", "runtime", "staging", "backups", "assets", "incoming", "restores"):
            self.storage[name] = self.root / "operations" / name
            self.storage[name].mkdir(parents=True)
        self.updater = self.root / "updater"
        (self.updater / "evidence").mkdir(parents=True)
        self.clock = time.time() - 10_000
        self.labels = ""

    def write(self, path: Path, value=None, size=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if value is not None else "x" * size)
        return path

    def operation(self, *, finished=True, recovered=None, deployment=DEPLOYMENT, updater=False):
        """One operation with its large directories and small records, newest so far."""
        operation = str(uuid4())
        evidence = self.storage["evidence"]
        self.write(evidence / (operation + ".intent.json"), {"deployment": deployment, "operation": operation})
        if finished:
            self.clock += 60
            for suffix in (".apply-attempt.json", ".backup.json", ".completion.jsonl"):
                path = self.write(evidence / (operation + suffix), {})
                os.utime(path, (self.clock, self.clock))
        self.write(self.storage["backups"] / operation / "000001.blob", size=4096)
        self.write(self.storage["assets"] / operation / "web.tar", size=8192)
        self.write(self.storage["runtime"] / operation / "active.json", {})
        if recovered:
            self.write(evidence / (operation + ".recovery-attempt.json"), {"review": {"recovery": recovered}})
            self.write(evidence / (operation + ".recovery-complete.json"), {})
            self.write(self.storage["assets"] / recovered / "compose.json", {})
            self.write(self.storage["restores"] / recovered / "deployment" / "web.tar", size=4096)
            self.write(self.storage["incoming"] / str(uuid4()) / operation / "manifest.json", {})
        if updater:
            self.write(self.updater / "evidence" / (operation + ".review.json"), {})
            self.write(self.updater / "evidence" / operation / "manifest.json", size=4096)
        return operation

    def status(self, committed, pending=None, reserved=None):
        return {"schema_version": 1, "blocked": bool(pending or reserved),
                "reservation": {"deployment": DEPLOYMENT, "operation": reserved} if reserved else None,
                "deployments": {DEPLOYMENT: {"committed": {"selection": {"operation": committed, "backup": committed},
                                                           "completion": {}},
                                             "pending": pending}}}

    def docker(self, args):
        if args[:2] == ["container", "ls"]:
            return "abc\n" if self.labels else ""
        return self.labels

    def retain(self, committed, apply=False, updater_state=None, **status):
        if updater_state is not None:
            self.write(self.updater / "state.json", updater_state)
        return retention(self.storage, self.status(committed, **status), apply=apply, run=self.docker,
                         updater_root=self.updater)


class RetentionTests(Fixture):
    def test_keeps_the_committed_and_previous_operation_and_removes_older_large_files(self):
        oldest, older, previous, current = (self.operation(updater=True) for _ in range(4))
        report = self.retain(current)
        removed = {(item["area"], item["operation"]) for item in report["remove"]}
        self.assertEqual(removed, {(area, op) for op in (oldest, older) for area in ("backups", "assets", "updater")})
        reasons = {item["operation"]: item["reason"] for item in report["keep"]}
        self.assertEqual(reasons, {current: "committed operation of russound-dev",
                                   previous: "previous operation of russound-dev"})
        self.assertFalse(report["applied"])
        self.assertGreater(report["would_free_bytes"], 0)
        self.assertTrue((self.storage["backups"] / oldest).exists(), "a dry run removes nothing")

        applied = self.retain(current, apply=True)
        self.assertEqual(applied["removed"], 6)
        self.assertEqual(applied["freed_bytes"], report["would_free_bytes"])
        for op in (oldest, older):
            self.assertFalse((self.storage["backups"] / op).exists())
            self.assertFalse((self.storage["assets"] / op).exists())
            self.assertFalse((self.updater / "evidence" / op).exists())
            self.assertTrue((self.storage["runtime"] / op / "active.json").exists(), "small records stay")
            self.assertTrue((self.storage["evidence"] / (op + ".intent.json")).exists())
            self.assertTrue((self.updater / "evidence" / (op + ".review.json")).exists())
        for op in (previous, current):
            self.assertTrue((self.storage["backups"] / op).exists())
            self.assertTrue((self.updater / "evidence" / op).exists())
        self.assertEqual(summary(applied), {"removed": 6, "skipped": 0, "freed_bytes": applied["freed_bytes"],
                                            "error": None})
        self.assertIn("Removed 6", render(applied))

    def test_previous_skips_recovered_unfinished_and_other_deployments(self):
        previous = self.operation()
        self.operation(recovered=str(uuid4()))
        self.operation(finished=False)
        self.operation(deployment="other")
        current = self.operation()
        reasons = {item["operation"]: item["reason"] for item in self.retain(current)["keep"]}
        self.assertEqual(reasons.get(previous), "previous operation of russound-dev")

    def test_recovery_directories_and_received_backups_follow_their_operation(self):
        recovery = str(uuid4())
        failed = self.operation(recovered=recovery)
        self.operation()
        current = self.operation()
        report = self.retain(current)
        by_area = {item["area"] for item in report["remove"] if item["operation"] == failed}
        self.assertEqual(by_area, {"backups", "assets", "restores", "incoming"})
        paths = {item["path"] for item in report["remove"]}
        self.assertIn(str(self.storage["assets"] / recovery), paths)
        self.assertIn(str(self.storage["restores"] / recovery), paths)

    def test_the_updater_operation_its_restore_and_running_references_are_kept(self):
        restore_source, updater_op, handover, recovered_in_use = (self.operation() for _ in range(4))
        recovery = str(uuid4())
        self.write(self.storage["evidence"] / (recovered_in_use + ".recovery-attempt.json"),
                   {"review": {"recovery": recovery}})
        self.write(self.storage["assets"] / recovery / "compose.json", {})
        self.operation()
        current = self.operation()
        self.labels = (f"pi-handover-{handover.replace('-', '')} "
                       f"{self.storage['assets']}/{recovery}/compose.json {self.storage['runtime']}/{current}\n")
        report = self.retain(current, updater_state={"phase": "failed", "operation": updater_op,
                                                     "restore": {"operation": restore_source}})
        reasons = {item["operation"]: item["reason"] for item in report["keep"]}
        self.assertEqual(reasons[restore_source], "the updater's restore source")
        self.assertEqual(reasons[updater_op], "the updater's current operation")
        self.assertEqual(reasons[handover], "referred to by the running containers")
        self.assertEqual(reasons[recovered_in_use], "referred to by the running containers")
        self.assertFalse({item["operation"] for item in report["remove"]}
                         & {restore_source, updater_op, handover, recovered_in_use})

    def test_unknown_entries_are_never_removed(self):
        self.operation()
        current = self.operation()
        stranger = self.storage["backups"] / str(uuid4())
        self.write(stranger / "000001.blob", size=10)
        named = self.storage["assets"] / "manual-copy"
        named.mkdir()
        loose = self.write(self.storage["incoming"] / "notes.txt", size=10)
        mixed = self.storage["incoming"] / str(uuid4())
        self.write(mixed / current / "a", size=1)
        self.write(mixed / "second" / "b", size=1)
        target = self.root / "outside"
        target.mkdir()
        link = self.storage["restores"] / str(uuid4())
        link.symlink_to(target)
        report = self.retain(current, apply=True)
        for path in (stranger, named, loose, mixed, link):
            self.assertIn(str(path), report["unidentified"])
            self.assertTrue(os.path.lexists(path))
        self.assertTrue(target.exists())

    def test_nothing_is_decided_while_the_state_or_records_are_unclear(self):
        old = self.operation()
        self.operation()
        current = self.operation()
        for status in ({"pending": {"operation": current}}, {"reserved": current}):
            with self.subTest(status=status), self.assertRaises(RetentionError):
                self.retain(current, apply=True, **status)
        self.write(self.updater / "state.json", size=3)
        with self.assertRaisesRegex(RetentionError, "updater record"):
            retention(self.storage, self.status(current), apply=True, run=self.docker, updater_root=self.updater)
        (self.updater / "state.json").unlink()

        def failing(args):
            raise RetentionError("docker is not reachable")
        with self.assertRaisesRegex(RetentionError, "containers use"):
            retention(self.storage, self.status(current), apply=True, run=failing, updater_root=self.updater)
        with self.assertRaisesRegex(RetentionError, "no committed operation"):
            retention(self.storage, {"blocked": False, "reservation": None,
                                     "deployments": {DEPLOYMENT: {"committed": None, "pending": None}}},
                      apply=True, run=self.docker)
        self.assertTrue((self.storage["backups"] / old).exists())

    def test_a_failed_removal_is_reported_and_the_rest_continues(self):
        first, second = self.operation(), self.operation()
        self.operation()
        current = self.operation()
        real = retention_module.shutil.rmtree

        def flaky(path, *args, **kwargs):
            if Path(path).name == first and Path(path).parent == self.storage["backups"]:
                raise PermissionError("busy")
            real(path, *args, **kwargs)
        with patch.object(retention_module.shutil, "rmtree", side_effect=flaky):
            report = self.retain(current, apply=True)
        self.assertEqual([item["path"] for item in report["skipped"]], [str(self.storage["backups"] / first)])
        self.assertFalse((self.storage["backups"] / second).exists())
        self.assertEqual(summary(report)["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
