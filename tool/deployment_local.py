"""Portable local image selection metadata; no filesystem or daemon mutation."""

from datetime import datetime
from typing import Any
from uuid import UUID

from tool.deployment_files import is_mapping
from tool.deployment_images import DIGEST, REVISION, SelectionError, validate_request


MAX_ARCHIVE = 8 * 1024**3
LOCAL_CANDIDATE = "local-development-image-candidate"


def validate_manifest(value: Any, gateway: str, platform: str) -> dict[str, Any]:
    validate_request(gateway, platform)
    fields = {"schema_version", "kind", "operation", "revision", "tree", "gateway",
              "platform", "built_at", "build_network", "images"}
    if (not is_mapping(value) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "local-development-image-bundle" or value["gateway"] != gateway
            or value["platform"] != platform or type(value["build_network"]) is not bool
            or any(not isinstance(value[key], str) or not REVISION.fullmatch(value[key])
                   for key in ("revision", "tree"))):
        raise SelectionError("Invalid local build manifest identity")
    try:
        if not isinstance(value["operation"], str) or str(UUID(value["operation"])) != value["operation"]:
            raise ValueError
        if (not isinstance(value["built_at"], str)
                or datetime.fromisoformat(value["built_at"]).utcoffset() is None):
            raise ValueError
    except ValueError:
        raise SelectionError("Invalid local build operation or timestamp") from None
    names = {"web", "ser2net"} if gateway == "managed" else {"web"}
    if not is_mapping(value["images"]) or set(value["images"]) != names:
        raise SelectionError("Local image component set mismatch")
    for name, image in value["images"].items():
        if (not is_mapping(image) or set(image) != {"reference", "tag", "archive", "bytes", "sha256"}
                or not isinstance(image["reference"], str) or not DIGEST.fullmatch(image["reference"])
                or image["tag"] != f'mpdrussoundpi-local/{name}:{value["operation"]}'
                or image["archive"] != name + ".tar" or type(image["bytes"]) is not int
                or not 0 < image["bytes"] <= MAX_ARCHIVE
                or not isinstance(image["sha256"], str) or not DIGEST.fullmatch(image["sha256"])):
            raise SelectionError("Invalid local image artifact descriptor")
    return value


def validate_candidate(value: Any) -> dict[str, Any]:
    if (not is_mapping(value)
            or set(value) != {"schema_version", "kind", "manifest_sha256", "manifest", "configs"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != LOCAL_CANDIDATE
            or not isinstance(value["manifest_sha256"], str) or not DIGEST.fullmatch(value["manifest_sha256"])
            or not is_mapping(value["manifest"])):
        raise SelectionError("Invalid local image candidate")
    manifest = value["manifest"]
    gateway, platform = manifest.get("gateway"), manifest.get("platform")
    if not isinstance(gateway, str) or not isinstance(platform, str):
        raise SelectionError("Local image gateway and platform must be strings")
    validate_manifest(manifest, gateway, platform)
    if (not is_mapping(value["configs"]) or set(value["configs"]) != set(manifest["images"])
            or any(not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                   for digest in value["configs"].values())):
        raise SelectionError("Invalid portable local image config identities")
    return value


def validate_loaded(value: Any, selection: dict[str, Any]) -> None:
    validate_candidate(selection)
    images = selection["manifest"]["images"]
    if (not is_mapping(value) or set(value) != set(images)
            or any(not isinstance(value[name], str)
                   or value[name] not in (image["reference"], selection["configs"][name])
                   for name, image in images.items())):
        raise SelectionError("Loaded image IDs differ from the approved root/config identities")
