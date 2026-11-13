import copy
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from email.message import Message

from tool import deployment_images as images


REVISION = "a" * 40
OTHER = "b" * 40


class FixtureRegistry:
    def __init__(self):
        self.content = {}
        self.requests = []
        self.initial = {}
        self.alias_reads = {}
        self.change_alias = False
        for name in ("web", "ser2net"):
            self.add_image(name)

    def add_image(self, name, revision=REVISION, architecture="arm64", source=images.SOURCE):
        repository = images.REPOSITORIES[name]

        def add(resource, value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            digest = images.digest_of(raw)
            self.content[(repository, resource + digest)] = (raw, {"Docker-Content-Digest": digest})
            return {"digest": digest, "size": len(raw), "mediaType": value.get("mediaType")}

        config = add("blobs/", {
            "os": "linux", "architecture": architecture,
            "config": {"Labels": {"org.opencontainers.image.revision": revision,
                                   "org.opencontainers.image.version": "dev-" + revision,
                                   "org.opencontainers.image.source": source}},
        })
        config["mediaType"] = next(iter(images.CONFIG_TYPES))
        manifest = add("manifests/", {
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config, "layers": [],
        })
        manifest["platform"] = {"os": "linux", "architecture": architecture}
        index = add("manifests/", {
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [manifest],
        })
        payload = self.content[(repository, "manifests/" + index["digest"])]
        self.content[(repository, "manifests/dev")] = payload
        self.content[(repository, "manifests/dev-" + revision)] = payload
        self.initial[name] = index["digest"]

    def get(self, repository, resource):
        self.requests.append((repository, resource))
        if resource == "manifests/dev":
            count = self.alias_reads.get(repository, 0) + 1
            self.alias_reads[repository] = count
            if self.change_alias and count > 1:
                raw, _ = self.content[(repository, resource)]
                raw = raw + b" "
                return raw, {"Docker-Content-Digest": images.digest_of(raw)}
        try:
            return self.content[(repository, resource)]
        except KeyError:
            raise images.SelectionError("Registry metadata request failed: HTTP 404") from None


class DeploymentImageTests(unittest.TestCase):
    def test_selects_single_revision_and_records_both_manifest_levels(self):
        registry = FixtureRegistry()
        selected = images.resolve("managed", "linux/arm64", registry)
        self.assertEqual(selected["revision"], REVISION)
        self.assertEqual(set(selected["images"]), {"web", "ser2net"})
        self.assertEqual(images.validate_selection(selected, "managed", "linux/arm64"), selected)
        for name, image in selected["images"].items():
            self.assertTrue(image["reference"].endswith(registry.initial[name]))
            self.assertNotEqual(image["index_digest"], image["platform_digest"])
            self.assertIsNone(image["fetched_at"])
        self.assertTrue(all(resource.startswith(("manifests/", "blobs/")) for _, resource in registry.requests))
        self.assertEqual(set(registry.alias_reads.values()), {2})

    def test_external_and_dummy_do_not_resolve_gateway(self):
        for gateway in ("external", "dummy"):
            registry = FixtureRegistry()
            selection = images.resolve(gateway, "linux/arm64", registry)
            self.assertEqual(set(selection["images"]), {"web"})
            self.assertTrue(all(repository == images.REPOSITORIES["web"] for repository, _ in registry.requests))

    def test_selects_amd64_without_using_arm64_metadata(self):
        registry = FixtureRegistry()
        for name in ("web", "ser2net"):
            registry.add_image(name, architecture="amd64")
        selected = images.resolve("managed", "linux/amd64", registry)
        self.assertEqual(selected["platform"], "linux/amd64")
        images.verify(selected, registry)

    def test_moving_alias_incomplete_pair_and_mismatched_labels_fail(self):
        for fault in ("race", "missing", "revision", "source", "platform", "exact"):
            with self.subTest(fault=fault):
                registry = FixtureRegistry()
                if fault == "race":
                    registry.change_alias = True
                elif fault == "missing":
                    del registry.content[(images.REPOSITORIES["ser2net"], "manifests/dev")]
                elif fault == "revision":
                    registry.add_image("ser2net", OTHER)
                elif fault == "source":
                    registry.add_image("ser2net", source="https://invalid.example")
                elif fault == "platform":
                    registry.add_image("ser2net", architecture="amd64")
                else:
                    del registry.content[(images.REPOSITORIES["web"], "manifests/dev-" + REVISION)]
                with self.assertRaises(images.SelectionError):
                    images.resolve("managed", "linux/arm64", registry)

    def test_corrupt_manifest_header_and_config_digest_fail(self):
        for fault in ("header", "config", "manifest"):
            with self.subTest(fault=fault):
                registry = FixtureRegistry()
                if fault == "header":
                    key = (images.REPOSITORIES["web"], "manifests/dev")
                    raw, _ = registry.content[key]
                    registry.content[key] = raw, {"Docker-Content-Digest": "sha256:" + "0" * 64}
                else:
                    key = next(key for key in registry.content if key[0] == images.REPOSITORIES["web"]
                               and key[1].startswith("blobs/" if fault == "config" else "manifests/sha256:"))
                    raw, headers = registry.content[key]
                    registry.content[key] = raw + b" ", headers
                with self.assertRaises(images.SelectionError):
                    images.resolve("external", "linux/arm64", registry)

    def test_reuse_never_contacts_registry_and_cli_does_not_change_file(self):
        selection = images.resolve("external", "linux/arm64", FixtureRegistry())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            images.save_selection(path, selection)
            before = path.read_bytes()
            with patch.object(images, "Registry", side_effect=AssertionError("Unexpected network access")):
                self.assertEqual(images.load_selection(path, "external", "linux/arm64"), selection)
            result = subprocess.run(
                [sys.executable, "-m", "tool.deployment_images", "reuse",
                 "--selection", str(path), "--gateway", "external"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), selection)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(images.SelectionError):
                images.save_selection(path, selection)
            self.assertEqual(path.read_bytes(), before)
            symlink = Path(directory) / "link"
            symlink.symlink_to(path)
            with self.assertRaises(images.SelectionError):
                images.load_selection(symlink, "external", "linux/arm64")

    def test_selection_rejects_malformed_or_wrong_scope_without_fallback(self):
        selected = images.resolve("external", "linux/arm64", FixtureRegistry())
        for field, value in (("schema_version", 2), ("schema_version", True),
                             ("kind", "committed"), ("channel", "stable"),
                             ("platform", "linux/amd64"), ("gateway", "managed"),
                             ("revision", "bad"), ("resolved_at", "2026-01-01")):
            candidate = {**selected, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(images.SelectionError):
                images.validate_selection(candidate, "external", "linux/arm64")
        for field, value in (("repository", "other/web"), ("reference", "web:dev"),
                             ("fetched_at", "invented"), ("config_digest", "invalid")):
            candidate = copy.deepcopy(selected)
            candidate["images"]["web"][field] = value
            with self.subTest(field=field), self.assertRaises(images.SelectionError):
                images.validate_selection(candidate, "external", "linux/arm64")

    def test_verify_uses_only_exact_digests_even_if_aliases_change(self):
        registry = FixtureRegistry()
        selection = images.resolve("managed", "linux/arm64", registry)
        registry.requests.clear()
        registry.change_alias = True
        images.verify(selection, registry)
        self.assertTrue(registry.requests)
        self.assertFalse(any(resource.startswith("manifests/dev") for _, resource in registry.requests))

    def test_check_compares_index_digests_without_mutating_previous_selection(self):
        registry = FixtureRegistry()
        selection = images.resolve("managed", "linux/arm64", registry)
        before = copy.deepcopy(selection)
        unchanged = images.check_selection(selection, registry)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["comparison_level"], "index_digest")
        for name in ("web", "ser2net"):
            registry.add_image(name, OTHER)
        changed = images.check_selection(selection, registry)
        self.assertTrue(changed["changed"])
        self.assertFalse(changed["selection_modified"])
        self.assertEqual(selection, before)
        del registry.content[(images.REPOSITORIES["ser2net"], "manifests/dev")]
        with self.assertRaises(images.SelectionError):
            images.check_selection(selection, registry)
        self.assertEqual(selection, before)

    def test_ambiguous_platform_or_incorrect_descriptor_size_fail(self):
        for fault in ("duplicate", "size"):
            registry = FixtureRegistry()
            key = (images.REPOSITORIES["web"], "manifests/dev")
            raw, _ = registry.content[key]
            index = json.loads(raw)
            if fault == "duplicate":
                index["manifests"].append(index["manifests"][0])
            else:
                index["manifests"][0]["size"] += 1
            raw = json.dumps(index).encode()
            registry.content[key] = raw, {"Docker-Content-Digest": images.digest_of(raw)}
            with self.subTest(fault=fault), self.assertRaises(images.SelectionError):
                images.resolve("external", "linux/arm64", registry)

    def test_malformed_metadata_and_selection_report_explicit_errors(self):
        for value in (b"[]", b"invalid", b'{"a":'):
            with self.subTest(value=value), self.assertRaises(images.SelectionError):
                images.json_object(value)
        for media_type in ([], {}, 2, None):
            with self.subTest(media_type=media_type):
                with self.assertRaises(images.SelectionError):
                    images.descriptor({"mediaType": media_type}, images.MANIFEST_TYPES)
                registry = FixtureRegistry()
                raw = json.dumps({"mediaType": media_type, "schemaVersion": 2}).encode()
                registry.content[(images.REPOSITORIES["web"], "manifests/dev")] = (
                    raw, {"Docker-Content-Digest": images.digest_of(raw)},
                )
                with self.assertRaises(images.SelectionError):
                    images.resolve("external", "linux/arm64", registry)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            path.write_text("{invalid")
            with self.assertRaisesRegex(images.SelectionError, "Image selection file must be valid JSON"):
                images.load_selection(path, "external", "linux/arm64")

    def test_blob_redirect_drops_credentials_and_other_redirects_fail(self):
        class Response(BytesIO):
            headers = {}

        for destination, allowed in (
            ("https://production.cloudfront.docker.com/config?test=1", True),
            ("https://production.cloudflare.docker.com/config?test=1", True),
            ("http://production.cloudfront.docker.com/config", False),
            ("https://unrelated.example/config", False),
            ("https://production.cloudfront.docker.com:invalid/config", False),
            ("https://user@production.cloudfront.docker.com/config", False),
        ):
            with self.subTest(destination=destination):
                registry = images.Registry()
                headers = Message()
                headers["Location"] = destination
                redirect = HTTPError("https://registry-1.docker.io/test", 307, "redirect", headers, None)
                with patch.object(registry.opener, "open", side_effect=[redirect, Response(b"{}")]) as opened:
                    if allowed:
                        raw, _ = registry._get("https://registry-1.docker.io/test",
                                               {"Authorization": "Bearer test-placeholder"}, blob_redirect=True)
                        self.assertEqual(raw, b"{}")
                        self.assertNotIn("Authorization", opened.call_args.args[0].headers)
                    else:
                        with self.assertRaises(images.SelectionError):
                            registry._get("https://registry-1.docker.io/test", {}, blob_redirect=True)
                        self.assertEqual(opened.call_count, 1)
                with patch.object(registry.opener, "open", side_effect=redirect):
                    with self.assertRaisesRegex(images.SelectionError, "HTTP 307"):
                        registry._get("https://registry-1.docker.io/test", {}, blob_redirect=False)

    def test_reuse_cli_in_process_cannot_construct_registry(self):
        selection = images.resolve("external", "linux/arm64", FixtureRegistry())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            images.save_selection(path, selection)
            argv = ["deployment_images", "reuse", "--gateway", "external", "--selection", str(path)]
            with patch.object(sys, "argv", argv), patch("sys.stdout", new_callable=StringIO) as output:
                with patch.object(images, "Registry", side_effect=AssertionError("Must remain offline")):
                    self.assertEqual(images.main(), 0)
                self.assertEqual(json.loads(output.getvalue()), selection)

    def test_registry_requests_anonymous_pull_only_and_sanitizes_errors(self):
        registry = images.Registry()
        calls = []

        def get(url, headers, blob_redirect=False):
            calls.append((url, headers))
            if url.startswith("https://auth.docker.io/token?"):
                return b'{"token":"test-placeholder"}', {}
            return b"{}", {}

        registry._get = get
        registry.get(images.REPOSITORIES["web"], "manifests/dev")
        self.assertIn("scope=repository%3Aitcornernet%2Fmpdrussoundpi-web%3Apull", calls[0][0])
        self.assertNotIn("push", calls[0][0])
        self.assertNotIn("Authorization", calls[0][1])
        self.assertEqual(calls[1][1]["Authorization"], "Bearer test-placeholder")
        for error in (HTTPError("private-url", 429, "private-response", {}, None),
                      URLError("private-response")):
            registry = images.Registry()
            with patch.object(registry.opener, "open", side_effect=error):
                with self.assertRaises(images.SelectionError) as caught:
                    registry.get(images.REPOSITORIES["web"], "manifests/dev")
                self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
