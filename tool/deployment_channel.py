"""Bounded duplex deployment framing; filenames are validated, never extracted."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any, BinaryIO, IO

from tool.deployment_backup import MAX_BYTES, MAX_MANIFEST, manifest_value
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_inputs import attributes
from tool.deployment_state import operation_id


class ChannelError(ValueError):
    """Transport is incomplete or uncertain; retain both endpoints."""


class Channel:
    def __init__(self, source: IO[bytes], destination: IO[bytes]):
        self.source, self.destination = source, destination

    def exact(self, count: int, output: BinaryIO | None = None) -> bytes:
        value = bytearray()
        while count:
            block = self.source.read(min(count, 1024**2))
            if not block:
                raise ChannelError("Deployment channel ended unexpectedly; inspect target pending state")
            count -= len(block)
            if output is None:
                value.extend(block)
            elif output.write(block) != len(block):
                raise ChannelError("Deployment output write was incomplete")
        return bytes(value)

    def send(self, value: dict[str, Any]) -> None:
        raw = (json.dumps(value, sort_keys=True) + "\n").encode()
        if len(raw) > MAX_MANIFEST:
            raise ChannelError("Deployment frame exceeds the metadata limit")
        self.destination.write(struct.pack("!Q", len(raw)))
        self.destination.write(raw)
        self.destination.flush()

    def receive(self) -> dict[str, Any]:
        count = struct.unpack("!Q", self.exact(8))[0]
        if not 0 < count <= MAX_MANIFEST:
            raise ChannelError("Deployment frame exceeds the metadata limit")
        value = json.loads(self.exact(count))
        if not is_mapping(value):
            raise ChannelError("Expected a deployment object")
        return value

    def send_file(self, path: Path) -> None:
        with checked_directory(path.parent, os.getuid()) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1
                        or not 0 <= before.st_size <= MAX_BYTES):
                    raise ChannelError("Deployment transfer source requires a private immutable regular file")
                digest = hashlib.file_digest(source, "sha256").hexdigest()
                source.seek(0)
                self.send({"file": path.name, "bytes": before.st_size, "sha256": digest})
                while block := source.read(1024**2):
                    self.destination.write(block)
                self.destination.flush()
                if (attributes(before) != attributes(os.fstat(source.fileno()))
                        or attributes(before) != attributes(os.stat(path.name, dir_fd=directory, follow_symlinks=False))):
                    raise ChannelError("Source file changed during deployment transfer")

    def receive_file(self, parent: Path, expected: str, *, size: int | None = None,
                     checksum: str | None = None, maximum: int = MAX_BYTES) -> None:
        header = self.receive()
        if (set(header) != {"file", "bytes", "sha256"} or header["file"] != expected
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", expected)
                or expected in (".", "..") or type(header["bytes"]) is not int
                or not 0 <= header["bytes"] <= MAX_BYTES
                or not isinstance(header["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", header["sha256"])
                or header["bytes"] > maximum or (size is not None and header["bytes"] != size)
                or (checksum is not None and header["sha256"] != checksum)):
            raise ChannelError("Unexpected deployment payload descriptor")
        with checked_directory(parent, os.getuid()) as directory:
            fd = os.open(expected, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                self.exact(header["bytes"], target)
                target.flush()
                os.fchmod(target.fileno(), 0o400)
                os.fsync(target.fileno())
            fd = os.open(expected, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != header["sha256"]:
                    raise ChannelError("Deployment payload checksum mismatch")
            os.fsync(directory)


def portable_backup(path: Path, operation: str, checksum: str) -> dict[str, Any]:
    """Verify off-host private backup bytes without Linux source metadata APIs.

    Restoration metadata is part of the authenticated manifest; storage on the
    controller need not support Linux xattrs, ACLs or the original service IDs.
    """
    operation_id(operation)
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ChannelError("Backup requires a separately retained manifest identity")
    with checked_directory(path, os.getuid()) as directory:
        def contents(name: str, size: int | None = None, limit: int = MAX_MANIFEST) -> bytes:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1
                        or (size is not None and info.st_size != size) or info.st_size > limit):
                    raise ChannelError("Unsafe or oversized backup payload")
                return source.read(limit + 1)
        raw = contents("manifest.json")
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ChannelError("Off-host manifest differs from the target identity")
        manifest = manifest_value(json.loads(raw), operation)
        expected = {"manifest.json"}
        for entry in manifest["entries"]:
            if entry["type"] != "file":
                continue
            expected.add(entry["blob"])
            fd = os.open(entry["blob"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1
                        or info.st_size != entry["size"]
                        or hashlib.file_digest(source, "sha256").hexdigest() != entry["sha256"]):
                    raise ChannelError("Off-host backup content mismatch")
        if set(os.listdir(directory)) != expected:
            raise ChannelError("Off-host backup has partial or unexpected payloads")
    return manifest


def send_backup(channel: Channel, path: Path, operation: str, checksum: str) -> None:
    manifest = portable_backup(path, operation, checksum)
    channel.send({"event": "backup", "operation": operation, "manifest_sha256": checksum})
    channel.send_file(path / "manifest.json")
    for entry in manifest["entries"]:
        if entry["type"] == "file":
            channel.send_file(path / entry["blob"])
    portable_backup(path, operation, checksum)


def receive_backup(channel: Channel, parent: Path, header: dict[str, Any]) -> tuple[Path, str]:
    if set(header) != {"event", "operation", "manifest_sha256"} or header["event"] != "backup":
        raise ChannelError("Missing off-host recovery header")
    operation, checksum = header["operation"], header["manifest_sha256"]
    operation_id(operation)
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ChannelError("Invalid backup manifest identity")
    with checked_directory(parent, os.getuid()) as directory:
        os.mkdir(operation, 0o700, dir_fd=directory)
        os.fsync(directory)
    path = parent / operation
    channel.receive_file(path, "manifest.json", maximum=MAX_MANIFEST, checksum=checksum)
    raw = (path / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ChannelError("Received manifest differs from target identity")
    manifest = manifest_value(json.loads(raw), operation)
    for entry in manifest["entries"]:
        if entry["type"] == "file":
            channel.receive_file(path, entry["blob"], size=entry["size"], checksum=entry["sha256"])
    portable_backup(path, operation, checksum)
    return path, checksum
