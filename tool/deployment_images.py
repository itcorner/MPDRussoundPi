"""Read-only development image resolution; candidate files are not deployment records."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from tool.deployment_files import FileSafetyError, checked_directory
from mpdrussoundpi.common.imageregistry import (  # noqa: F401  (re-exported for the deployment tooling)
    CONFIG_TYPES, DIGEST, INDEX_TYPES, MANIFEST_TYPES, MAX_METADATA, REPOSITORIES, REVISION, SOURCE,
    NoRedirect, Registry, RegistryError as SelectionError, descriptor, digest_of, json_object, metadata,
    platform_config,
)


def inspect_image(registry: Registry, name: str, reference: str, platform: str,
                  expected_revision: str | None = None) -> dict[str, Any]:
    repository = REPOSITORIES[name]
    found = platform_config(registry, repository, reference, platform)
    labels = found["labels"]
    revision = labels.get("org.opencontainers.image.revision")
    if (not isinstance(revision, str) or not REVISION.fullmatch(revision)
            or labels.get("org.opencontainers.image.source") != SOURCE
            or labels.get("org.opencontainers.image.version") != "dev-" + revision
            or (expected_revision is not None and revision != expected_revision)):
        raise SelectionError("Development source, version or revision labels do not match")
    return {
        "repository": repository, "reference": repository + "@" + found["index_digest"],
        "index_digest": found["index_digest"], "platform_digest": found["platform_digest"],
        "config_digest": found["config_digest"], "revision": revision, "fetched_at": None,
    }


def validate_request(gateway: str, platform: str) -> None:
    if gateway not in ("managed", "external", "dummy", "standalone"):
        raise SelectionError("Gateway must be managed, external, dummy or standalone")
    if platform not in ("linux/arm64", "linux/amd64"):
        raise SelectionError("Platform must be linux/arm64 or linux/amd64")


def components(gateway: str) -> tuple[str, ...]:
    if gateway == "managed":
        return ("web", "ser2net")
    if gateway == "external":
        return ("web",)
    if gateway == "dummy":
        return ("web", "dummy")
    if gateway == "standalone":
        return ("ser2net",)
    raise SelectionError("Unsupported gateway layout")


def resolve(gateway: str, platform: str, registry: Registry | None = None) -> dict[str, Any]:
    validate_request(gateway, platform)
    if gateway == "dummy":
        raise SelectionError("Dummy deployments require a verified local web/dummy build bundle; no approved registry artifact")
    registry = registry or Registry()
    names = components(gateway)
    first = names[0]
    first_alias = inspect_image(registry, first, "dev", platform)
    revision = first_alias["revision"]
    images = {}
    aliases = {first: first_alias}
    for name in names:
        if name != first:
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
    if gateway == "dummy":
        raise SelectionError("Registry dummy candidates are unavailable; select a verified local web/dummy bundle")
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
    names = set(components(gateway))
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


def selection_directory(path: Path) -> Path:
    if not path.name or path.name in (".", "..") or path.parent.is_symlink():
        raise SelectionError("Selection path must name a file through a real parent directory")
    try:
        return path.parent.resolve(strict=True)
    except OSError as error:
        raise SelectionError("Selection parent directory is unavailable") from error


def load_selection(path: Path, gateway: str, platform: str) -> dict[str, Any]:
    from tool.deployment_inputs import InputError, read_file

    try:
        parent = selection_directory(path)
        with checked_directory(parent, os.getuid(), private=False) as directory:
            content, _ = read_file(directory, path.name, os.getuid(), (0o600,))
        if len(content) > MAX_METADATA:
            raise SelectionError("Image selection exceeds the size limit")
        return validate_selection(json_object(content, "Image selection file"), gateway, platform)
    except (OSError, FileSafetyError, InputError) as error:
        raise SelectionError("Cannot read a safe private image selection file") from error


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
    from tool.deployment_inputs import InputError, write_file

    try:
        parent = selection_directory(path)
        if not isinstance(value, dict):
            raise SelectionError("Cannot save an invalid image selection")
        validate_selection(value, value.get("gateway"), value.get("platform"))
        raw = (json.dumps(value, indent=2) + "\n").encode()
        if len(raw) > MAX_METADATA:
            raise SelectionError("Image selection exceeds the size limit")
        with checked_directory(parent, os.getuid(), private=False) as directory:
            write_file(directory, path.name, raw, owner=os.getuid(), group=os.getgid(), mode=0o600)
    except (OSError, FileSafetyError, InputError) as error:
        raise SelectionError("Cannot create a private candidate file; choose a new path and check permissions") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("resolve", "reuse", "verify", "check"))
    parser.add_argument("--gateway", choices=("managed", "external", "dummy", "standalone"), default="managed")
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
