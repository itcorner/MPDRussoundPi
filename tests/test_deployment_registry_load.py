import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool import deployment_load as loads
from tool.deployment_images import SOURCE, resolve
from tool.deployment_state import StateError, StateStore
from test_deployment_images import FixtureRegistry


INTENT = "sha256:" + "a" * 64


class RegistryLoadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-registry-load-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.store = StateStore(self.root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.selection = resolve("managed", "linux/arm64", FixtureRegistry())
        self.operation = str(uuid4())
        self.store.begin("fixture", self.operation, self.selection, INTENT, ["apply"])
        self.store.advance("fixture", self.operation, "inputs-verified")
        self.store.advance("fixture", self.operation, "backup-complete")
        self.pulls = []

    def inspect(self, argv):
        name = next(name for name, image in self.selection["images"].items() if image["reference"] == argv[-1])
        image = self.selection["images"][name]
        return json.dumps([{
            "Id": image["config_digest"], "Os": "linux", "Architecture": "arm64",
            "Config": {"Labels": {"org.opencontainers.image.source": SOURCE,
                                  "org.opencontainers.image.revision": self.selection["revision"]}},
        }]).encode()

    def pull(self, argv, **kwargs):
        self.assertEqual(os.fstat(kwargs["stdout"].fileno()).st_mode & 0o777, 0o600)
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"]["phase"], "backup-complete")
        self.pulls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def load(self, **patches):
        with patch.object(loads, "verify") as verify, \
                patch.object(loads, "local_engine", return_value="default"), \
                patch.object(loads, "command", side_effect=patches.get("inspect", self.inspect)), \
                patch.object(loads.subprocess, "run", side_effect=patches.get("pull", self.pull)):
            result = loads.load_registry_candidate(self.store, "fixture", self.operation, INTENT,
                                                   self.selection, self.evidence)
            verify.assert_called_once_with(self.selection)
            return result

    def test_exact_pinned_pair_is_pulled_once_and_state_schema_remains_registry_specific(self):
        result = self.load()
        self.assertEqual(result, {name: image["config_digest"] for name, image in self.selection["images"].items()})
        self.assertEqual(len(self.pulls), 2)
        for argv in self.pulls:
            self.assertIn("--platform", argv)
            self.assertIn("@sha256:", argv[-1])
        pending = self.store.status()["deployments"]["fixture"]["pending"]
        self.assertNotIn("loaded_images", pending)
        self.assertTrue(self.store.status()["blocked"])
        other = self.root / "other"
        other.mkdir(mode=0o700)
        with patch.object(loads, "verify"), patch.object(loads, "local_engine", return_value="default"), \
                self.assertRaises(StateError):
            loads.load_registry_candidate(self.store, "fixture", self.operation, INTENT, self.selection, other)

    def test_wrong_config_identity_retains_marker_without_fallback(self):
        def incorrect(argv):
            actual = json.loads(self.inspect(argv))
            actual[0]["Id"] = "sha256:" + "f" * 64
            return json.dumps(actual).encode()
        with self.assertRaises(loads.LoadError):
            self.load(inspect=incorrect)
        self.assertEqual(len(self.pulls), 1)
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_load_started"])

    def test_pull_timeout_or_nonzero_never_advances_or_retries(self):
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.load(pull=timeout)
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"]["phase"], "backup-complete")

    def test_explicit_recovery_requires_all_checks_and_preserves_old_pending_evidence(self):
        self.load()
        before = copy.deepcopy(self.store.status()["deployments"]["fixture"]["pending"])
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": {"containers": "passed", "http": "passed",
                                "backend": "passed", "playback": "passed"}}
        with self.assertRaises(StateError):
            self.store.resolve_recovery("fixture", self.operation,
                                        {**receipt, "required": {"containers": "passed"}})
        self.assertEqual(self.store.status()["deployments"]["fixture"]["pending"], before)
        self.store.resolve_recovery("fixture", self.operation, receipt)
        self.assertFalse(self.store.status()["blocked"])
        self.assertNotIn("fixture", self.store.status()["deployments"])
