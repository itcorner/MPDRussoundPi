import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from test_deployment_build import fixture_bundle
from test_deployment_native import selection_fixture
from tool.deployment_load import select_bundle
from tool.deployment_state import PHASES, StateError, StateStore


INTENT = "sha256:" + "d" * 64


class PiHandoverStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-handover-state-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        root.chmod(0o700)
        self.store = StateStore(root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.native = selection_fixture()
        self.native_operation = str(uuid4())
        required = ["units", "http", "backend", "playback"]
        self.store.begin("fixture", self.native_operation, self.native, INTENT, required)
        for phase in PHASES[1:]:
            self.store.advance("fixture", self.native_operation, phase)
        self.store.finalize("fixture", self.native_operation, {
            "operation": self.native_operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(required, "passed"),
        })
        self.previous = self.store.status()["deployments"]["fixture"]["committed"]
        bundle, checksum, _ = fixture_bundle(root, "managed")
        self.images = select_bundle(bundle, checksum, "managed", "linux/arm64")
        self.operation = str(uuid4())
        self.required = ["apply", "containers", "http", "backend", "playback"]

    def test_native_record_remains_committed_until_image_transaction_finalizes(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        self.store.advance("fixture", self.operation, "inputs-verified")
        with self.assertRaises(StateError):
            self.store.advance("fixture", self.operation, "backup-complete")
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.previous)
        self.store.advance("fixture", self.operation, "backup-complete", backup=self.operation)
        with self.assertRaises(StateError):
            self.store.advance("fixture", self.operation, "apply-complete")
        self.store.start_image_load("fixture", self.operation, self.images, INTENT)
        loaded = {name: image["reference"] for name, image in self.images["manifest"]["images"].items()}
        self.store.complete_image_load("fixture", self.operation, self.images, INTENT, loaded)
        self.store.advance("fixture", self.operation, "apply-complete")
        self.store.advance("fixture", self.operation, "checks-complete")
        self.assertEqual(self.store.status()["deployments"]["fixture"]["committed"], self.previous)
        self.store.finalize("fixture", self.operation, {
            "operation": self.operation, "intent_digest": INTENT,
            "invocation": "verified-complete", "required": dict.fromkeys(self.required, "passed"),
        })
        state = self.store.status()
        self.assertFalse(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"]["selection"]["candidate"], self.images)
        self.assertEqual(state["deployments"]["fixture"]["committed"]["selection"]["loaded_images"], loaded)

    def test_existing_recovery_refuses_native_restore_for_pending_image_candidate(self):
        self.store.begin("fixture", self.operation, self.images, INTENT, self.required,
                         expected_committed=self.previous)
        receipt = {"operation": self.operation, "review_digest": INTENT,
                   "required": dict.fromkeys(("containers", "http", "backend", "playback"), "passed")}
        with self.assertRaisesRegex(StateError, "Only native recovery"):
            self.store.resolve_recovery("fixture", self.operation, receipt,
                                        restore_native=True, native_previous=self.previous)
        state = self.store.status()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["deployments"]["fixture"]["committed"], self.previous)
        self.assertEqual(state["deployments"]["fixture"]["pending"]["operation"], self.operation)


if __name__ == "__main__":
    unittest.main()
