import copy
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tool import deployment_build as builds
from tool import deployment_load as loads
from tool.deployment_local import validate_candidate
from tool.deployment_images import SelectionError
from tool.deployment_state import StateError, StateStore
from test_deployment_build import fixture_bundle, rewrite_manifest


ROOT = Path(__file__).resolve().parents[1]
INTENT = "sha256:" + "d" * 64


def oci_bundle(parent):
    bundle, _, manifest = fixture_bundle(parent)
    image = manifest["images"]["web"]
    path = bundle / image["archive"]
    with tarfile.open(path) as archive:
        entries = {}
        for member in archive:
            with archive.extractfile(member) as source:
                entries[member.name] = source.read()
    blobs = {}
    for name in ("config.json", "layer.tar"):
        digest = hashlib.sha256(entries[name]).hexdigest()
        blobs[name] = "blobs/sha256/" + digest
    descriptor = json.dumps({
        "config": {"digest": "sha256:" + blobs["config.json"].split("/")[-1]},
        "layers": [{"digest": "sha256:" + blobs["layer.tar"].split("/")[-1]}],
    }).encode()
    root = hashlib.sha256(descriptor).hexdigest()
    payloads = {blobs[name]: entries[name] for name in blobs}
    payloads.update({
        "blobs/sha256/" + root: descriptor,
        "index.json": json.dumps({"manifests": [{"digest": "sha256:" + root}]}).encode(),
        "manifest.json": json.dumps([{"Config": blobs["config.json"],
                                     "RepoTags": [image["tag"]], "Layers": [blobs["layer.tar"]]}]).encode(),
    })
    path.chmod(0o600)
    with tarfile.open(path, "w") as archive:
        for name, raw in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    path.chmod(0o400)
    image.update(reference="sha256:" + root, bytes=path.stat().st_size,
                 sha256="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
    return bundle, rewrite_manifest(bundle, manifest)


class ReservedImageLoadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-load-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.bundle, self.digest, _ = fixture_bundle(self.root, "managed")
        self.selection = loads.select_bundle(self.bundle, self.digest, "managed", "linux/arm64")
        self.store = StateStore(self.root / "state", owner_uid=os.getuid())
        self.store.initialize()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.operation = str(uuid4())
        self.mutations = []

    def begin(self, ready=True):
        self.store.begin("demo", self.operation, self.selection, INTENT, ["apply", "routing"])
        if ready:
            self.store.advance("demo", self.operation, "inputs-verified")
            self.store.advance("demo", self.operation, "backup-complete")

    def pending(self):
        return self.store.status()["deployments"]["demo"]["pending"]

    def inspect(self, component, classic=False):
        manifest = self.selection["manifest"]
        return {"Id": self.selection["configs"][component] if classic else manifest["images"][component]["reference"],
                "Os": "linux", "Architecture": "arm64", "Config": {"Labels": {
                    "org.opencontainers.image.revision": manifest["revision"],
                    "org.opencontainers.image.source": builds.SOURCE}}}

    def fake_command(self, argv):
        if "info" in argv:
            return json.dumps({"OSType": "linux", "Architecture": "aarch64", "SecurityOptions": []}).encode()
        if "ls" in argv:
            return b""
        if "inspect" in argv:
            component = "ser2net" if "/ser2net:" in argv[-1] else "web"
            return json.dumps([self.inspect(component)]).encode()
        self.fail("Unexpected Docker command: " + repr(argv))

    def fake_run(self, argv, **kwargs):
        self.assertEqual(argv, ["docker", "--context", "fixture", "image", "load"])
        self.assertTrue((self.store.root / "reservation/image-load.json").is_file())
        self.assertEqual(self.pending()["phase"], "backup-complete")
        self.assertIsNone(self.pending()["loaded_images"])
        self.assertTrue(kwargs["stdin"].read(512))
        kwargs["stdout"].write(b"private load diagnostics\n")
        self.mutations.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def load(self, **overrides):
        values = {"store": self.store, "deployment": "demo", "operation": self.operation,
                  "intent_digest": INTENT, "selection": self.selection,
                  "bundle": self.bundle, "evidence": self.evidence}
        return loads.load_reserved_bundle(**{**values, **overrides})

    def docker(self, command=None, run=None):
        stack = ExitStack()
        stack.enter_context(patch.object(loads, "local_engine", return_value="fixture"))
        stack.enter_context(patch.object(loads, "command", side_effect=command or self.fake_command))
        stack.enter_context(patch.object(loads.subprocess, "run", side_effect=run or self.fake_run))
        return stack

    def test_local_record_load_check_finalize_and_next_operation(self):
        self.begin(ready=False)
        self.store.start_image_transfer("demo", self.operation, self.selection, INTENT)
        self.store.complete_image_transfer("demo", self.operation, self.selection, INTENT)
        self.store.advance("demo", self.operation, "inputs-verified")
        self.store.advance("demo", self.operation, "backup-complete")
        with self.docker():
            loaded = self.load()
        self.assertEqual(loaded, {name: image["reference"]
                                 for name, image in self.selection["manifest"]["images"].items()})
        self.assertEqual(self.pending()["loaded_images"], loaded)
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertTrue(self.store.status()["blocked"])
        log = self.evidence / (self.operation + ".image-load.log")
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"private load diagnostics", log.read_bytes())
        self.store.advance("demo", self.operation, "apply-complete")
        self.store.advance("demo", self.operation, "checks-complete")
        self.store.finalize("demo", self.operation, {
            "operation": self.operation, "intent_digest": INTENT, "invocation": "verified-complete",
            "required": {"apply": "passed", "routing": "passed"},
        })
        state = self.store.status()
        self.assertFalse(state["blocked"])
        committed = state["deployments"]["demo"]["committed"]["selection"]
        self.assertEqual(committed["candidate"], self.selection)
        self.assertEqual(committed["loaded_images"], loaded)
        self.assertFalse(state["image_load_started"])
        self.assertFalse(state["image_transfer_started"])
        self.operation = str(uuid4())
        self.begin(ready=False)
        self.assertEqual(self.pending()["candidate"], self.selection)
        self.assertEqual(self.store.status()["deployments"]["demo"]["committed"]["selection"], committed)

    def test_invalid_local_candidates_never_reserve(self):
        for key, value in (("configs", {}), ("manifest_sha256", "bad"), ("kind", "other"),
                           ("schema_version", True), ("manifest", {"gateway": None})):
            selection = {**self.selection, key: value}
            with self.subTest(key=key), self.assertRaises((StateError, SelectionError)):
                self.store.begin("demo", self.operation, selection, INTENT, ["apply"])
            self.assertFalse(self.store.status()["blocked"])
        selection = copy.deepcopy(self.selection)
        selection["manifest"]["images"]["web"]["reference"] = "web:dev"
        with self.assertRaises(SelectionError):
            validate_candidate(selection)

    def test_loaded_state_cannot_skip_image_load_or_accept_unrelated_ids(self):
        self.begin()
        before = (self.store.root / "demo.json").read_bytes()
        with self.assertRaises(StateError):
            self.store.advance("demo", self.operation, "apply-complete")
        images = {name: image["reference"] for name, image in self.selection["manifest"]["images"].items()}
        with self.assertRaises(StateError):
            self.store.complete_image_load("demo", self.operation, self.selection, INTENT, images)
        self.store.start_image_load("demo", self.operation, self.selection, INTENT)
        for result in ({}, {**images, "web": "sha256:" + "f" * 64}, {**images, "extra": images["web"]}):
            with self.assertRaises(StateError):
                self.store.complete_image_load("demo", self.operation, self.selection, INTENT, result)
        self.assertEqual((self.store.root / "demo.json").read_bytes(), before)
        self.assertTrue(self.store.status()["blocked"])
        marker = self.store.root / "reservation/image-load.json"
        marker.write_text(json.dumps({"operation": self.operation, "intent_digest": "sha256:" + "e" * 64}))
        with self.assertRaises(StateError):
            self.store.complete_image_load("demo", self.operation, self.selection, INTENT, images)
        self.assertEqual((self.store.root / "demo.json").read_bytes(), before)

    def test_no_docker_mutation_without_matching_reservation_approval_and_phase(self):
        with self.docker(), self.assertRaises(StateError):
            self.load()
        self.begin(ready=False)
        for overrides in ({}, {"intent_digest": "sha256:" + "e" * 64},
                          {"operation": str(uuid4())}, {"deployment": "other"}):
            evidence = self.root / str(uuid4())
            evidence.mkdir(mode=0o700)
            with self.subTest(overrides=overrides), self.docker(), self.assertRaises(StateError):
                self.load(evidence=evidence, **overrides)
        self.assertFalse(self.mutations)
        self.assertFalse(self.store.status()["image_load_started"])

    def test_pair_failure_leaves_pending_and_no_automatic_retry(self):
        self.begin()
        count = 0

        def partial(argv, **kwargs):
            nonlocal count
            count += 1
            self.fake_run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 1 if count == 2 else 0)

        with self.docker(run=partial), self.assertRaises(loads.LoadError):
            self.load()
        self.assertEqual(len(self.mutations), 2)
        self.assertIsNone(self.pending()["loaded_images"])
        self.assertEqual(self.pending()["phase"], "backup-complete")
        fresh = self.root / "retry-evidence"
        fresh.mkdir(mode=0o700)
        with self.docker(), self.assertRaises(StateError):
            self.load(evidence=fresh)
        self.assertEqual(len(self.mutations), 2)
        with self.assertRaises(StateError):
            self.store.begin("different", str(uuid4()), self.selection, INTENT, ["apply"])

    def test_process_loss_after_durable_marker_blocks_retry(self):
        self.begin()
        code = """
import json, os, sys
from pathlib import Path
from tool.deployment_state import StateStore
store = StateStore(Path(sys.argv[1]), owner_uid=os.getuid())
pending = store.status()['deployments']['demo']['pending']
store.start_image_load('demo', pending['operation'], pending['candidate'], pending['intent_digest'])
os._exit(23)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.store.root)], cwd=ROOT, timeout=10)
        self.assertEqual(child.returncode, 23)
        with self.docker(), self.assertRaises(StateError):
            self.load()
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertFalse(self.mutations)

    def test_timeout_preserves_uncertainty(self):
        self.begin()
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 600)
        with self.docker(run=timeout):
            with self.assertRaises(loads.LoadError):
                self.load()
        self.assertIsNone(self.pending()["loaded_images"])
        self.assertTrue(self.store.status()["image_load_started"])

    def test_receipt_write_failure_keeps_reservation_after_daemon_mutation(self):
        self.begin()
        write = self.store._write
        def fail_receipt(directory, name, value, **kwargs):
            if name == "demo.json":
                raise OSError("synthetic write failure")
            return write(directory, name, value, **kwargs)
        with self.docker(), patch.object(self.store, "_write", side_effect=fail_receipt):
            with self.assertRaises(StateError):
                self.load()
        self.assertEqual(len(self.mutations), 2)
        self.assertIsNone(self.pending()["loaded_images"])
        self.assertTrue(self.store.status()["blocked"])
        self.assertTrue(self.store.status()["image_load_started"])

    def test_load_marker_fsync_failure_prevents_docker_mutation(self):
        self.begin()
        write = self.store._write
        def uncertain(directory, name, value, **kwargs):
            write(directory, name, value, **kwargs)
            if name == "image-load.json":
                raise OSError("synthetic persistence uncertainty")
        with self.docker(), patch.object(self.store, "_write", side_effect=uncertain):
            with self.assertRaises(StateError):
                self.load()
        self.assertFalse(self.mutations)
        self.assertTrue(self.store.status()["image_load_started"])
        self.assertTrue(self.store.status()["blocked"])

    def test_transferred_artifact_drift_or_candidate_mismatch_precedes_load_marker(self):
        self.begin()
        wrong = copy.deepcopy(self.selection)
        wrong["configs"]["web"] = "sha256:" + "f" * 64
        with self.docker(), self.assertRaises(loads.LoadError):
            self.load(selection=wrong)
        archive = self.bundle / "web.tar"
        archive.chmod(0o600)
        with self.docker(), self.assertRaises(loads.LoadError):
            self.load()
        self.assertFalse(self.mutations)
        self.assertFalse(self.store.status()["image_load_started"])

    def test_remote_or_wrong_platform_daemon_and_tag_collision_do_not_mutate(self):
        self.begin()
        for info in ({"OSType": "linux", "Architecture": "x86_64"},
                     {"OSType": "linux", "Architecture": "arm64", "SecurityOptions": ["name=userns"]}):
            with self.docker(command=lambda argv: json.dumps(info).encode()), self.assertRaises(loads.LoadError):
                self.load()
        with patch.object(loads, "local_engine", side_effect=builds.BuildError("Remote endpoint")):
            with self.assertRaises(loads.LoadError):
                self.load()
        def collision(argv):
            return b"sha256:" + b"f" * 64 if "ls" in argv else self.fake_command(argv)
        with self.docker(command=collision), self.assertRaises(loads.LoadError):
            self.load()
        self.assertFalse(self.mutations)
        self.assertFalse(self.store.status()["image_load_started"])

    def test_archive_replaced_during_load_never_records_success(self):
        self.begin()
        def replace(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if len(self.mutations) == 1:
                path = self.bundle / "web.tar"
                raw = path.read_bytes()
                path.unlink()
                path.write_bytes(raw)
                path.chmod(0o400)
            return result
        with self.docker(run=replace), self.assertRaises(loads.LoadError):
            self.load()
        self.assertIsNone(self.pending()["loaded_images"])
        self.assertTrue(self.store.status()["image_load_started"])

    def test_classic_target_config_id_is_recorded_instead_of_controller_root_id(self):
        self.bundle, self.digest = oci_bundle(self.root)
        self.selection = loads.select_bundle(self.bundle, self.digest, "external", "linux/arm64")
        self.begin()
        def classic(argv):
            if "inspect" in argv:
                return json.dumps([self.inspect("web", classic=True)]).encode()
            return self.fake_command(argv)
        with self.docker(command=classic):
            loaded = self.load()
        self.assertNotEqual(loaded["web"], self.selection["manifest"]["images"]["web"]["reference"])
        self.assertEqual(loaded["web"], self.selection["configs"]["web"])
        self.assertEqual(self.pending()["loaded_images"], loaded)

    def test_wrong_loaded_id_platform_or_labels_never_records_complete_pair(self):
        for fault in ("id", "platform", "revision", "labels", "empty"):
            self.store = StateStore(self.root / ("state-" + fault), owner_uid=os.getuid())
            self.store.initialize()
            self.evidence = self.root / ("evidence-" + fault)
            self.evidence.mkdir(mode=0o700)
            self.begin()
            actual = self.inspect("web")
            if fault == "id":
                actual["Id"] = "sha256:" + "f" * 64
            elif fault == "platform":
                actual["Architecture"] = "amd64"
            elif fault == "revision":
                actual["Config"]["Labels"]["org.opencontainers.image.revision"] = "f" * 40
            elif fault == "labels":
                actual["Config"]["Labels"] = None
            def mismatch(argv):
                if "inspect" in argv:
                    return json.dumps([] if fault == "empty" else [actual]).encode()
                return self.fake_command(argv)
            with self.subTest(fault=fault), self.docker(command=mismatch), self.assertRaises(loads.LoadError):
                self.load()
            self.assertIsNone(self.pending()["loaded_images"])
            self.assertTrue(self.store.status()["blocked"])


@unittest.skipUnless(os.environ.get("RUSSOUND_RUN_LOCAL_BUILD_TESTS") == "1",
                     "Opt in to real local Docker build/save/reserved-load")
class ActualReservedLoadTests(unittest.TestCase):
    def test_real_private_copy_and_reserved_load_preserve_exact_pair(self):
        docker = ["docker", "--context", builds.local_docker()]
        info = json.loads(builds.command(docker + ["info", "--format", "{{json .}}"]))
        architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
        platform = info["OSType"] + "/" + architecture
        with tempfile.TemporaryDirectory(prefix="russound-real-load-") as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            source = root / "source"
            source.mkdir()
            for args in (["init", "--quiet"], ["config", "user.email", "fixture@example.invalid"],
                         ["config", "user.name", "Fixture"]):
                builds.command(["git", "-C", str(source), *args])
            (source / "marker").write_text("reserved load fixture " + str(uuid4()))
            for name in ("Dockerfile", "Dockerfile.ser2net"):
                (source / name).write_text("FROM scratch\nCOPY marker /marker\n")
            builds.command(["git", "-C", str(source), "add", "."])
            builds.command(["git", "-C", str(source), "commit", "--quiet", "-m", "Fixture"])
            revision = builds.command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
            artifacts = root / "builds"
            artifacts.mkdir(mode=0o700)
            bundle, checksum = builds.build(source, revision, artifacts, "managed", platform)
            manifest = builds.verify_bundle(bundle, checksum, "managed", platform)
            tags = {image["tag"] for image in manifest["images"].values()}
            try:
                copied = root / "received"
                shutil.copytree(bundle, copied)
                selection = loads.select_bundle(copied, checksum, "managed", platform)
                for tag in tags:
                    builds.command(docker + ["image", "rm", tag])
                state = StateStore(root / "state", owner_uid=os.getuid())
                state.initialize()
                operation = str(uuid4())
                state.begin("fixture", operation, selection, INTENT, ["image-load", "runtime"])
                state.advance("fixture", operation, "inputs-verified")
                state.advance("fixture", operation, "backup-complete")
                evidence = root / "evidence"
                evidence.mkdir(mode=0o700)
                loaded = loads.load_reserved_bundle(state, "fixture", operation, INTENT,
                                                    selection, copied, evidence)
                for name, reference in loaded.items():
                    actual = json.loads(builds.command(
                        docker + ["image", "inspect", manifest["images"][name]["tag"]]))[0]
                    builds.image_identity(actual, reference, platform, revision)
                pending = state.status()["deployments"]["fixture"]["pending"]
                self.assertEqual(pending["loaded_images"], loaded)
                self.assertEqual(pending["phase"], "backup-complete")
                self.assertTrue(state.status()["blocked"])
                self.assertIsNone(state.status()["deployments"]["fixture"]["committed"])
                with self.assertRaises((StateError, loads.LoadError)):
                    loads.load_reserved_bundle(state, "fixture", operation, INTENT, selection, copied, evidence)
            finally:
                present = builds.command(docker + ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
                for tag in tags & set(present.decode().splitlines()):
                    builds.command(docker + ["image", "rm", tag])


if __name__ == "__main__":
    unittest.main()
