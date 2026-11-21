"""Anonymous, read-only Docker Hub metadata for the project's own published images.

Shared by the controller's deployment tooling (``tool.deployment_images``) and the
web application's update check, so there is one registry client. Only the
project's repositories, its tag forms and digests are requested; responses are
size-limited and digest-checked, and blob redirects may go only to Docker's
download hosts without credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SOURCE = "https://github.com/itcorner/MPDRussoundPi"
REPOSITORIES = {name: f"itcornernet/mpdrussoundpi-{name}" for name in ("web", "ser2net")}
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
MAX_METADATA = 4 * 1024 * 1024
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
REVISION = re.compile(r"[a-f0-9]{40}")
# Tags the project publishes (dev, dev-<commit>, latest, X.Y, X.Y.Z, X.Y.Z-rc.N) and digests.
MANIFEST_RESOURCE = re.compile(
    r"manifests/(?:dev|dev-[a-f0-9]{40}|latest|\d{1,4}\.\d{1,4}(?:\.\d{1,4}(?:-rc\.\d{1,4})?)?|sha256:[a-f0-9]{64})"
    r"|blobs/sha256:[a-f0-9]{64}")


class RegistryError(ValueError):
    """Registry metadata that is unavailable, malformed or untrustworthy."""


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def json_object(content: bytes, context: str = "Registry metadata") -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise RegistryError(f"{context} must be valid JSON") from error
    if not isinstance(value, dict):
        raise RegistryError(f"{context} must be a JSON object")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Registry:
    """Anonymous, pull-only access to the two public Docker Hub repositories."""

    def __init__(self) -> None:
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.tokens: dict[str, str] = {}

    def _get(self, url: str, headers: dict[str, str], blob_redirect: bool = False) -> tuple[bytes, Any]:
        try:
            with self.opener.open(Request(url, headers=headers), timeout=30) as response:
                body = response.read(MAX_METADATA + 1)
                if len(body) > MAX_METADATA:
                    raise RegistryError("Registry metadata exceeds the size limit")
                return body, response.headers
        except HTTPError as error:
            if blob_redirect and error.code in (302, 307):
                location = error.headers.get("Location", "")
                try:
                    destination = urlsplit(location)
                    port = destination.port
                except ValueError:
                    raise RegistryError("Registry blob redirect is malformed") from None
                if (destination.scheme != "https"
                        or destination.hostname not in ("production.cloudfront.docker.com",
                                                        "production.cloudflare.docker.com")
                        or destination.username or destination.password
                        or port not in (None, 443) or destination.fragment):
                    raise RegistryError("Registry blob redirect is not an approved Docker download host") from None
                # Only config blobs may redirect; registry credentials must not follow them.
                return self._get(location, {}, blob_redirect=False)
            raise RegistryError(f"Registry metadata request failed: HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError) as error:
            raise RegistryError("Registry metadata request failed; check connectivity and retry explicitly") from error

    def get(self, repository: str, resource: str) -> tuple[bytes, Any]:
        if repository not in REPOSITORIES.values():
            raise RegistryError("Unsupported deployment image repository")
        if not MANIFEST_RESOURCE.fullmatch(resource):
            raise RegistryError("Unsupported registry resource")
        if repository not in self.tokens:
            query = urlencode({"service": "registry.docker.io", "scope": f"repository:{repository}:pull"})
            raw, _ = self._get("https://auth.docker.io/token?" + query, {})
            token = json_object(raw).get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
                raise RegistryError("Registry returned an invalid pull token")
            self.tokens[repository] = token
        url = f"https://registry-1.docker.io/v2/{repository}/{resource}"
        headers = {"Authorization": "Bearer " + self.tokens[repository],
                   "Accept": ", ".join(sorted(INDEX_TYPES | MANIFEST_TYPES))}
        raw, response_headers = self._get(url, headers, blob_redirect=resource.startswith("blobs/"))
        return raw, response_headers


def descriptor(value: Any, types: set[str]) -> dict[str, Any]:
    if (not isinstance(value, dict) or not isinstance(value.get("mediaType"), str)
            or value["mediaType"] not in types
            or not isinstance(value.get("digest"), str) or not DIGEST.fullmatch(value["digest"])
            or type(value.get("size")) is not int or not 0 < value["size"] <= MAX_METADATA):
        raise RegistryError("Invalid registry metadata descriptor")
    return value


def metadata(registry: Registry, repository: str, reference: str,
             types: set[str], expected: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    raw, headers = registry.get(repository, "manifests/" + reference)
    digest = digest_of(raw)
    if headers.get("Docker-Content-Digest") != digest:
        raise RegistryError("Registry manifest digest mismatch")
    if DIGEST.fullmatch(reference) and reference != digest:
        raise RegistryError("Requested manifest digest mismatch")
    if expected and (expected["digest"] != digest or expected["size"] != len(raw)):
        raise RegistryError("Manifest descriptor checksum or size mismatch")
    value = json_object(raw)
    if (value.get("schemaVersion") != 2 or not isinstance(value.get("mediaType"), str)
            or value["mediaType"] not in types):
        raise RegistryError("Unsupported registry manifest format")
    return value, digest


def platform_config(registry: Registry, repository: str, reference: str, platform: str) -> dict[str, Any]:
    """Index, platform manifest and image configuration of one tag or digest for one platform."""
    index, index_digest = metadata(registry, repository, reference, INDEX_TYPES)
    entries = index.get("manifests")
    if not isinstance(entries, list):
        raise RegistryError("Image index has no platform manifests")
    os_name, architecture = platform.split("/")
    matches = [
        entry for entry in entries if isinstance(entry, dict)
        and isinstance(entry.get("platform"), dict)
        and entry["platform"].get("os") == os_name
        and entry["platform"].get("architecture") == architecture
        and entry["platform"].get("variant", "") in ("", "v8" if architecture == "arm64" else "")
    ]
    if len(matches) != 1:
        raise RegistryError("Requested platform is missing or ambiguous in image index")
    selected = descriptor(matches[0], MANIFEST_TYPES)
    manifest, platform_digest = metadata(registry, repository, selected["digest"], MANIFEST_TYPES, selected)
    config = descriptor(manifest.get("config"), CONFIG_TYPES)
    raw, _ = registry.get(repository, "blobs/" + config["digest"])
    if digest_of(raw) != config["digest"] or len(raw) != config["size"]:
        raise RegistryError("Image configuration checksum or size mismatch")
    image = json_object(raw)
    if image.get("os") != os_name or image.get("architecture") != architecture:
        raise RegistryError("Image configuration platform mismatch")
    labels = image.get("config", {}).get("Labels") if isinstance(image.get("config"), dict) else None
    return {
        "index_digest": index_digest, "platform_digest": platform_digest, "config_digest": config["digest"],
        "labels": labels if isinstance(labels, dict) else {},
        "created": image.get("created") if isinstance(image.get("created"), str) else None,
    }
