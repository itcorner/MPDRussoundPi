import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import yaml

from deploymentfixtures import ROOT
from test_deployment_images import FixtureRegistry
from tool import deployment_tool_upgrade as upgrade
from tool.deployment_images import resolve
from tool.deployment_state import StateError, StateStore


class ToolingUpgradeStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-tool-upgrade-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tools = ROOT
        self.state_root = self.root / "state"
        self.store = StateStore(self.state_root, owner_uid=os.getuid())
        self.store.initialize()
        self.owner_uid = os.getuid()

    def test_acquire_reserves_host_state_until_explicit_release(self):
        operation = upgrade.acquire_lock(self.tools, self.state_root, owner_uid=self.owner_uid)

        status = self.store.status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["reservation"], {
            "schema_version": 1,
            "deployment": "tooling-upgrade",
            "operation": operation,
        })
        with self.assertRaises(StateError):
            self.store.begin(
                "demo",
                str(uuid4()),
                resolve("external", "linux/arm64", FixtureRegistry()),
                "sha256:" + "d" * 64,
                ["apply"],
            )

        upgrade._release_owned_lock(self.state_root, operation, self.owner_uid)
        self.assertFalse(self.store.status()["blocked"])

    def test_existing_or_pending_deployment_blocks_tooling_lock(self):
        self.store.begin(
            "demo",
            str(uuid4()),
            resolve("external", "linux/arm64", FixtureRegistry()),
            "sha256:" + "d" * 64,
            ["apply"],
        )
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.acquire_lock(self.tools, self.state_root, owner_uid=self.owner_uid)
        self.assertTrue(self.store.status()["blocked"])

    def test_lock_release_requires_the_exact_operation(self):
        operation = upgrade.acquire_lock(self.tools, self.state_root, owner_uid=self.owner_uid)
        with self.assertRaises(upgrade.UpgradeError):
            upgrade._release_owned_lock(self.state_root, str(uuid4()), self.owner_uid)
        self.assertEqual(self.store.status()["reservation"]["operation"], operation)


@unittest.skipUnless(os.name == "posix", "tooling swap requires a POSIX filesystem")
class ToolingUpgradeExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-tool-swap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.opt = self.root / "opt"
        self.opt.mkdir(mode=0o755)
        self.active = self.opt / "mpdrussoundpi-deploy"
        self.state_root = self.root / "state"
        self.owner_uid = os.getuid()
        self.store = StateStore(self.state_root, owner_uid=self.owner_uid)
        self.store.initialize()
        self.status = patch.object(
            upgrade, "_state_status",
            side_effect=lambda _tools, _state: self.store.status(),
        )
        self.status.start()
        self.addCleanup(self.status.stop)
        self.tools_constant = patch.object(upgrade, "TOOLS", self.active)
        self.tools_constant.start()
        self.addCleanup(self.tools_constant.stop)
        self.git_environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }

    def _repository(self, path: Path, content: str) -> str:
        path.mkdir(mode=0o755)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        (path / "revision.txt").write_text(content)
        subprocess.run(["git", "-C", str(path), "add", "revision.txt"],
                       check=True, env=self.git_environment)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", content],
                       check=True, env=self.git_environment)
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def _portable_exchange(self, left: Path, right: Path) -> None:
        temporary = left.with_name(left.name + ".test-swap")
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    def test_exchange_preserves_old_checkout_and_reconcile_restores_it(self):
        old_revision = self._repository(self.active, "old")
        candidate = self.opt / "candidate"
        new_revision = self._repository(candidate, "new")
        stage = self.active.with_name(f"{self.active.name}.upgrade-{new_revision}")
        candidate.rename(stage)
        operation = upgrade.acquire_lock(
            self.active, self.state_root, owner_uid=self.owner_uid
        )

        with patch.object(upgrade, "_exchange", side_effect=self._portable_exchange), \
                patch.object(upgrade, "_fsync_parent"):
            upgrade.exchange(
                old_revision,
                new_revision,
                new_revision,
                operation,
                owner_uid=self.owner_uid,
                tools=self.active,
                state_root=self.state_root,
            )
            self.assertEqual(upgrade._git(self.active, "rev-parse", "HEAD"), new_revision)
            self.assertEqual(upgrade._git(stage, "rev-parse", "HEAD"), old_revision)

            result = upgrade.reconcile(
                old_revision,
                new_revision,
                operation,
                owner_uid=self.owner_uid,
                tools=self.active,
                state_root=self.state_root,
            )
        self.assertEqual(result, "original-tooling-restored")
        self.assertEqual(upgrade._git(self.active, "rev-parse", "HEAD"), old_revision)
        self.assertEqual(upgrade._git(stage, "rev-parse", "HEAD"), new_revision)
        self.assertFalse(self.store.status()["blocked"])

    def test_reconcile_without_exchange_keeps_original_and_candidate(self):
        old_revision = self._repository(self.active, "old")
        candidate = self.opt / "candidate"
        new_revision = self._repository(candidate, "new")
        stage = self.active.with_name(f"{self.active.name}.upgrade-{new_revision}")
        candidate.rename(stage)
        operation = upgrade.acquire_lock(
            self.active, self.state_root, owner_uid=self.owner_uid
        )

        result = upgrade.reconcile(
            old_revision,
            new_revision,
            operation,
            owner_uid=self.owner_uid,
            tools=self.active,
            state_root=self.state_root,
        )

        self.assertEqual(result, "original-tooling-retained")
        self.assertEqual(upgrade._git(self.active, "rev-parse", "HEAD"), old_revision)
        self.assertEqual(upgrade._git(stage, "rev-parse", "HEAD"), new_revision)
        self.assertFalse(self.store.status()["blocked"])

    @unittest.skipUnless(
        hasattr(__import__("ctypes").CDLL(None), "renameat2"),
        "Linux renameat2 is unavailable",
    )
    def test_atomic_exchange_swaps_directory_names(self):
        left = self.opt / "left"
        right = self.opt / "right"
        left.mkdir()
        right.mkdir()
        (left / "which").write_text("left")
        (right / "which").write_text("right")

        upgrade._exchange(left, right)

        self.assertEqual((left / "which").read_text(), "right")
        self.assertEqual((right / "which").read_text(), "left")


class ToolingUpgradePlaybookTests(unittest.TestCase):
    def test_upgrade_and_restore_are_separate_from_application_deployment(self):
        upgrade_play = yaml.safe_load(
            (ROOT / "ansible/playbooks/upgrade-deployment.yml").read_text()
        )[0]
        restore_play = yaml.safe_load(
            (ROOT / "ansible/playbooks/restore-deployment-tools.yml").read_text()
        )[0]
        self.assertEqual(upgrade_play["hosts"], "musicpi")
        self.assertEqual(restore_play["hosts"], "musicpi")
        self.assertTrue(upgrade_play["any_errors_fatal"])
        upgrade_tasks = str(upgrade_play)
        restore_tasks = str(restore_play)
        self.assertIn("deployment_tools_expected_revision", upgrade_tasks)
        self.assertIn("deployment_tools_revision", upgrade_tasks)
        self.assertIn("deployment_tools_restore_revision", restore_tasks)
        self.assertIn("deployment_tool_upgrade.py", upgrade_tasks)
        self.assertIn("deployment_tool_upgrade.py", restore_tasks)
        upgrade_operator = next(
            task for task in upgrade_play["tasks"][0]["block"]
            if task["name"] == "Verify the reviewed deployment operator starts and exposes help"
        )
        restore_operator = next(
            task for task in restore_play["tasks"][0]["block"]
            if task["name"] == "Verify restored deployment operator entry point"
        )
        self.assertEqual(
            upgrade_operator["ansible.builtin.command"]["chdir"],
            "/opt/mpdrussoundpi-deploy",
        )
        self.assertEqual(
            restore_operator["ansible.builtin.command"]["chdir"],
            "/opt/mpdrussoundpi-deploy",
        )
        self.assertNotIn("systemd", upgrade_tasks)
        self.assertNotIn("docker", upgrade_tasks.lower())
        self.assertNotIn("systemd", restore_tasks)
        self.assertNotIn("docker", restore_tasks.lower())
