"""Internal reviewed SSH handoff for reserved local image artifacts.

The receiver is callable by the future runner; it never provisions state,
loads Docker images, stops services or grants deployment approval.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import tempfile
from typing import Any, BinaryIO

from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_images import DIGEST, MAX_METADATA
from tool.deployment_inputs import read_file, write_file
from tool.deployment_load import bundle_streams, select_bundle
from tool.deployment_local import validate_candidate
from tool.deployment_state import STATE_ROOT, StateStore, identity, operation_id


class TransferError(ValueError):
    """Transport failed or is uncertain; retain evidence and inspect both ends."""


def encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def ssh_arguments(host: str, user: str, port: int, known_hosts: Path,
                  identity_file: Path | None = None) -> list[str]:
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", host)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", user)
            or type(port) is not int or not 1 <= port <= 65535):
        raise TransferError("Select an explicit SSH host, user and port")
    for path in (known_hosts, identity_file):
        if path is not None and (not path.is_absolute() or ".." in path.parts
                                 or re.search(r"[\s%]", str(path))):
            raise TransferError("SSH trust/key paths must be absolute without expansion tokens")
    argv = ["ssh", "-F", "/dev/null", "-T",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=" + str(known_hosts), "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no", "-o", "VerifyHostKeyDNS=no",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-p", str(port), "-l", user]
    if identity_file is not None:
        argv.extend(["-o", "IdentitiesOnly=yes", "-i", str(identity_file)])
    return argv


def exact(source: BinaryIO, size: int, destination: BinaryIO) -> None:
    while size:
        block = source.read(min(size, 1024**2))
        if not block:
            raise TransferError("Artifact stream ended before its declared length")
        if destination.write(block) != len(block):
            raise TransferError("Artifact stream write was incomplete")
        size -= len(block)


def request(value: Any) -> dict[str, Any]:
    if (not is_mapping(value)
            or set(value) != {"schema_version", "deployment", "operation", "intent_digest",
                             "selection", "manifest_bytes"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["intent_digest"], str) or not DIGEST.fullmatch(value["intent_digest"])
            or type(value["manifest_bytes"]) is not int or not 0 < value["manifest_bytes"] <= MAX_METADATA):
        raise TransferError("Invalid image-transfer request")
    identity(value["deployment"])
    operation_id(value["operation"])
    validate_candidate(value["selection"])
    return value


def receipt(value: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "kind": "verified-local-image-transfer",
            "deployment": value["deployment"], "operation": value["operation"],
            "intent_digest": value["intent_digest"],
            "manifest_sha256": value["selection"]["manifest_sha256"]}


def receive(store: StateStore, parent: Path, source: BinaryIO) -> dict[str, Any]:
    """Receive one framed stream into a new private operation directory."""
    header = source.read(8)
    if len(header) != 8:
        raise TransferError("Missing image-transfer request length")
    length = struct.unpack("!Q", header)[0]
    if not 0 < length <= MAX_METADATA:
        raise TransferError("Image-transfer request exceeds the metadata limit")
    with tempfile.TemporaryFile() as metadata:
        exact(source, length, metadata)
        metadata.seek(0)
        value = request(json.load(metadata))
    if store.owner_uid != os.getuid():
        raise TransferError("Receiver must run as the deployment-state owner")
    if parent == store.root or store.root in parent.parents or parent in store.root.parents:
        raise TransferError("Received images must be separate from deployment state")
    selection = value["selection"]
    manifest = selection["manifest"]
    with checked_directory(parent, os.getuid()) as directory:
        # A marker persists even if directory creation or receiving later fails.
        store.start_image_transfer(value["deployment"], value["operation"], selection, value["intent_digest"])
        os.mkdir(value["operation"], mode=0o700, dir_fd=directory)
        os.fsync(directory)
    path = parent / value["operation"]
    with checked_directory(path, os.getuid()) as directory:
        files = [("manifest.json", value["manifest_bytes"])]
        files.extend((image["archive"], image["bytes"]) for _, image in sorted(manifest["images"].items()))
        for name, size in files:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                exact(source, size, target)
                target.flush()
                os.fchmod(target.fileno(), 0o400)
                os.fsync(target.fileno())
            os.fsync(directory)
        if source.read(1):
            raise TransferError("Unexpected trailing bytes in image-transfer stream")
        actual = select_bundle(path, selection["manifest_sha256"], manifest["gateway"], manifest["platform"])
        if actual != selection:
            raise TransferError("Received images differ from the reserved candidate")
        result = receipt(value)
        write_file(directory, "transfer.json", encoded(result),
                   owner=os.getuid(), group=os.getgid(), mode=0o400)
    store.complete_image_transfer(value["deployment"], value["operation"], selection, value["intent_digest"])
    return result


class ImageTransfer:
    """In-memory preview seal; an acknowledgement is not proof of human approval.

    The future controller runner must collect explicit approval for review()
    and keep this object, source bytes and SSH trust under its custody.
    """

    def __init__(self, bundle: Path, checksum: str, gateway: str, platform: str, *,
                 deployment: str, operation: str, intent_digest: str, host: str, user: str,
                 port: int, known_hosts: Path, remote_tools: Path, remote_parent: Path,
                 identity_file: Path | None = None):
        if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", host)
                or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", user)
                or type(port) is not int or not 1 <= port <= 65535):
            raise TransferError("Select an explicit SSH host, user and port")
        for path in (remote_tools, remote_parent):
            if not path.is_absolute() or ".." in path.parts or path == Path("/") or "\n" in str(path):
                raise TransferError("Remote tooling and artifact roots must be explicit absolute paths")
        for path in (known_hosts, identity_file):
            if path is not None and (not path.is_absolute() or ".." in path.parts
                                     or re.search(r"[\s%]", str(path))):
                raise TransferError("SSH trust/key paths must be absolute without whitespace or expansion tokens")
        self.bundle = bundle
        self.known_hosts = known_hosts
        self.identity_file = identity_file
        with checked_directory(known_hosts.parent, os.getuid(), private=False) as directory:
            trust, self._trust_attributes = read_file(directory, known_hosts.name, os.getuid(), (0o400, 0o600))
        self._trust = hashlib.sha256(trust).hexdigest()
        self._key_attributes = None
        if identity_file is not None:
            with checked_directory(identity_file.parent, os.getuid(), private=False) as directory:
                _, self._key_attributes = read_file(directory, identity_file.name, os.getuid(), (0o400, 0o600))
        with checked_directory(bundle, os.getuid()) as directory:
            raw, _ = read_file(directory, "manifest.json", os.getuid(), (0o400,))
        self._request = request({
            "schema_version": 1, "deployment": deployment, "operation": operation, "intent_digest": intent_digest,
            "selection": select_bundle(bundle, checksum, gateway, platform), "manifest_bytes": len(raw),
        })
        self._argv = ssh_arguments(host, user, port, known_hosts, identity_file)
        self._argv.extend([host, "cd " + shlex.quote(str(remote_tools))
                           + " && exec /usr/bin/python3 -m tool.deployment_transfer receive --parent "
                           + shlex.quote(str(remote_parent))])
        self._plan: dict[str, Any] = {"kind": "local-image-transfer-review", "request": self._request,
                                      "ssh": self._argv, "known_hosts_sha256": self._trust}
        self._seal = hashlib.sha256(encoded(self._plan)).hexdigest()

    def review(self) -> dict[str, Any]:
        return {"digest": "sha256:" + self._seal, "plan": copy.deepcopy(self._plan)}

    def _verify(self, approved_digest: str) -> None:
        if (approved_digest != "sha256:" + self._seal
                or hashlib.sha256(encoded(self._plan)).hexdigest() != self._seal
                or self._plan["request"] != self._request or self._plan["ssh"] != self._argv):
            raise TransferError("Transfer acknowledgement differs from the reviewed target/artifacts")
        with checked_directory(self.known_hosts.parent, os.getuid(), private=False) as directory:
            raw, info = read_file(directory, self.known_hosts.name, os.getuid(), (0o400, 0o600))
        if info != self._trust_attributes or hashlib.sha256(raw).hexdigest() != self._trust:
            raise TransferError("SSH host trust changed; request a new review")
        if self.identity_file is not None:
            with checked_directory(self.identity_file.parent, os.getuid(), private=False) as directory:
                _, info = read_file(directory, self.identity_file.name, os.getuid(), (0o400, 0o600))
            if info != self._key_attributes:
                raise TransferError("SSH identity changed; request a new review")

    def send(self, approved_digest: str, evidence: Path) -> dict[str, Any]:
        """One SSH invocation; no retry or fallback after any uncertain result."""
        try:
            self._verify(approved_digest)
            value = self._request
            selection = value["selection"]
            manifest = selection["manifest"]
            if (evidence == self.bundle or self.bundle in evidence.parents
                    or evidence in self.bundle.parents):
                raise TransferError("Transport evidence must be separate from image storage")
            if re.search(r"[\s%]", str(evidence)):
                raise TransferError("Transport evidence path must not contain SSH filename expansion tokens")
            with bundle_streams(self.bundle, selection["manifest_sha256"],
                                manifest["gateway"], manifest["platform"]) as (actual, streams, configs):
                if actual != manifest or configs != selection["configs"]:
                    raise TransferError("Source artifacts changed after transfer review")
                with checked_directory(evidence, os.getuid()) as directory:
                    fd = os.open(value["operation"] + ".transfer.log",
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    with os.fdopen(fd, "wb") as log, tempfile.TemporaryFile(dir=evidence) as wire, \
                            tempfile.TemporaryFile(dir=evidence) as output:
                        os.fchmod(log.fileno(), 0o600)
                        log.write(encoded({"review_digest": approved_digest, "state": "starting-ssh-transfer"}))
                        log.flush()
                        os.fsync(log.fileno())
                        os.fsync(directory)
                        metadata = encoded(value)
                        wire.write(struct.pack("!Q", len(metadata)))
                        wire.write(metadata)
                        with checked_directory(self.bundle, os.getuid()) as source:
                            raw, _ = read_file(source, "manifest.json", os.getuid(), (0o400,))
                        if len(raw) != value["manifest_bytes"]:
                            raise TransferError("Source manifest length changed")
                        wire.write(raw)
                        for name in sorted(streams):
                            exact(streams[name], manifest["images"][name]["bytes"], wire)
                        wire.flush()
                        wire.seek(0)
                        self._verify(approved_digest)
                        with checked_directory(self.known_hosts.parent, os.getuid(), private=False) as trust_source:
                            trust, info = read_file(trust_source, self.known_hosts.name, os.getuid(), (0o400, 0o600))
                        if info != self._trust_attributes or hashlib.sha256(trust).hexdigest() != self._trust:
                            raise TransferError("SSH trust changed while preparing the transfer")
                        trust_name = value["operation"] + ".known_hosts"
                        write_file(directory, trust_name, trust, owner=os.getuid(), group=os.getgid(), mode=0o400)
                        argv = self._argv.copy()
                        argv[argv.index("UserKnownHostsFile=" + str(self.known_hosts))] = (
                            "UserKnownHostsFile=" + str(evidence / trust_name))
                        result = subprocess.run(argv, stdin=wire, stdout=output, stderr=log, timeout=1800)
                        log.flush()
                        os.fsync(log.fileno())
                        if result.returncode:
                            raise TransferError("SSH transfer failed; inspect private evidence and target reservation")
                        output.seek(0)
                        response = output.read(MAX_METADATA + 1)
                        if len(response) > MAX_METADATA:
                            raise TransferError("Target verification receipt exceeds the size limit")
                        verified = json.loads(response)
                        if (not is_mapping(verified) or type(verified.get("schema_version")) is not int
                                or verified != receipt(value)):
                            raise TransferError("Missing or mismatched target verification receipt")
                        self._verify(approved_digest)
            with checked_directory(evidence, os.getuid()) as directory:
                write_file(directory, value["operation"] + ".transfer.json", response,
                           owner=os.getuid(), group=os.getgid(), mode=0o400)
            return receipt(value)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.TimeoutExpired) as error:
            if isinstance(error, TransferError):
                raise
            raise TransferError("Image transfer failed or is uncertain; retain both ends and do not retry") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("receive",))
    parser.add_argument("--parent", type=Path, required=True)
    args = parser.parse_args()
    try:
        os.umask(0o077)
        result = receive(StateStore(STATE_ROOT), args.parent, sys.stdin.buffer)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print("Image transfer blocked or uncertain; inspect private target evidence", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
