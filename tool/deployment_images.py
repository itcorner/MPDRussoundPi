"""Read-only development image resolution; candidate files are not deployment records."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
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


class SelectionError(ValueError):
    """An incomplete or untrustworthy image selection."""


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def json_object(content: bytes, context: str = "Registry metadata") -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise SelectionError(f"{context} must be valid JSON") from error
    if not isinstance(value, dict):
        raise SelectionError(f"{context} must be a JSON object")
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
                    raise SelectionError("Registry metadata exceeds the size limit")
                return body, response.headers
        except HTTPError as error:
            if blob_redirect and error.code in (302, 307):
                location = error.headers.get("Location", "")
                try:
                    destination = urlsplit(location)
                    port = destination.port
                except ValueError:
                    raise SelectionError("Registry blob redirect is malformed") from None
                if (destination.scheme != "https"
                        or destination.hostname not in ("production.cloudfront.docker.com",
                                                        "production.cloudflare.docker.com")
                        or destination.username or destination.password
                        or port not in (None, 443) or destination.fragment):
                    raise SelectionError("Registry blob redirect is not an approved Docker download host") from None
                # Only config blobs may redirect; registry credentials must not follow them.
                return self._get(location, {}, blob_redirect=False)
            raise SelectionError(f"Registry metadata request failed: HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError) as error:
            raise SelectionError("Registry metadata request failed; check connectivity and retry explicitly") from error

    def get(self, repository: str, resource: str) -> tuple[bytes, Any]:
        if repository not in REPOSITORIES.values():
            raise SelectionError("Unsupported deployment image repository")
        if not re.fullmatch(r"(?:manifests/(?:dev|dev-[a-f0-9]{40}|sha256:[a-f0-9]{64})|blobs/sha256:[a-f0-9]{64})",
                            resource):
            raise SelectionError("Unsupported registry resource")
        if repository not in self.tokens:
            query = urlencode({"service": "registry.docker.io", "scope": f"repository:{repository}:pull"})
            raw, _ = self._get("https://auth.docker.io/token?" + query, {})
            token = json_object(raw).get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
                raise SelectionError("Registry returned an invalid pull token")
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
        raise SelectionError("Invalid registry metadata descriptor")
    return value


def metadata(registry: Registry, repository: str, reference: str,
             types: set[str], expected: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    raw, headers = registry.get(repository, "manifests/" + reference)
    digest = digest_of(raw)
    if headers.get("Docker-Content-Digest") != digest:
        raise SelectionError("Registry manifest digest mismatch")
    if DIGEST.fullmatch(reference) and reference != digest:
        raise SelectionError("Requested manifest digest mismatch")
    if expected and (expected["digest"] != digest or expected["size"] != len(raw)):
        raise SelectionError("Manifest descriptor checksum or size mismatch")
    value = json_object(raw)
    if (value.get("schemaVersion") != 2 or not isinstance(value.get("mediaType"), str)
            or value["mediaType"] not in types):
        raise SelectionError("Unsupported registry manifest format")
    return value, digest


def inspect_image(registry: Registry, name: str, reference: str, platform: str,
                  expected_revision: str | None = None) -> dict[str, Any]:
    repository = REPOSITORIES[name]
    index, index_digest = metadata(registry, repository, reference, INDEX_TYPES)
    entries = index.get("manifests")
    if not isinstance(entries, list):
        raise SelectionError("Image index has no platform manifests")
    os_name, architecture = platform.split("/")
    matches = [
        entry for entry in entries if isinstance(entry, dict)
        and isinstance(entry.get("platform"), dict)
        and entry["platform"].get("os") == os_name
        and entry["platform"].get("architecture") == architecture
        and entry["platform"].get("variant", "") in ("", "v8" if architecture == "arm64" else "")
    ]
    if len(matches) != 1:
        raise SelectionError("Requested platform is missing or ambiguous in image index")
    selected = descriptor(matches[0], MANIFEST_TYPES)
    manifest, platform_digest = metadata(registry, repository, selected["digest"], MANIFEST_TYPES, selected)
    config = descriptor(manifest.get("config"), CONFIG_TYPES)
    raw, _ = registry.get(repository, "blobs/" + config["digest"])
    if digest_of(raw) != config["digest"] or len(raw) != config["size"]:
        raise SelectionError("Image configuration checksum or size mismatch")
    image = json_object(raw)
    if image.get("os") != os_name or image.get("architecture") != architecture:
        raise SelectionError("Image configuration platform mismatch")
    labels = image.get("config", {}).get("Labels") if isinstance(image.get("config"), dict) else None
    revision = labels.get("org.opencontainers.image.revision") if isinstance(labels, dict) else None
    if (not isinstance(revision, str) or not REVISION.fullmatch(revision)
            or labels.get("org.opencontainers.image.source") != SOURCE
            or labels.get("org.opencontainers.image.version") != "dev-" + revision
            or (expected_revision is not None and revision != expected_revision)):
        raise SelectionError("Development source, version or revision labels do not match")
    return {
        "repository": repository, "reference": repository + "@" + index_digest,
        "index_digest": index_digest, "platform_digest": platform_digest,
        "config_digest": config["digest"], "revision": revision, "fetched_at": None,
    }


def validate_request(gateway: str, platform: str) -> None:
    if gateway not in ("managed", "external", "dummy"):
        raise SelectionError("Gateway must be managed, external or dummy")
    if platform not in ("linux/arm64", "linux/amd64"):
        raise SelectionError("Platform must be linux/arm64 or linux/amd64")


def resolve(gateway: str, platform: str, registry: Registry | None = None) -> dict[str, Any]:
    validate_request(gateway, platform)
    registry = registry or Registry()
    # Select one revision from web, then require both moving aliases and exact tags to agree.
    web_alias = inspect_image(registry, "web", "dev", platform)
    revision = web_alias["revision"]
    names = ("web", "ser2net") if gateway == "managed" else ("web",)
    images = {}
    aliases = {"web": web_alias}
    for name in names:
        if name != "web":
            aliases[name] = inspect_image(registry, name, "dev", platform, revision)
        images[name] = inspect_image(registry, name, "dev-" + revision, platform, revision)
        if images[name] != aliases[name]:
            raise SelectionError("Development alias and exact revision tag disagree; retry explicitly")
    for name in names:
        _, current = metadata(registry, REPOSITORIES[name], "dev", INDEX_TYPES)
        if current != images[name]["index_digest"]:
            raise SelectionError("Development alias changed during resolution; retry explicitly")
    return {
        "schema_version": 1, "kind": "development-image-candidate", "channel": "dev",
        "gateway": gateway, "platform": platform, "revision": revision,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "images": images,
    }


def validate_selection(value: Any, gateway: str, platform: str) -> dict[str, Any]:
    validate_request(gateway, platform)
    fields = {"schema_version", "kind", "channel", "gateway", "platform", "revision", "resolved_at", "images"}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "development-image-candidate" or value["channel"] != "dev"
            or value["gateway"] != gateway or value["platform"] != platform
            or not isinstance(value["revision"], str) or not REVISION.fullmatch(value["revision"])
            or not isinstance(value["resolved_at"], str)):
        raise SelectionError("Image selection schema, gateway or platform mismatch")
    try:
        timestamp = datetime.fromisoformat(value["resolved_at"])
        if timestamp.utcoffset() is None:
            raise ValueError("Timezone required")
    except ValueError as error:
        raise SelectionError("Invalid image resolution timestamp") from error
    names = {"web", "ser2net"} if gateway == "managed" else {"web"}
    if not isinstance(value["images"], dict) or set(value["images"]) != names:
        raise SelectionError("Image selection component set mismatch")
    image_fields = {"repository", "reference", "index_digest", "platform_digest", "config_digest",
                    "revision", "fetched_at"}
    for name in names:
        image = value["images"][name]
        if (not isinstance(image, dict) or set(image) != image_fields
                or image["repository"] != REPOSITORIES[name] or image["revision"] != value["revision"]
                or image["fetched_at"] is not None
                or any(not isinstance(image[key], str) or not DIGEST.fullmatch(image[key])
                       for key in ("index_digest", "platform_digest", "config_digest"))
                or image["reference"] != image["repository"] + "@" + image["index_digest"]):
            raise SelectionError("Image selection identity is invalid")
    return value


def load_selection(path: Path, gateway: str, platform: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise SelectionError("Selection must be an existing regular file, not a symlink")
        with path.open("rb") as source:
            content = source.read(MAX_METADATA + 1)
        if len(content) > MAX_METADATA:
            raise SelectionError("Image selection exceeds the size limit")
        return validate_selection(json_object(content, "Image selection file"), gateway, platform)
    except OSError as error:
        raise SelectionError("Cannot read the image selection file") from error


def verify(selection: dict[str, Any], registry: Registry | None = None) -> None:
    validate_selection(selection, selection["gateway"], selection["platform"])
    registry = registry or Registry()
    for name, image in selection["images"].items():
        actual = inspect_image(registry, name, image["index_digest"], selection["platform"],
                               selection["revision"])
        if actual != image:
            raise SelectionError("Pinned registry identity no longer matches the selected metadata")


def check_selection(selection: dict[str, Any], registry: Registry | None = None) -> dict[str, Any]:
    validate_selection(selection, selection["gateway"], selection["platform"])
    candidate = resolve(selection["gateway"], selection["platform"], registry)
    return {
        "kind": "development-image-check",
        "changed": any(candidate["images"][name]["index_digest"] != image["index_digest"]
                       for name, image in selection["images"].items()),
        "comparison_level": "index_digest", "candidate": candidate,
        "selection_modified": False,
    }


def save_selection(path: Path, value: dict[str, Any]) -> None:
    # Exclusive creation: a candidate never overwrites a previous selection or deployment pin.
    try:
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as target:
            target.write((json.dumps(value, indent=2) + "\n").encode())
            target.flush()
            os.fsync(target.fileno())
    except OSError as error:
        raise SelectionError("Cannot create candidate file; choose a new path and check permissions") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("resolve", "reuse", "verify", "check"))
    parser.add_argument("--gateway", choices=("managed", "external", "dummy"), default="managed")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"), default="linux/arm64")
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.action == "resolve") == (args.selection is not None):
        parser.error("resolve does not take --selection; reuse, verify and check require it")
    if args.output is not None and args.action != "resolve":
        parser.error("--output is supported only by resolve and never overwrites a file")
    try:
        if args.action == "resolve":
            value = resolve(args.gateway, args.platform)
            if args.output:
                save_selection(args.output, value)
        else:
            value = load_selection(args.selection, args.gateway, args.platform)
            if args.action == "verify":
                verify(value)
            elif args.action == "check":
                value = check_selection(value)
        print(json.dumps(value))
        return 0
    except SelectionError as error:
        print(f"Image selection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
