"""Internal, one-shot image loading for a reserved local development operation.

No load-only operator CLI or SSH transport is exposed. The invoking runner must custody
approval and backup evidence before recording the backup-complete boundary.
"""

from contextlib import ExitStack, contextmanager
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from typing import Any, BinaryIO, Generator

from tool.deployment_build import archive_identity, command, digest_stream, image_identity, local_engine, verify_bundle
from tool.deployment_files import checked_directory, is_list, is_mapping
from tool.deployment_inputs import attributes
from tool.deployment_images import verify
from tool.deployment_local import LOCAL_CANDIDATE, validate_candidate
from tool.deployment_state import StateError, StateStore, operation_id


class LoadError(ValueError):
    """Image loading failed or is uncertain; preserve evidence and reservation."""


@contextmanager
def bundle_streams(path: Path, checksum: str, gateway: str,
                   platform: str) -> Generator[tuple[dict[str, Any], dict[str, BinaryIO], dict[str, str]], None, None]:
    manifest = verify_bundle(path, checksum, gateway, platform)
    with ExitStack() as stack:
        directory = stack.enter_context(checked_directory(path, os.getuid()))
        streams: dict[str, BinaryIO] = {}
        configs: dict[str, str] = {}
        for name, image in manifest["images"].items():
            fd = os.open(image["archive"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            stream = stack.enter_context(os.fdopen(fd, "rb"))
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1
                    or before.st_size != image["bytes"]):
                raise LoadError("Local image archive is unsafe or changed")
            digest, size = digest_stream(stream)
            if digest != image["sha256"] or size != image["bytes"]:
                raise LoadError("Local image archive checksum changed")
            configs[name] = archive_identity(stream, image, platform, manifest["revision"])
            stream.seek(0)
            streams[name] = stream
            if attributes(before) != attributes(os.fstat(stream.fileno())):
                raise LoadError("Local image archive changed during verification")
        fingerprints = {name: attributes(os.fstat(stream.fileno())) for name, stream in streams.items()}
        yield manifest, streams, configs
        for name, stream in streams.items():
            current = os.stat(manifest["images"][name]["archive"], dir_fd=directory, follow_symlinks=False)
            if (fingerprints[name] != attributes(current)
                    or fingerprints[name] != attributes(os.fstat(stream.fileno()))):
                raise LoadError("Local image archive changed while loading; inspect daemon and pending state")
        if verify_bundle(path, checksum, gateway, platform) != manifest:
            raise LoadError("Local bundle changed while loading")


def select_bundle(path: Path, checksum: str, gateway: str, platform: str) -> dict[str, Any]:
    """Prepare portable non-secret state metadata; this does not approve or load."""
    with bundle_streams(path, checksum, gateway, platform) as (manifest, _, configs):
        selection: dict[str, Any] = {"schema_version": 1, "kind": LOCAL_CANDIDATE, "manifest_sha256": checksum,
                                     "manifest": manifest, "configs": configs}
        validate_candidate(selection)
    return selection


def load_reserved_bundle(store: StateStore, deployment: str, operation: str, intent_digest: str,
                         selection: dict[str, Any], bundle: Path, evidence: Path) -> dict[str, str]:
    """Load already-transferred private bytes into the local target daemon once.

    Failure after the durable load marker may mean Docker is still working.
    Never automatically retry, roll back, finalize, or release the reservation.
    """
    try:
        operation_id(operation)
        if store.owner_uid != os.getuid():
            raise LoadError("Target image loading must run as the deployment-state owner")
        if (evidence == store.root or store.root in evidence.parents or evidence in store.root.parents
                or evidence == bundle or bundle in evidence.parents):
            raise LoadError("Image-load evidence must be separate from state and bundle storage")
        selection = copy.deepcopy(selection)
        validate_candidate(selection)
        manifest = selection["manifest"]
        gateway, platform = manifest["gateway"], manifest["platform"]
        docker = ["docker", "--context", local_engine()]
        info = json.loads(command(docker + ["info", "--format", "{{json .}}"]))
        if not is_mapping(info):
            raise LoadError("Invalid local Docker daemon metadata")
        architecture = info.get("Architecture")
        if not isinstance(architecture, str):
            raise LoadError("Invalid local Docker daemon architecture")
        architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(architecture, architecture)
        security = info.get("SecurityOptions")
        if (f'{info.get("OSType")}/{architecture}' != platform or not is_list(security)
                or any(not isinstance(option, str) or "rootless" in option or "userns" in option
                       for option in security)):
            raise LoadError("Target daemon platform or namespace mode is unsupported")
        loaded: dict[str, str] = {}
        with bundle_streams(bundle, selection["manifest_sha256"], gateway, platform) as (actual, streams, configs):
            if actual != manifest or configs != selection["configs"]:
                raise LoadError("Transferred artifacts differ from the approved local selection")
            for name, image in manifest["images"].items():
                existing = command(docker + ["image", "ls", "--no-trunc", "--quiet",
                                            "--filter", "reference=" + image["tag"]]).decode().splitlines()
                if any(value not in (image["reference"], configs[name]) for value in existing):
                    raise LoadError("Local operation tag already names an unrelated image")
            with checked_directory(evidence, os.getuid()) as directory:
                fd = os.open(operation + ".image-load.log",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "wb") as log:
                    os.fchmod(log.fileno(), 0o600)
                    os.fsync(log.fileno())
                    os.fsync(directory)
                    store.start_image_load(deployment, operation, selection, intent_digest)
                    for name, image in manifest["images"].items():
                        result = subprocess.run(docker + ["image", "load"], stdin=streams[name],
                                                stdout=log, stderr=log, timeout=600)
                        if result.returncode:
                            raise LoadError("Docker image load failed; inspect private evidence and pending state")
                        inspected = json.loads(command(docker + ["image", "inspect", image["tag"]]))[0]
                        reference = inspected.get("Id") if is_mapping(inspected) else None
                        if not isinstance(reference, str) or reference not in (image["reference"], configs[name]):
                            raise LoadError("Loaded image ID is not the approved root or config identity")
                        image_identity(inspected, reference, platform, manifest["revision"])
                        loaded[name] = reference
                    for name, reference in loaded.items():
                        inspected = json.loads(command(docker + ["image", "inspect",
                                                                manifest["images"][name]["tag"]]))[0]
                        image_identity(inspected, reference, platform, manifest["revision"])
                    log.flush()
                    os.fsync(log.fileno())
        store.complete_image_load(deployment, operation, selection, intent_digest, loaded)
        return loaded
    except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError,
            tarfile.TarError, subprocess.TimeoutExpired) as error:
        if isinstance(error, (LoadError, StateError)):
            raise
        raise LoadError("Local image load failed or is uncertain; retain private evidence and reservation") from None


def load_registry_candidate(store: StateStore, deployment: str, operation: str, intent_digest: str,
                            selection: dict[str, Any], evidence: Path) -> dict[str, str]:
    """Pull only the pinned registry pair and verify actual platform/config IDs."""
    verify(selection)
    if store.owner_uid != os.getuid():
        raise LoadError("Registry loader must execute as target state owner")
    docker = ["docker", "--context", local_engine()]
    loaded: dict[str, str] = {}
    with checked_directory(evidence, os.getuid()) as directory:
        fd = os.open(operation + ".image-load.log",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as log:
            os.fchmod(log.fileno(), 0o600)
            os.fsync(log.fileno())
            os.fsync(directory)
            store.start_registry_pull(deployment, operation, selection, intent_digest)
            for name, image in selection["images"].items():
                result = subprocess.run(docker + ["pull", "--platform", selection["platform"], image["reference"]],
                                        stdout=log, stderr=log, timeout=600)
                if result.returncode:
                    raise LoadError("Pinned image pull failed; retain reservation and evidence")
                actual = json.loads(command(docker + ["image", "inspect", image["reference"]]))[0]
                reference = actual["Id"]
                if reference not in (image["index_digest"], image["platform_digest"], image["config_digest"]):
                    raise LoadError("Pulled image ID differs from approved registry metadata")
                image_identity(actual, reference, selection["platform"], selection["revision"])
                loaded[name] = reference
            log.flush()
            os.fsync(log.fileno())
    return loaded
