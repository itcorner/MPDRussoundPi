import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tool import deployment_runner as runners
from tool import deployment_build as builds
from tool.deployment_backup import FileBackup
from tool.deployment_inputs import InputError
from tool.deployment_state import StateError, StateStore
from test_deployment_build import fixture_bundle


ANSIBLE = Path(sys.executable).parent / "ansible-playbook"


class DevelopmentRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-run-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.bundle, self.digest, _ = fixture_bundle(self.root, "external")
        self.platform = "linux/arm64"
        self.store = StateStore(self.root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.paths = {}
        for name in ("staging", "runtime", "backups", "evidence", "offhost", "live", "sources"):
            self.paths[name] = self.root / name
            self.paths[name].mkdir(mode=0o700)
        self.config = self.paths["live"] / "config.json"
        self.config.write_text('{"notes":"preserve operator data"}')
        self.config.chmod(0o600)
        self.secret = self.paths["sources"] / "secret"
        self.secret.write_text("synthetic-private-token")
        self.secret.chmod(0o600)
        self.playbook = self.paths["sources"] / "apply.yml"
        self.inventory = self.paths["sources"] / "inventory.yml"
        self.playbook.write_text("[]\n")
        self.inventory.write_text("fixture ansible_connection=local\n")
        self.playbook.chmod(0o600)
        self.inventory.chmod(0o600)
        self.events = []
        self.running = True
        self.transaction: runners.DevelopmentRun
        self.hooks = runners.RunHooks(
            preflight=self.preflight, stop_writers=self.stop, verify_quiescent=self.quiescent,
            verify_backup=self.offhost, check_runtime=self.check)
        # macOS lacks the Linux fd-xattr interface; Linux integration below
        # exercises the real implementation without this compatibility double.
        if not hasattr(os, "listxattr"):
            metadata = patch("tool.deployment_backup.os.listxattr", return_value=[], create=True)
            metadata.start()
            self.addCleanup(metadata.stop)

    def preflight(self):
        self.events.append("preflight")

    def stop(self):
        self.events.append("stop")
        self.assertTrue(self.store.status()["blocked"])
        self.assertEqual(self.pending()["phase"], "inputs-verified")
        self.running = False

    def quiescent(self):
        if self.running:
            raise RuntimeError("writers active")

    def offhost(self, backup):
        self.events.append("offhost")
        self.quiescent()
        destination = self.paths["offhost"] / backup.operation
        shutil.copytree(backup.path, destination)
        FileBackup(self.paths["offhost"], backup.operation, backup.manifest_sha256).verify()

    def check(self, images):
        self.events.append("checks")
        self.assertTrue(self.running)
        self.assertEqual(self.pending()["phase"], "apply-complete")
        return {"routing": "passed"}

    def prepare(self, **overrides):
        kwargs = {
            "store": self.store, "deployment": "demo", "host": "fixture",
            "bundle": self.bundle, "manifest_sha256": self.digest, "gateway": "external",
            "platform": self.platform, "ansible": ANSIBLE, "playbook": self.playbook,
            "inventory": self.inventory, "protected_inputs": {"credentials": self.secret},
            "recovery_sources": {"web": self.paths["live"]},
            **{name: self.paths[name] for name in ("staging", "runtime", "backups", "evidence")},
            "required_apply": ["apply"], "required_checks": ["routing"], "hooks": self.hooks,
        }
        self.transaction = runners.DevelopmentRun(**{**kwargs, **overrides})
        return self.transaction

    def pending(self):
        return self.store.status()["deployments"]["demo"]["pending"]

    def load(self, store, deployment, operation, intent, selection, bundle, evidence):
        self.events.append("load")
        self.quiescent()
        self.assertEqual(self.pending()["phase"], "backup-complete")
        self.assertTrue((self.paths["offhost"] / operation / "manifest.json").is_file())
        store.start_image_load(deployment, operation, selection, intent)
        images = {name: image["reference"] for name, image in selection["manifest"]["images"].items()}
        store.complete_image_load(deployment, operation, selection, intent, images)
        return images

    def apply(self, **kwargs):
        self.events.append("apply")
        self.assertFalse(self.running)
        self.assertEqual(self.pending()["phase"], "backup-complete")
        variables = json.loads(kwargs["variables"].read_text())
        self.assertEqual(variables["deployment_images"], self.pending()["loaded_images"])
        self.assertNotEqual(kwargs["playbook"], self.playbook)
        self.assertEqual(kwargs["playbook"].read_bytes(), self.playbook.read_bytes())
        self.assertEqual(kwargs["inventory"].read_bytes(), self.inventory.read_bytes())
        installed = Path(variables["deployment_inputs"]["credentials"])
        self.assertEqual(installed.read_bytes(), self.secret.read_bytes())
        self.running = True
        return {"apply": "passed"}

    def execute(self, approve=None):
        return self.transaction.execute(approve or (lambda review: review["digest"]))

    def test_complete_ordered_run_retains_runtime_inputs_backup_and_commits(self):
        run = self.prepare()
        review = run.review()
        public = json.dumps(review)
        self.assertNotIn(str(self.paths["live"]), public)
        self.assertNotIn(str(self.secret), public)
        self.assertNotIn("synthetic-private-token", public)
        with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                patch.object(runners, "run_checked", side_effect=self.apply):
            result = self.execute()
        self.assertEqual(self.events, ["preflight", "preflight", "stop", "offhost", "load", "apply", "checks"])
        self.assertFalse(self.store.status()["blocked"])
        committed = self.store.status()["deployments"]["demo"]["committed"]["selection"]
        self.assertEqual(committed["operation"], run.operation)
        self.assertEqual(committed["backup"], run.operation)
        self.assertEqual(committed["loaded_images"], result["images"])
        self.assertEqual(result["required"], {"apply": "passed", "routing": "passed"})
        self.assertTrue((self.paths["runtime"] / run.operation / "credentials.input").is_file())
        self.assertTrue((self.paths["backups"] / run.operation / "manifest.json").is_file())
        self.assertEqual(self.config.read_text(), '{"notes":"preserve operator data"}')
        with self.assertRaises(runners.RunError):
            self.execute()

    def test_declined_or_wrong_approval_never_reserves_or_stops(self):
        self.prepare()
        for answer in (None, "", "sha256:" + "f" * 64):
            with self.assertRaises(runners.RunError):
                self.execute(lambda review: answer)
        self.assertEqual(self.events, ["preflight"])
        self.assertFalse(self.store.status()["blocked"])
        self.assertTrue(self.running)
        self.assertEqual(list(self.paths["evidence"].iterdir()), [])

    def test_changed_sources_or_review_plan_cannot_be_approved(self):
        self.prepare()
        self.secret.write_text("changed")
        with self.assertRaises(InputError):
            self.execute()
        self.assertFalse(self.store.status()["blocked"])
        self.secret.write_text("synthetic-private-token")
        self.transaction._plan["interrupt_writers"] = False
        with self.assertRaises(runners.RunError):
            self.execute()
        self.assertTrue(self.running)

    def test_source_drift_inside_approval_callback_blocks_before_reservation(self):
        self.prepare()
        def mutate(review):
            self.inventory.write_text("other ansible_connection=local\n")
            return review["digest"]
        with self.assertRaises(InputError):
            self.execute(mutate)
        self.assertFalse(self.store.status()["blocked"])
        self.assertTrue(self.running)

    def test_failure_after_stop_or_backup_never_applies_or_clears_state(self):
        for boundary in ("stop", "backup", "load", "apply", "checks"):
            with self.subTest(boundary=boundary):
                self.setUp_subcase(boundary)
                self.prepare()
                def stop():
                    self.stop()
                    if boundary == "stop":
                        raise RuntimeError("uncertain stop")
                def backup(value):
                    self.offhost(value)
                    if boundary == "backup":
                        raise RuntimeError("unverified offhost copy")
                def load(*args):
                    images = self.load(*args)
                    if boundary == "load":
                        raise RuntimeError("uncertain load")
                    return images
                def apply(**kwargs):
                    result = self.apply(**kwargs)
                    if boundary == "apply":
                        raise StateError("incomplete callback")
                    return result
                def check(images):
                    self.check(images)
                    return {"routing": "failed"}
                self.transaction._hooks = runners.RunHooks(self.preflight, stop, self.quiescent, backup, check)
                with patch.object(runners, "load_reserved_bundle", side_effect=load), \
                        patch.object(runners, "run_checked", side_effect=apply):
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.execute()
                state = self.store.status()
                self.assertTrue(state["blocked"])
                self.assertIsNone(state["deployments"]["demo"]["committed"])
                expected = "inputs-verified" if boundary in ("stop", "backup") else (
                    "apply-complete" if boundary == "checks" else "backup-complete")
                self.assertEqual(self.pending()["phase"], expected)
                with self.assertRaises(runners.RunError):
                    self.execute()

    def setUp_subcase(self, name):
        self.events = []
        self.running = True
        self.store = StateStore(self.root / ("state-" + name), owner_uid=os.getuid())
        self.store.initialize()
        for key in ("staging", "runtime", "backups", "evidence", "offhost"):
            self.paths[key] = self.root / (key + "-" + name)
            self.paths[key].mkdir(mode=0o700)

    def test_false_success_hook_and_active_writer_guard_block_progress(self):
        self.prepare(hooks=runners.RunHooks(self.preflight, Mock(return_value=False),
                                            self.quiescent, self.offhost, self.check))
        with self.assertRaises(runners.RunError):
            self.execute()
        self.assertTrue(self.running)
        self.assertEqual(self.pending()["phase"], "inputs-verified")
        self.assertTrue(self.store.status()["blocked"])
        self.setUp_subcase("active-writer")
        self.prepare(hooks=runners.RunHooks(self.preflight, Mock(return_value=None),
                                            self.quiescent, self.offhost, self.check))
        with self.assertRaisesRegex(RuntimeError, "writers active"):
            self.execute()
        self.assertTrue(self.running)
        self.assertEqual(self.pending()["phase"], "inputs-verified")
        self.assertTrue(self.store.status()["blocked"])

    def test_incomplete_or_extra_independent_checks_do_not_finalize(self):
        for result in ({}, {"routing": "passed", "unexpected": "passed"}, None, True):
            self.setUp_subcase(str(len(list(self.root.iterdir()))))
            self.prepare(hooks=runners.RunHooks(self.preflight, self.stop, self.quiescent,
                                                self.offhost, Mock(return_value=result)))
            with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                    patch.object(runners, "run_checked", side_effect=self.apply):
                with self.assertRaises(runners.RunError):
                    self.execute()
            self.assertEqual(self.pending()["phase"], "apply-complete")
            self.assertTrue(self.store.status()["blocked"])

    def test_mutated_installed_inputs_are_detected_before_apply(self):
        self.prepare()
        def corrupt(*args):
            result = self.load(*args)
            path = self.paths["runtime"] / self.transaction.operation / "credentials.input"
            path.chmod(0o600)
            path.write_text("wrong")
            path.chmod(0o400)
            return result
        with patch.object(runners, "load_reserved_bundle", side_effect=corrupt), \
                patch.object(runners, "run_checked") as apply:
            with self.assertRaises(InputError):
                self.execute()
            apply.assert_not_called()
        self.assertEqual(self.pending()["phase"], "backup-complete")
        self.assertTrue(self.store.status()["blocked"])

    def test_failed_recorded_update_preserves_previous_committed_selection(self):
        self.prepare()
        with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                patch.object(runners, "run_checked", side_effect=self.apply):
            self.execute()
        before = self.store.status()["deployments"]["demo"]["committed"]
        self.prepare(hooks=runners.RunHooks(self.preflight, self.stop, self.quiescent,
                                            self.offhost, lambda images: {"routing": "failed"}))
        with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                patch.object(runners, "run_checked", side_effect=self.apply):
            with self.assertRaises(runners.RunError):
                self.execute()
        state = self.store.status()
        self.assertEqual(state["deployments"]["demo"]["committed"], before)
        self.assertEqual(self.pending()["phase"], "apply-complete")
        self.assertTrue(state["blocked"])

    def test_finalization_write_failure_retains_all_successful_pending_evidence(self):
        self.prepare()
        with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                patch.object(runners, "run_checked", side_effect=self.apply), \
                patch.object(self.store, "finalize", side_effect=StateError("uncertain persistence")):
            with self.assertRaises(StateError):
                self.execute()
        self.assertEqual(self.pending()["phase"], "checks-complete")
        self.assertTrue(self.store.status()["blocked"])
        self.assertIsNone(self.store.status()["deployments"]["demo"]["committed"])
        self.assertTrue((self.paths["backups"] / self.transaction.operation / "manifest.json").is_file())
        self.assertTrue((self.paths["runtime"] / self.transaction.operation / "credentials.input").is_file())
        with self.assertRaises(runners.RunError):
            self.execute()

    def test_controlled_ansible_timeout_preserves_backup_and_blocks_retry(self):
        self.prepare()
        def timeout(argv, **kwargs):
            self.assertEqual(kwargs["timeout"], 600)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        with patch.object(runners, "load_reserved_bundle", side_effect=self.load), \
                patch("tool.deployment_completion.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(StateError, "target work may continue"):
                self.execute()
        self.assertEqual(self.pending()["phase"], "backup-complete")
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue((self.paths["backups"] / self.transaction.operation / "manifest.json").is_file())
        with self.assertRaises(runners.RunError):
            self.execute()

    def test_controlled_ansible_rejects_nonprivate_or_missing_extra_variables(self):
        from tool.deployment_completion import run_checked
        path = self.paths["runtime"] / "variables.json"
        path.write_text("{}")
        path.chmod(0o600)
        link = self.paths["runtime"] / "link"
        link.symlink_to(path)
        for variables in (path, link, self.paths["runtime"] / "missing"):
            with self.subTest(variables=variables), \
                    patch("tool.deployment_completion.subprocess.run") as invoke:
                with self.assertRaises(ValueError):
                    run_checked(ansible=ANSIBLE, playbook=self.playbook, inventory=self.inventory,
                                variables=variables, evidence=self.paths["evidence"] / "completion.jsonl",
                                operation=str(uuid4()), host="fixture", required=["apply"])
                invoke.assert_not_called()

    def test_published_candidate_uses_the_same_backup_apply_checks_and_commit(self):
        from tool.deployment_images import resolve
        from test_deployment_images import FixtureRegistry
        selection = resolve("external", "linux/arm64", FixtureRegistry())
        self.prepare(selection=selection)
        expected = {"web": selection["images"]["web"]["config_digest"]}
        def registry(store, deployment, operation, intent, selected, evidence):
            self.events.append("registry")
            self.assertEqual(self.pending()["phase"], "backup-complete")
            store.start_registry_pull(deployment, operation, selected, intent)
            return expected
        def apply(**kwargs):
            self.assertEqual(json.loads(kwargs["variables"].read_bytes())["deployment_images"], expected)
            self.running = True
            return {"apply": "passed"}
        with patch.object(runners, "load_registry_candidate", side_effect=registry), \
                patch.object(runners, "load_reserved_bundle") as local, \
                patch.object(runners, "run_checked", side_effect=apply):
            self.execute()
            local.assert_not_called()
        committed = self.store.status()["deployments"]["demo"]["committed"]["selection"]
        self.assertNotIn("loaded_images", committed)
        self.assertEqual(committed["candidate"], selection)
        self.assertFalse(self.store.status()["blocked"])

    def test_unsafe_overlapping_storage_or_required_ids_fail_before_mutation(self):
        for overrides in ({"runtime": self.paths["staging"]}, {"required_checks": ["apply"]},
                          {"recovery_sources": {}}, {"protected_inputs": {"playbook": self.secret}},
                          {"recovery_sources": {"state": self.store.root}}):
            with self.subTest(overrides=overrides), self.assertRaises(runners.RunError):
                self.prepare(**overrides)
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(list(self.paths["staging"].iterdir()), [])

    @unittest.skipUnless(ANSIBLE.is_file(), "Local Ansible required")
    def test_real_ansible_uses_protected_inputs_loaded_ids_and_complete_callback(self):
        self.real_ansible_run(actual_images=False)

    def real_ansible_run(self, *, actual_images):
        output = self.paths["live"] / "applied.json"
        self.playbook.write_text(json.dumps([{
            "hosts": "all", "gather_facts": False, "tasks": [{
                "ansible.builtin.copy": {
                    "content": "{{ deployment_images | to_json }}", "dest": str(output), "mode": "0600"},
                "tags": ["deployment_required_apply"],
            }],
        }]))
        def check(images):
            self.events.append("checks")
            self.assertEqual(json.loads(output.read_text()), images)
            self.assertEqual(self.pending()["phase"], "apply-complete")
            return {"routing": "passed"}
        self.prepare(hooks=runners.RunHooks(self.preflight, self.stop, self.quiescent, self.offhost, check))
        with (nullcontext() if actual_images else
              patch.object(runners, "load_reserved_bundle", side_effect=self.load)):
            result = self.execute()
        self.assertEqual(result["required"], {"apply": "passed", "routing": "passed"})
        self.assertFalse(self.store.status()["blocked"])
        self.assertEqual(json.loads(output.read_text()), result["images"])
        evidence = self.paths["evidence"] / (self.transaction.operation + ".completion.jsonl")
        self.assertIn('"event": "end"', evidence.read_text())
        self.assertNotIn("synthetic-private-token", evidence.read_text())
        return result

    @unittest.skipUnless(ANSIBLE.is_file() and os.environ.get("RUSSOUND_RUN_LOCAL_BUILD_TESTS") == "1",
                         "Opt in to real Docker build/export/load and Ansible transaction")
    def test_real_build_load_backup_ansible_checks_and_finalization(self):
        docker = ["docker", "--context", builds.local_docker()]
        info = json.loads(builds.command(docker + ["info", "--format", "{{json .}}"]))
        arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
        self.platform = info["OSType"] + "/" + arch
        source = self.root / "build-source"
        source.mkdir(mode=0o700)
        for args in (["init", "--quiet"], ["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "Fixture"]):
            builds.command(["git", "-C", str(source), *args])
        (source / "marker").write_text("one-shot deployment run " + str(uuid4()))
        (source / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
        builds.command(["git", "-C", str(source), "add", "."])
        builds.command(["git", "-C", str(source), "commit", "--quiet", "-m", "Fixture"])
        revision = builds.command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
        artifacts = self.root / "artifacts"
        artifacts.mkdir(mode=0o700)
        self.bundle, self.digest = builds.build(source, revision, artifacts, "external", self.platform)
        manifest = builds.verify_bundle(self.bundle, self.digest, "external", self.platform)
        tag = manifest["images"]["web"]["tag"]
        try:
            builds.command(docker + ["image", "rm", tag])
            result = self.real_ansible_run(actual_images=True)
            actual = json.loads(builds.command(docker + ["image", "inspect", tag]))[0]
            builds.image_identity(actual, result["images"]["web"], self.platform, revision)
        finally:
            present = builds.command(docker + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
            if tag in present.decode().splitlines():
                builds.command(docker + ["image", "rm", tag])


if __name__ == "__main__":
    unittest.main()
from contextlib import nullcontext
