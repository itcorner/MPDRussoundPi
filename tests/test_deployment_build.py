import copy
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

import yaml

from tool import deployment_build as builds


ROOT = Path(__file__).resolve().parents[1]


def image_archive(path, tag, revision, platform="linux/arm64", *, bad_tag=False):
    system, architecture = platform.split("/")
    config = json.dumps({
        "os": system, "architecture": architecture,
        "config": {"Labels": {"org.opencontainers.image.revision": revision,
                              "org.opencontainers.image.source": builds.SOURCE}},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]},
    }).encode()
    reference = "sha256:" + hashlib.sha256(config).hexdigest()
    entries = {
        "config.json": config, "layer.tar": b"synthetic-layer",
        "manifest.json": json.dumps([{"Config": "config.json",
                                     "RepoTags": ["unrelated:overwrite" if bad_tag else tag],
                                     "Layers": ["layer.tar"]}]).encode(),
    }
    with tarfile.open(path, "w") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    path.chmod(0o400)
    return reference


def fixture_bundle(parent, gateway="external", platform="linux/arm64"):
    operation = str(uuid4())
    bundle = parent / operation
    bundle.mkdir(mode=0o700)
    revision = "a" * 40
    images = {}
    for name in (("web", "ser2net") if gateway == "managed" else ("web",)):
        archive = bundle / (name + ".tar")
        tag = f"mpdrussoundpi-local/{name}:{operation}"
        reference = image_archive(archive, tag, revision, platform)
        raw = archive.read_bytes()
        images[name] = {"reference": reference, "tag": tag, "archive": archive.name,
                        "bytes": len(raw), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    manifest = {
        "schema_version": 1, "kind": "local-development-image-bundle", "operation": operation,
        "revision": revision, "tree": "b" * 40, "gateway": gateway, "platform": platform,
        "built_at": "2026-11-13T21:00:00+00:00", "build_network": False, "images": images,
    }
    digest = rewrite_manifest(bundle, manifest)
    return bundle, digest, manifest


def rewrite_manifest(bundle, value):
    path = bundle / "manifest.json"
    if path.exists():
        path.chmod(0o600)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    path.chmod(0o400)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class LocalBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="russound-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.source / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
        (self.source / "Dockerfile.ser2net").write_text("FROM scratch\nCOPY marker /marker\n")
        (self.source / "marker").write_text("source fixture\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.storage = self.root / "bundles"
        self.storage.mkdir(mode=0o700)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True).strip()

    def test_requires_full_matching_revision_and_clean_tracked_and_untracked_source(self):
        for revision in ("HEAD", self.revision[:8], "f" * 40):
            with self.subTest(revision=revision), self.assertRaises(builds.BuildError):
                builds.clean_source(self.source, revision)
        tree = builds.clean_source(self.source, self.revision)
        self.assertEqual(len(tree), 40)
        (self.source / "marker").write_text("dirty\n")
        with self.assertRaisesRegex(builds.BuildError, "clean source"):
            builds.clean_source(self.source, self.revision)
        self.assertEqual((self.source / "marker").read_text(), "dirty\n")
        (self.source / "marker").write_text("source fixture\n")
        (self.source / "untracked").touch()
        with self.assertRaisesRegex(builds.BuildError, "clean source"):
            builds.clean_source(self.source, self.revision)

    def test_snapshot_uses_committed_bytes_never_ignored_files(self):
        (self.source / ".git/info/exclude").write_text("private\n")
        (self.source / "private").write_text("must not enter context")
        staging = self.root / "staging"
        staging.mkdir()
        builds.source_context(self.source, self.revision, staging)
        self.assertEqual((staging / "context/marker").read_text(), "source fixture\n")
        self.assertFalse((staging / "context/private").exists())
        self.assertFalse((staging / "context/.git").exists())
        builds.clean_source(self.source, self.revision)

    def test_source_links_are_not_extracted(self):
        (self.source / "link").symlink_to("/etc/passwd")
        self.git("add", "link")
        self.git("commit", "--quiet", "-m", "Link fixture")
        revision = self.git("rev-parse", "HEAD")
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaisesRegex(builds.BuildError, "unsupported entries"):
            builds.source_context(self.source, revision, staging)
        self.assertFalse((staging / "context/link").exists())

    def test_private_umask_does_not_make_packaged_static_directories_unreadable(self):
        assets = self.source / "web/static"
        assets.mkdir(parents=True)
        (assets / "index.html").write_text("fixture")
        self.git("add", "web")
        self.git("commit", "--quiet", "-m", "Nested assets")
        revision = self.git("rev-parse", "HEAD")
        staging = self.root / "staging"
        staging.mkdir()
        previous = os.umask(0o077)
        try:
            builds.source_context(self.source, revision, staging)
        finally:
            os.umask(previous)
        self.assertEqual((staging / "context/web").stat().st_mode & 0o777, 0o755)
        self.assertEqual((staging / "context/web/static").stat().st_mode & 0o777, 0o755)
        self.assertEqual((staging / "context/web/static/index.html").stat().st_mode & 0o777, 0o644)

    def test_local_context_and_builder_required_even_if_remote_builder_is_selected(self):
        context = [{"Name": "fixture", "Endpoints": {"docker": {"Host": "unix:///fixture.sock"}}}]
        builder = {"Name": "fixture", "Driver": "docker", "Nodes": [{"Endpoint": "fixture"}]}
        with patch.dict(os.environ, {}, clear=True), patch.object(
                builds, "command", side_effect=[json.dumps(context).encode(), json.dumps(builder).encode()]):
            self.assertEqual(builds.local_docker(), "fixture")
        for host in ("ssh://private-host", "tcp://127.0.0.1:2375"):
            context[0]["Endpoints"]["docker"]["Host"] = host
            with patch.dict(os.environ, {}, clear=True), patch.object(
                    builds, "command", return_value=json.dumps(context).encode()):
                with self.assertRaises(builds.BuildError):
                    builds.local_docker()
        context[0]["Endpoints"]["docker"]["Host"] = "unix:///fixture.sock"
        builder["Driver"] = "remote"
        with patch.dict(os.environ, {}, clear=True), patch.object(
                builds, "command", side_effect=[json.dumps(context).encode(), json.dumps(builder).encode()]):
            with self.assertRaises(builds.BuildError):
                builds.local_docker()
        for variable in ("DOCKER_HOST", "DOCKER_CONTEXT"):
            with patch.dict(os.environ, {variable: "override"}), self.assertRaises(builds.BuildError):
                builds.local_docker()
        for context in (None, {}, [None], [{"Endpoints": []}], [{"Endpoints": {"docker": None}}]):
            with patch.dict(os.environ, {}, clear=True), patch.object(
                    builds, "command", return_value=json.dumps(context).encode()):
                with self.assertRaises(builds.BuildError):
                    builds.local_docker()

    def test_copied_bundle_verifies_offline_with_separately_retained_identity(self):
        bundle, digest, manifest = fixture_bundle(self.storage, "managed")
        destination = self.root / "copied"
        shutil.copytree(bundle, destination)
        with patch.object(builds, "command", side_effect=AssertionError("No Docker/Git/network during verify")):
            self.assertEqual(builds.verify_bundle(destination, digest, "managed", "linux/arm64"), manifest)
        self.assertEqual(sorted(path.name for path in destination.iterdir()),
                         ["manifest.json", "ser2net.tar", "web.tar"])

    def test_invalid_identity_platform_gateway_manifest_or_archive_blocks_verification(self):
        bundle, digest, manifest = fixture_bundle(self.storage)
        for checksum, gateway, platform in (
                ("invalid", "external", "linux/arm64"), ("sha256:" + "0" * 64, "external", "linux/arm64"),
                (digest, "managed", "linux/arm64"), (digest, "external", "linux/amd64")):
            with self.subTest(checksum=checksum, gateway=gateway, platform=platform):
                with self.assertRaises(builds.BuildError):
                    builds.verify_bundle(bundle, checksum, gateway, platform)
        archive = bundle / "web.tar"
        archive.chmod(0o600)
        with self.assertRaises(builds.BuildError):
            builds.verify_bundle(bundle, digest, "external", "linux/arm64")
        archive.write_bytes(b"truncated")
        archive.chmod(0o400)
        with self.assertRaises(builds.BuildError):
            builds.verify_bundle(bundle, digest, "external", "linux/arm64")

    def test_malformed_descriptors_and_archive_links_or_other_tags_are_rejected(self):
        bundle, _, manifest = fixture_bundle(self.storage)
        for key, value in (("archive", "../escape"), ("bytes", True), ("reference", "mutable:tag"),
                           ("tag", "unrelated:overwrite"), ("sha256", "bad")):
            candidate = copy.deepcopy(manifest)
            candidate["images"]["web"][key] = value
            digest = rewrite_manifest(bundle, candidate)
            with self.subTest(field=key), self.assertRaises(builds.BuildError):
                builds.verify_bundle(bundle, digest, "external", "linux/arm64")
        archive = bundle / "web.tar"
        archive.chmod(0o600)
        image_archive(archive, manifest["images"]["web"]["tag"], manifest["revision"], bad_tag=True)
        manifest["images"]["web"]["sha256"] = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        digest = rewrite_manifest(bundle, manifest)
        with self.assertRaisesRegex(builds.BuildError, "selected local image tag"):
            builds.verify_bundle(bundle, digest, "external", "linux/arm64")
        archive.chmod(0o600)
        with tarfile.open(archive, "w") as tar:
            member = tarfile.TarInfo("../outside")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            tar.addfile(member)
        archive.chmod(0o400)
        manifest["images"]["web"].update(
            sha256="sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size)
        digest = rewrite_manifest(bundle, manifest)
        with self.assertRaises(builds.BuildError):
            builds.verify_bundle(bundle, digest, "external", "linux/arm64")
        self.assertFalse((self.root / "outside").exists())

    def fake_docker(self, fail_component=None):
        real = builds.command
        calls = []

        def execute(argv, **kwargs):
            if argv[0] != "docker":
                return real(argv, **kwargs)
            calls.append(argv)
            if "buildx" in argv:
                if fail_component and f"mpdrussoundpi-local/{fail_component}:" in " ".join(argv):
                    raise builds.BuildError("fixture build failure")
                return b""
            if "save" in argv:
                tag = argv[-1]
                image_archive(Path(argv[argv.index("--output") + 1]), tag, self.revision)
                return b""
            tag = argv[-1]
            config = json.dumps({
                "os": "linux", "architecture": "arm64",
                "config": {"Labels": {"org.opencontainers.image.revision": self.revision,
                                      "org.opencontainers.image.source": builds.SOURCE}},
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]},
            }).encode()
            return json.dumps([{"Id": "sha256:" + hashlib.sha256(config).hexdigest(),
                                "Os": "linux", "Architecture": "arm64",
                                "Config": json.loads(config)["config"]}]).encode()
        return execute, calls

    def test_build_pair_exports_private_complete_bundle_and_uses_pinned_local_builder(self):
        execute, calls = self.fake_docker()
        with patch.object(builds, "local_docker", return_value="fixture"), patch.object(
                builds, "command", side_effect=execute):
            bundle, digest = builds.build(self.source, self.revision, self.storage, "managed", "linux/arm64")
        value = builds.verify_bundle(bundle, digest, "managed", "linux/arm64")
        self.assertEqual(set(value["images"]), {"web", "ser2net"})
        self.assertEqual((bundle / "manifest.json").stat().st_mode & 0o777, 0o400)
        self.assertEqual((bundle / "build.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
        for argv in calls:
            self.assertEqual(argv[:3], ["docker", "--context", "fixture"])
            if "buildx" in argv:
                self.assertEqual(argv[argv.index("--builder") + 1], "fixture")
                self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertFalse(any(path.is_dir() for path in bundle.iterdir()))
        self.assertEqual(builds.clean_source(self.source, self.revision), value["tree"])

    def test_partial_pair_failure_retains_first_archive_but_no_manifest(self):
        execute, _ = self.fake_docker("ser2net")
        with patch.object(builds, "local_docker", return_value="fixture"), patch.object(
                builds, "command", side_effect=execute):
            with self.assertRaisesRegex(builds.BuildError, "fixture build failure"):
                builds.build(self.source, self.revision, self.storage, "managed", "linux/arm64")
        bundle = next(self.storage.iterdir())
        self.assertTrue((bundle / "web.tar").is_file())
        self.assertTrue((bundle / "build.log").is_file())
        self.assertFalse((bundle / "manifest.json").exists())
        self.assertFalse((bundle / "ser2net.tar").exists())

    def test_dirty_source_fails_before_docker_and_output_mutation(self):
        (self.source / "marker").write_text("do not discard\n")
        with patch.object(builds, "local_docker") as docker:
            with self.assertRaisesRegex(builds.BuildError, "clean source"):
                builds.build(self.source, self.revision, self.storage, "external", "linux/arm64")
        docker.assert_not_called()
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_build_network_is_opt_in_and_source_drift_retains_unusable_evidence(self):
        execute, calls = self.fake_docker()

        def mutate_after_save(argv, **kwargs):
            output = execute(argv, **kwargs)
            if argv[0] == "docker" and "save" in argv:
                (self.source / "marker").write_text("changed during build\n")
            return output

        with patch.object(builds, "local_docker", return_value="fixture"), patch.object(
                builds, "command", side_effect=mutate_after_save):
            with self.assertRaisesRegex(builds.BuildError, "clean source"):
                builds.build(self.source, self.revision, self.storage, "external", "linux/arm64",
                             allow_build_network=True)
        bundle = next(self.storage.iterdir())
        self.assertFalse((bundle / "manifest.json").exists())
        self.assertTrue((bundle / "web.tar").exists())
        build_command = next(argv for argv in calls if "buildx" in argv)
        self.assertEqual(build_command[build_command.index("--network") + 1], "default")
        self.assertEqual((self.source / "marker").read_text(), "changed during build\n")

    def test_archive_file_symlink_hardlink_and_missing_payload_block_reuse(self):
        for fault in ("symlink", "hardlink", "missing"):
            bundle, digest, _ = fixture_bundle(self.storage)
            archive = bundle / "web.tar"
            original = self.root / str(uuid4())
            archive.rename(original)
            if fault == "symlink":
                archive.symlink_to(original)
            elif fault == "hardlink":
                os.link(original, archive)
            with self.subTest(fault=fault), self.assertRaises(builds.BuildError):
                builds.verify_bundle(bundle, digest, "external", "linux/arm64")

    def test_oci_identity_requires_existing_descriptor_graph(self):
        reference = "sha256:" + "f" * 64
        for roots, error in (
                ([{"digest": reference}], "metadata is missing"),
                ([{"digest": reference}, {"digest": reference}], "exactly one"),
                ([{"digest": reference, "annotations": {"io.containerd.image.name": "unrelated:overwrite"}}],
                 "unrelated image tag")):
            bundle, _, manifest = fixture_bundle(self.storage)
            manifest["images"]["web"]["reference"] = reference
            archive = bundle / "web.tar"
            archive.chmod(0o600)
            with tarfile.open(archive, "a") as tar:
                content = json.dumps({"manifests": roots}).encode()
                member = tarfile.TarInfo("index.json")
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            archive.chmod(0o400)
            manifest["images"]["web"].update(
                sha256="sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size)
            digest = rewrite_manifest(bundle, manifest)
            with self.subTest(error=error), self.assertRaisesRegex(builds.BuildError, error):
                builds.verify_bundle(bundle, digest, "external", "linux/arm64")

    def test_command_failures_are_redacted_and_no_success_bundle_is_reported(self):
        with patch("tool.deployment_build.subprocess.run",
                   return_value=subprocess.CompletedProcess(["docker"], 1, b"", b"private details")):
            with self.assertRaises(builds.BuildError) as caught:
                builds.command(["docker", "fixture"])
        self.assertNotIn("private details", str(caught.exception))
        result = subprocess.run(
            [sys.executable, "-m", "tool.deployment_build", "verify", "--bundle", str(self.storage),
             "--manifest-sha256", "sha256:" + "0" * 64], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Local build failed:", result.stderr)

    @unittest.skipUnless(os.environ.get("RUSSOUND_RUN_LOCAL_BUILD_TESTS") == "1", "Opt in to local Docker build")
    def test_real_scratch_build_archive_and_reverify(self):
        bundle, digest = builds.build(self.source, self.revision, self.storage, "managed", "linux/arm64")
        value = builds.verify_bundle(bundle, digest, "managed", "linux/arm64")
        try:
            copied = self.root / "copied-bundle"
            shutil.copytree(bundle, copied)
            verified = json.loads(subprocess.check_output(
                [sys.executable, "-m", "tool.deployment_build", "verify", "--bundle", str(copied),
                 "--manifest-sha256", digest, "--gateway", "managed", "--platform", "linux/arm64"],
                cwd=ROOT, env={**os.environ, "DOCKER_HOST": "ssh://invalid.example"}, text=True))
            self.assertEqual(verified["operation"], value["operation"])
            self.assertFalse(verified["target_changed"])
            for name, image in value["images"].items():
                inspected = json.loads(builds.command(["docker", "image", "inspect", image["reference"]]))[0]
                builds.image_identity(inspected, image["reference"], "linux/arm64", self.revision)
                self.assertGreater((bundle / (name + ".tar")).stat().st_size, 0)
            self.assertFalse(value["build_network"])
            for image in value["images"].values():
                builds.command(["docker", "image", "rm", image["tag"]])
            for image in value["images"].values():
                builds.command(["docker", "image", "load", "--input", str(bundle / image["archive"])])
                inspected = json.loads(builds.command(["docker", "image", "inspect", image["tag"]]))[0]
                builds.image_identity(inspected, image["reference"], "linux/arm64", self.revision)
        finally:
            owned_tags = {image["tag"] for image in value["images"].values()}
            present_tags = builds.command(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
            for tag in set(present_tags.decode().splitlines()) & owned_tags:
                builds.command(["docker", "image", "rm", tag])


ANSIBLE = Path(sys.executable).parent / "ansible-playbook"


@unittest.skipUnless(ANSIBLE.is_file() and shutil.which("docker"), "Local Ansible and Compose required")
class LocalBuildPreviewTests(unittest.TestCase):
    def test_local_build_entry_verifies_offline_and_never_connects_to_target(self):
        with tempfile.TemporaryDirectory(prefix="russound-build-preview-") as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            for gateway in ("managed", "external", "dummy"):
                bundle, digest, manifest = fixture_bundle(root, gateway)
                variables = {
                    "ansible_host": "invalid.example", "ansible_connection": "ssh",
                    "russound_local_bundle": str(bundle), "russound_local_manifest_sha256": digest,
                    "russound_gateway": gateway, "russound_data_dir": "/tmp/build-preview",
                    **({"russound_backend_host": "gateway.example"} if gateway == "external" else {}),
                    **({"russound_serial_gid": 20} if gateway == "managed" else {}),
                    **({"russound_dummy_image": "sha256:" + "c" * 64} if gateway == "dummy" else {}),
                }
                inventory = root / "inventory.yml"
                inventory.write_text(yaml.safe_dump({"all": {"hosts": {"preview": variables}}}))
                argv = [str(ANSIBLE), "-i", str(inventory), str(ROOT / "ansible/playbooks/deploy-dev-build.yml")]
                for flags in ([], ["--check"]):
                    result = subprocess.run(argv + flags, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("local-archive-source-platform-checksum", result.stdout)
                    self.assertIn(manifest["images"]["web"]["reference"], result.stdout)
                    self.assertIn("changed=0", result.stdout)
                for override in (
                        {"russound_action": "apply"}, {"russound_local_manifest_sha256": "sha256:" + "0" * 64},
                        {"russound_web_image": "itcornernet/mpdrussoundpi-web@sha256:" + "d" * 64},
                        {"russound_image_source": "hub"},
                        {"russound_image_source": "hub", "russound_local_bundle": "",
                         "russound_local_manifest_sha256": ""}):
                    result = subprocess.run(argv + ["-e", json.dumps(override)],
                                            capture_output=True, text=True, timeout=60)
                    self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
