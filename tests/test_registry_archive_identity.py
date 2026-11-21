"""Archived registry images (multi-platform index plus attestations) and the operator's identity check.

The layout mirrors what `docker image save` produces with the containerd image
store for an image pulled from the registry: the OCI root is the image index,
which lists the pulled platform's manifest and its BuildKit attestation and also
manifests for platforms that were never pulled (absent from the archive).
"""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest

from tool import deployment_build as builds

TAG = "mpdrussoundpi-recovery/web:43b8dbee-7ac3-444e-96cb-95cdf973162c"
IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
ATTESTATION = {"vnd.docker.reference.type": "attestation-manifest"}


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def dump(value) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def descriptor(content: bytes, media_type=IMAGE_MANIFEST) -> dict:
    return {"mediaType": media_type, "digest": digest(content), "size": len(content)}


class Image:
    """A synthetic registry image with an arm64 payload and optional extras."""

    def __init__(self, platform="linux/arm64", layer=b"layer-bytes"):
        system, architecture = platform.split("/")
        self.layer = layer
        self.config = dump({
            "os": system, "architecture": architecture,
            "config": {"Labels": {"org.opencontainers.image.revision": "a" * 40,
                                  "org.opencontainers.image.source": builds.SOURCE}},
            "rootfs": {"type": "layers", "diff_ids": [digest(layer)]},
        })
        self.manifest = dump({
            "schemaVersion": 2, "mediaType": IMAGE_MANIFEST,
            "config": descriptor(self.config, "application/vnd.oci.image.config.v1+json"),
            "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar")],
        })
        self.empty, self.statement = b"{}", b'{"_type":"https://in-toto.io/Statement/v0.1"}'
        self.attestation = self.attestation_for(digest(self.manifest))
        self.blobs = {digest(blob): blob for blob in (layer, self.config, self.manifest, self.attestation, self.empty, self.statement)}
        self.platform = {"os": system, "architecture": architecture}
        self.children = [
            # Never pulled, so absent from the archive.
            {**descriptor(b"amd64-manifest"), "platform": {"os": "linux", "architecture": "amd64"}},
            {**descriptor(b"amd64-attestation"), "annotations": {**ATTESTATION, "vnd.docker.reference.digest": digest(b"amd64-manifest")},
             "platform": {"os": "unknown", "architecture": "unknown"}},
            {**descriptor(self.manifest), "platform": self.platform},
            {**descriptor(self.attestation), "annotations": {**ATTESTATION, "vnd.docker.reference.digest": digest(self.manifest)},
             "platform": {"os": "unknown", "architecture": "unknown"}},
        ]

    def attestation_for(self, subject_digest: str) -> bytes:
        return dump({
            "schemaVersion": 2, "mediaType": IMAGE_MANIFEST, "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
            "config": descriptor(self.empty, "application/vnd.oci.empty.v1+json"),
            "layers": [descriptor(self.statement, "application/vnd.in-toto+json")],
            "subject": {"mediaType": IMAGE_MANIFEST, "digest": subject_digest, "size": 1},
        })

    def archive(self, *, children=None, extra_blobs=(), tamper=None, layers_in_manifest=None, tag=TAG):
        index = dump({"schemaVersion": 2, "mediaType": INDEX, "manifests": self.children if children is None else children})
        blobs = dict(self.blobs)
        blobs[digest(index)] = index
        for blob in extra_blobs:
            blobs[digest(blob)] = blob
        entries = {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": dump({"schemaVersion": 2, "mediaType": INDEX, "manifests": [{
                **descriptor(index, INDEX),
                "annotations": {"io.containerd.image.name": "docker.io/" + tag,
                                "org.opencontainers.image.ref.name": tag.split(":")[1]}}]}),
            "manifest.json": dump([{
                "Config": "blobs/sha256/" + digest(self.config).split(":")[1], "RepoTags": [tag],
                "Layers": layers_in_manifest or ["blobs/sha256/" + digest(self.layer).split(":")[1]]}]),
        }
        entries.update({"blobs/sha256/" + key.split(":")[1]: value for key, value in blobs.items()})
        for name, content in (tamper or {}).items():
            entries[name] = content
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
        return buffer, {"tag": tag, "reference": digest(index)}


class RegistryArchiveTests(unittest.TestCase):
    def identity(self, buffer, image, platform="linux/arm64"):
        return builds.archive_identity(buffer, image, platform, None)

    def assertRejected(self, error, buffer, image, platform="linux/arm64"):
        with self.assertRaisesRegex(builds.BuildError, error):
            self.identity(buffer, image, platform)

    def test_registry_index_with_absent_platforms_and_attestations_is_accepted(self):
        image = Image()
        buffer, recorded = image.archive()
        self.assertEqual(self.identity(buffer, recorded), digest(image.config))

    def test_other_platform_content_may_be_present_and_is_then_verified(self):
        image = Image()
        other = Image("linux/amd64", layer=b"amd64-layer")
        image.children[0] = {**descriptor(other.manifest), "platform": {"os": "linux", "architecture": "amd64"}}
        image.children[1]["annotations"]["vnd.docker.reference.digest"] = digest(other.manifest)
        buffer, recorded = image.archive(extra_blobs=[other.manifest, other.config, other.layer])
        self.assertEqual(self.identity(buffer, recorded), digest(image.config), "another platform's config is not compared")

    def test_the_requested_platform_selects_what_must_be_present(self):
        buffer, recorded = Image().archive()
        self.assertRejected("does not match the source/platform", buffer, recorded, "linux/amd64")

    def test_the_selected_image_must_be_present_and_exact(self):
        image = Image()
        # Only other platforms are listed: the manifest blob is in the archive but nothing selects it.
        others = image.children[:2]
        self.assertRejected("does not select exactly the recorded config", *image.archive(children=others))
        absent = Image()
        absent.blobs.pop(digest(absent.manifest))
        self.assertRejected("metadata is missing", *absent.archive())

    def test_a_second_image_for_the_selected_platform_must_have_the_same_config(self):
        image = Image()
        rival = Image(layer=b"other-layer")
        image.children.append({**descriptor(rival.manifest), "platform": image.platform})
        self.assertRejected("different image config", *image.archive(extra_blobs=[rival.manifest, rival.config, rival.layer]))

    def test_an_attestation_must_be_bound_to_an_image_of_the_same_index(self):
        for change, error in (
                (lambda child: child["annotations"].update({"vnd.docker.reference.digest": digest(b"unrelated")}), "not bound"),
                (lambda child: child["annotations"].update({"vnd.docker.reference.digest": child["digest"]}), "not bound"),
                (lambda child: child.update({"platform": {"os": "linux", "architecture": "arm64"}}), "not bound"),
                (lambda child: child.pop("platform"), "not bound"),
                (lambda child: child.update({"annotations": "text"}), "annotations are invalid")):
            image = Image()
            change(image.children[3])
            with self.subTest(error=error):
                self.assertRejected(error, *image.archive())

    def test_a_present_attestation_must_belong_to_its_image(self):
        image = Image()
        forged = image.attestation_for(digest(b"another-image"))
        image.children[3] = {**descriptor(forged), "annotations": {**ATTESTATION, "vnd.docker.reference.digest": digest(image.manifest)},
                             "platform": {"os": "unknown", "architecture": "unknown"}}
        self.assertRejected("does not belong to its indexed image", *image.archive(extra_blobs=[forged]))

    def test_children_without_a_usable_platform_are_not_trusted(self):
        for platform in ({"os": "unknown", "architecture": "unknown"}, {"os": "linux"}, {"os": 5, "architecture": "arm64"}):
            image = Image()
            image.children[0] = {**descriptor(b"mystery"), "platform": platform}
            with self.subTest(platform=platform):
                self.assertRejected("without a usable platform", *image.archive())

    def test_other_platform_content_that_is_present_must_match_its_descriptor(self):
        image = Image()
        image.children[0] = {**descriptor(b"claimed"), "platform": {"os": "linux", "architecture": "amd64"}}
        claimed = digest(b"claimed").split(":")[1]
        self.assertRejected("descriptor digest mismatch", *image.archive(tamper={"blobs/sha256/" + claimed: b"{}"}))

    def test_descriptors_need_valid_digests_and_a_nonempty_index(self):
        image = Image()
        image.children[0]["digest"] = "sha256:xyz"
        self.assertRejected("child image descriptor is invalid", *image.archive())
        self.assertRejected("empty or invalid", *Image().archive(children=[]))
        self.assertRejected("descriptor is invalid", *Image().archive(children=["text"]))

    def test_layers_and_blob_integrity_are_still_enforced(self):
        image = Image()
        self.assertRejected("descriptor and Docker-save layers differ", *image.archive(
            layers_in_manifest=["blobs/sha256/" + "e" * 64]))
        name = "blobs/sha256/" + digest(image.layer).split(":")[1]
        self.assertRejected("blob digest mismatch", *image.archive(tamper={name: b"tampered"}))

    def test_wrong_tag_or_reference_is_still_rejected(self):
        image = Image()
        buffer, recorded = image.archive()
        self.assertRejected("Archive root does not match", buffer, {**recorded, "reference": digest(b"other")})
        other, recorded = image.archive(tag="unrelated:overwrite")
        self.assertRejected("only the selected local image tag", other, {**recorded, "tag": TAG})


if __name__ == "__main__":
    unittest.main()
