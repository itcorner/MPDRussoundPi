"""Explicit controller builds and portable verified development image bundles.

No target image load or service mutation is exposed. Bundle identity must be
retained separately before copying; a manifest is not a deployment record.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, BinaryIO, IO
from uuid import uuid4

from tool.deployment_files import FileSafetyError, checked_directory
from tool.deployment_images import DIGEST, REVISION, SOURCE, SelectionError, validate_request
from tool.deployment_inputs import attributes, read_file, write_file
from tool.deployment_local import MAX_ARCHIVE, validate_manifest
from tool.deployment_state import StateError, is_list, is_mapping


MAX_SOURCE = 512 * 1024**2
MAX_METADATA = 4 * 1024**2
DOCKERFILES = {"web": "Dockerfile", "ser2net": "Dockerfile.ser2net"}


class BuildError(ValueError):
    """Preparation failed or is uncertain; preserve artifacts and do not apply."""


def command(argv: list[str], *, cwd: Path | None = None, log: BinaryIO | None = None,
            timeout: int = 60) -> bytes:
    try:
        result = subprocess.run(argv, cwd=cwd, stdout=log or subprocess.PIPE,
                                stderr=log or subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError("Local build command unavailable or timed out; inspect private evidence") from None
    if result.returncode:
        raise BuildError("Local build command failed; inspect private evidence before retrying")
    return result.stdout or b""


def clean_source(source: Path, revision: str) -> str:
    if not REVISION.fullmatch(revision):
        raise BuildError("Select an explicit full source commit SHA")
    if not source.is_absolute() or source.resolve() != source:
        raise BuildError("Source must be a canonical absolute checkout path")
    git = ["git", "-C", str(source)]
    if Path(os.fsdecode(command(git + ["rev-parse", "--show-toplevel"])).strip()) != source:
        raise BuildError("Select the source checkout root")
    if command(git + ["rev-parse", "HEAD"]).decode().strip() != revision:
        raise BuildError("Source HEAD does not match the approved revision")
    if command(git + ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"]):
        raise BuildError("Local build requires clean source, including untracked files; never reset changes")
    for entry in command(git + ["ls-tree", "-r", "-z", revision]).split(b"\0"):
        if entry and entry.split(b" ", 1)[0] not in (b"100644", b"100755"):
            raise BuildError("Source links and submodules require explicit build-context support")
    tree = command(git + ["rev-parse", revision + "^{tree}"]).decode().strip()
    if not REVISION.fullmatch(tree):
        raise BuildError("Unsupported Git object format")
    return tree


def local_engine() -> str:
    if "DOCKER_HOST" in os.environ or "DOCKER_CONTEXT" in os.environ:
        raise BuildError("Unset Docker endpoint overrides; select a local controller context explicitly")
    try:
        context = json.loads(command(["docker", "context", "inspect"]))
        if not is_list(context) or len(context) != 1 or not is_mapping(context[0]):
            raise BuildError("Invalid controller Docker context")
        endpoints = context[0].get("Endpoints")
        if not is_mapping(endpoints) or not is_mapping(endpoints.get("docker")):
            raise BuildError("Invalid controller Docker endpoint")
        endpoint = endpoints["docker"].get("Host")
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
            raise BuildError("Controller builds require a local Docker endpoint")
        name = context[0].get("Name")
        if not isinstance(name, str) or not name:
            raise BuildError("Invalid local Docker context name")
        return name
    except (ValueError, KeyError, IndexError, TypeError):
        raise BuildError("Cannot establish a local Docker endpoint") from None


def local_docker() -> str:
    name = local_engine()
    try:
        builders = [json.loads(line) for line in command(
            ["docker", "buildx", "ls", "--format", "{{json .}}"]).splitlines()]
        if not any(
                is_mapping(builder) and builder.get("Name") == name and builder.get("Driver") == "docker"
                and is_list(builder.get("Nodes")) and len(builder["Nodes"]) == 1
                and is_mapping(builder["Nodes"][0])
                and builder["Nodes"][0].get("Endpoint") == name for builder in builders):
            raise BuildError("Controller requires a context-bound Docker builder, not a remote Buildx worker")
        return name
    except (ValueError, KeyError, IndexError, TypeError):
        raise BuildError("Cannot establish a local controller Docker builder") from None


def member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise BuildError("Invalid archive member path")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.rstrip("/").split("/")):
        raise BuildError("Unsafe archive member path")
    return path


def source_context(source: Path, revision: str, destination: Path) -> None:
    archive = destination / "source.tar"
    with archive.open("xb") as output:
        command(["git", "-C", str(source), "archive", "--format=tar", revision], log=output)
    if archive.stat().st_size > MAX_SOURCE:
        raise BuildError("Source archive exceeds the size limit")
    context = destination / "context"
    context.mkdir(mode=0o700)
    total = 0
    with tarfile.open(archive, "r:") as bundle:
        for index, member in enumerate(bundle):
            path = member_path(member.name)
            total += member.size
            if index >= 20000 or total > MAX_SOURCE or not (member.isdir() or member.isfile()):
                raise BuildError("Source archive has unsupported entries or exceeds limits")
            if path.parts[0] == ".git":
                raise BuildError("Git internals cannot be included in build context")
            target = context.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(mode=0o755)
                target.chmod(0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise BuildError("Source archive file is unreadable")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024**2)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def digest_stream(stream: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(1024**2):
        size += len(block)
        if size > MAX_ARCHIVE:
            raise BuildError("Image archive exceeds the size limit")
        digest.update(block)
    return "sha256:" + digest.hexdigest(), size


def image_identity(value: Any, reference: str, platform: str, revision: str) -> None:
    if not is_mapping(value):
        raise BuildError("Invalid built image metadata")
    config = value.get("Config")
    if not is_mapping(config) or not is_mapping(config.get("Labels")):
        raise BuildError("Built image requires source labels")
    labels = config["Labels"]
    if (value.get("Id") != reference or f'{value.get("Os")}/{value.get("Architecture")}' != platform
            or labels.get("org.opencontainers.image.revision") != revision
            or labels.get("org.opencontainers.image.source") != SOURCE):
        raise BuildError("Built image ID, platform or source labels do not match")


def archive_identity(stream: BinaryIO, image: dict[str, Any], platform: str, revision: str | None) -> str:
    """Inspect, never extract, the Docker save envelope and its config."""
    stream.seek(0)
    members: dict[str, tarfile.TarInfo] = {}
    total = 0
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        for index, member in enumerate(archive):
            member_path(member.name)
            total += member.size
            if (index >= 50000 or total > MAX_ARCHIVE or member.name in members
                    or not (member.isdir() or member.isfile())):
                raise BuildError("Image archive has unsafe, duplicate or excessive entries")
            members[member.name] = member

        def metadata(name: str) -> tuple[bytes, Any]:
            member = members.get(name)
            if member is None or not member.isfile() or member.size > MAX_METADATA:
                raise BuildError("Image archive metadata is missing or oversized")
            content = archive.extractfile(member)
            if content is None:
                raise BuildError("Image archive metadata is unreadable")
            with content:
                raw = content.read(MAX_METADATA + 1)
            return raw, json.loads(raw)

        _, manifest = metadata("manifest.json")
        if (not is_list(manifest) or len(manifest) != 1
                or not is_mapping(manifest[0]) or manifest[0].get("RepoTags") != [image["tag"]]):
            raise BuildError("Image archive must contain only the selected local image tag")
        selected = manifest[0]
        raw, config = metadata(selected["Config"])
        config_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not is_mapping(config) or not is_mapping(config.get("config")):
            raise BuildError("Archived image config is malformed")
        labels = config["config"].get("Labels")
        if (f'{config.get("os")}/{config.get("architecture")}' != platform
                or (revision is not None and (
                    not is_mapping(labels) or labels.get("org.opencontainers.image.revision") != revision
                    or labels.get("org.opencontainers.image.source") != SOURCE))):
            raise BuildError("Archived image config does not match the source/platform")
        if config_digest != image["reference"] or "index.json" in members:
            # Containerd-backed Docker identifies an image by its OCI root,
            # whereas classic Docker identifies it by the config digest.
            _, root = metadata("index.json")
            if (not is_mapping(root) or not is_list(root.get("manifests"))
                    or len(root["manifests"]) != 1 or not is_mapping(root["manifests"][0])):
                raise BuildError("Archive must select exactly one OCI root image")
            selected_root = root["manifests"][0]
            root_digest = selected_root.get("digest")
            if (not isinstance(root_digest, str) or not DIGEST.fullmatch(root_digest)
                    or (config_digest != image["reference"] and root_digest != image["reference"])):
                raise BuildError("Archive root does not match the recorded Docker image ID")
            annotations = selected_root.get("annotations", {})
            if (not is_mapping(annotations)
                    or annotations.get("io.containerd.image.name", image["tag"]) not in
                    (image["tag"], "docker.io/" + image["tag"])
                    or annotations.get("org.opencontainers.image.ref.name", image["tag"]) not in
                    (image["tag"], "docker.io/" + image["tag"], image["tag"].split(":")[1])
                    or annotations.get("config.digest", config_digest) != config_digest):
                raise BuildError("Archive root annotation selects an unrelated image tag or config")
            pending = [root_digest]
            visited: set[str] = set()
            configs: set[str] = set()
            while pending:
                digest = pending.pop()
                if digest in visited or len(visited) >= 64 or not DIGEST.fullmatch(digest):
                    raise BuildError("Archive image descriptor graph is invalid or excessive")
                visited.add(digest)
                raw_descriptor, descriptor = metadata("blobs/sha256/" + digest.split(":")[1])
                if "sha256:" + hashlib.sha256(raw_descriptor).hexdigest() != digest or not is_mapping(descriptor):
                    raise BuildError("Archive image descriptor digest mismatch")
                if "manifests" in descriptor:
                    if not is_list(descriptor["manifests"]) or not descriptor["manifests"]:
                        raise BuildError("Archive image index is empty or invalid")
                    for child in descriptor["manifests"]:
                        if not is_mapping(child) or not isinstance(child.get("digest"), str):
                            raise BuildError("Archive child image descriptor is invalid")
                        pending.append(child["digest"])
                else:
                    child = descriptor.get("config")
                    if not is_mapping(child) or not isinstance(child.get("digest"), str):
                        raise BuildError("Archive image config descriptor is invalid")
                    configs.add(child["digest"])
                    if child["digest"] != config_digest:
                        raise BuildError("Archive descriptor selects a different image config")
                    raw_config, _ = metadata("blobs/sha256/" + config_digest.split(":")[1])
                    if "sha256:" + hashlib.sha256(raw_config).hexdigest() != config_digest:
                        raise BuildError("Archive descriptor config blob mismatch")
                    descriptor_layers = descriptor.get("layers")
                    if not is_list(descriptor_layers) or any(
                            not is_mapping(layer) or not isinstance(layer.get("digest"), str)
                            or not DIGEST.fullmatch(layer["digest"]) for layer in descriptor_layers):
                        raise BuildError("Archive OCI layer descriptors are invalid")
                    if ["blobs/sha256/" + layer["digest"].split(":")[1]
                            for layer in descriptor_layers] != selected.get("Layers"):
                        raise BuildError("Archive image descriptor and Docker-save layers differ")
            if configs != {config_digest}:
                raise BuildError("Archive image ID does not select exactly the recorded config")
        layers = selected.get("Layers")
        if not is_list(layers) or not layers:
            raise BuildError("Image archive has no layer descriptors")
        for layer in layers:
            if not isinstance(layer, str) or layer not in members or not members[layer].isfile():
                raise BuildError("Image archive layer is missing")
        for name, member in members.items():
            if member.isfile() and re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name):
                content = archive.extractfile(member)
                if content is None:
                    raise BuildError("Image archive blob is unreadable")
                with content:
                    digest, _ = digest_stream(content)
                if digest != "sha256:" + name.rsplit("/", 1)[1]:
                    raise BuildError("Image archive blob digest mismatch")
        return config_digest


def verify_bundle(path: Path, manifest_sha256: str, gateway: str, platform: str) -> dict[str, Any]:
    if not DIGEST.fullmatch(manifest_sha256):
        raise BuildError("A separately retained local bundle manifest SHA-256 is required")
    try:
        with checked_directory(path, os.getuid()) as directory:
            raw, _ = read_file(directory, "manifest.json", os.getuid(), (0o400,))
            if len(raw) > MAX_METADATA:
                raise BuildError("Local build manifest exceeds the size limit")
            if "sha256:" + hashlib.sha256(raw).hexdigest() != manifest_sha256:
                raise BuildError("Local build manifest changed from the retained identity")
            value = validate_manifest(json.loads(raw), gateway, platform)
            for image in value["images"].values():
                fd = os.open(image["archive"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                            or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1
                            or before.st_size != image["bytes"]):
                        raise BuildError("Image archive requires immutable private single-link bytes")
                    digest, size = digest_stream(stream)
                    if digest != image["sha256"] or size != image["bytes"]:
                        raise BuildError("Image archive checksum mismatch")
                    archive_identity(stream, image, platform, value["revision"])
                    if (attributes(before) != attributes(os.fstat(stream.fileno()))
                            or attributes(before) != attributes(os.stat(image["archive"], dir_fd=directory,
                                                                        follow_symlinks=False))):
                        raise BuildError("Image archive changed during verification")
            return value
    except (OSError, ValueError, KeyError, TypeError, RecursionError, tarfile.TarError) as error:
        if isinstance(error, BuildError):
            raise
        raise BuildError("Local bundle verification failed; retain private evidence") from None


def build(source: Path, revision: str, parent: Path, gateway: str, platform: str, *,
          allow_build_network: bool = False) -> tuple[Path, str]:
    try:
        validate_request(gateway, platform)
        tree = clean_source(source, revision)
        if parent == source or source in parent.parents:
            raise BuildError("Build output must be outside the source checkout")
        context_name = local_docker()
        docker = ["docker", "--context", context_name]
        operation = str(uuid4())
        with checked_directory(parent, os.getuid()) as directory:
            os.mkdir(operation, 0o700, dir_fd=directory)
            os.fsync(directory)
        path = parent / operation
        images: dict[str, Any] = {}
        with (path / "build.log").open("xb") as log, tempfile.TemporaryDirectory(
                prefix="russound-build-", dir=path) as staging:
            os.fchmod(log.fileno(), 0o600)
            context_root = Path(staging)
            source_context(source, revision, context_root)
            if clean_source(source, revision) != tree:
                raise BuildError("Source changed during snapshot creation")
            for name in (("web", "ser2net") if gateway == "managed" else ("web",)):
                tag = f"mpdrussoundpi-local/{name}:{operation}"
                command(docker + ["buildx", "build", "--builder", context_name,
                         "--load", "--provenance=false", "--sbom=false",
                         "--platform", platform, "--network", "default" if allow_build_network else "none",
                         "--label", "org.opencontainers.image.revision=" + revision,
                         "--label", "org.opencontainers.image.source=" + SOURCE,
                         "--tag", tag, "-f", DOCKERFILES[name], "."],
                        cwd=context_root / "context", log=log, timeout=1800)
                inspected = json.loads(command(docker + ["image", "inspect", tag]))[0]
                reference = inspected.get("Id")
                if not isinstance(reference, str) or not DIGEST.fullmatch(reference):
                    raise BuildError("Built image has no exact Docker identity")
                image_identity(inspected, reference, platform, revision)
                archive = path / (name + ".tar")
                command(docker + ["image", "save", "--output", str(archive), tag], log=log, timeout=600)
                archive.chmod(0o400)
                with archive.open("rb") as stream:
                    os.fsync(stream.fileno())
                    digest, size = digest_stream(stream)
                images[name] = {"reference": reference, "tag": tag, "archive": archive.name,
                                "sha256": digest, "bytes": size}
            if clean_source(source, revision) != tree:
                raise BuildError("Source changed during build; do not use partial artifacts")
            log.flush()
            os.fsync(log.fileno())
        value: dict[str, Any] = {"schema_version": 1, "kind": "local-development-image-bundle",
                 "operation": operation, "revision": revision, "tree": tree,
                 "gateway": gateway, "platform": platform,
                 "built_at": datetime.now(timezone.utc).isoformat(),
                 "build_network": allow_build_network, "images": images}
        validate_manifest(value, gateway, platform)
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        with checked_directory(path, os.getuid()) as directory:
            write_file(directory, "manifest.json", raw, owner=os.getuid(), group=os.getgid(), mode=0o400)
        manifest_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
        verify_bundle(path, manifest_sha256, gateway, platform)
        return path, manifest_sha256
    except BuildError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError, tarfile.TarError):
        raise BuildError("Local build incomplete; preserve artifacts and inspect private build.log") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--gateway", choices=("managed", "external", "dummy"), default="managed")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"), default="linux/arm64")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--allow-build-network", action="store_true",
                        help="Allow RUN package downloads; base-image resolution can access the registry independently")
    args = parser.parse_args()
    if args.action == "build":
        if (args.source is None or args.revision is None or args.output_parent is None
                or args.bundle is not None or args.manifest_sha256 is not None):
            parser.error("build requires --source, --revision, --output-parent and no bundle inputs")
    elif (args.bundle is None or args.manifest_sha256 is None or args.source is not None
          or args.revision is not None or args.output_parent is not None or args.allow_build_network):
        parser.error("verify requires only --bundle, --manifest-sha256 and layout/platform")
    try:
        os.umask(0o077)
        if args.action == "build":
            assert args.source is not None and args.revision is not None and args.output_parent is not None
            path, digest = build(args.source, args.revision, args.output_parent, args.gateway,
                                 args.platform, allow_build_network=args.allow_build_network)
            print(json.dumps({"bundle": str(path), "manifest_sha256": digest, "target_changed": False}))
        else:
            assert args.bundle is not None and args.manifest_sha256 is not None
            value = verify_bundle(args.bundle, args.manifest_sha256, args.gateway, args.platform)
            print(json.dumps({"kind": value["kind"], "operation": value["operation"],
                              "revision": value["revision"], "gateway": value["gateway"],
                              "platform": value["platform"],
                              "images": {name: image["reference"] for name, image in value["images"].items()},
                              "target_changed": False, "apply_supported": False}))
        return 0
    except (BuildError, FileSafetyError, SelectionError, StateError) as error:
        print(f"Local build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
