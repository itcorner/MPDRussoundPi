"""Private approved-input snapshots for the unfinished deployment runner."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Generator

from tool.deployment_files import FileSafetyError, checked_directory
from tool.deployment_state import StateError, identity, operation_id


MAX_INPUT = 16 * 1024 * 1024


class InputError(ValueError):
    """Input preparation, drift or installation failed; retain private evidence."""


def fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def attributes(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
        "mtime": info.st_mtime_ns, "ctime": info.st_ctime_ns,
    }


def read_file(directory: int, name: str, owner: int, modes: tuple[int, ...]) -> tuple[bytes, dict[str, int]]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != owner or stat.S_IMODE(before.st_mode) not in modes
                or before.st_size > MAX_INPUT):
            raise InputError("Protected file has unsafe ownership, permissions, type or size")
        raw = source.read(MAX_INPUT + 1)
        after = os.fstat(source.fileno())
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (len(raw) > MAX_INPUT or len(raw) != after.st_size
                or attributes(before) != attributes(after) or attributes(after) != attributes(current)):
            raise InputError("Protected file changed while being read")
        return raw, attributes(after)


def write_file(directory: int, name: str, content: bytes, *, owner: int, group: int, mode: int) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as target:
        target.write(content)
        target.flush()
        info = os.fstat(target.fileno())
        if (info.st_uid, info.st_gid) != (owner, group):
            os.fchown(target.fileno(), owner, group)
        os.fchmod(target.fileno(), mode)
        os.fsync(target.fileno())
    os.fsync(directory)


def runtime_traversal(path: Path, owner: int, group: int) -> None:
    for ancestor in path.parents:
        info = ancestor.stat()
        shift = 6 if info.st_uid == owner else 3 if info.st_gid == group else 0
        if not (stat.S_IMODE(info.st_mode) >> shift) & 1:
            raise InputError("Service identity lacks runtime ancestor traversal permissions")


class InputSnapshot:
    """One operation's private snapshot, with an in-memory approval seal.

    The manifest contains sensitive source paths and fingerprints: never emit it
    into logs, previews or deployment records. This first API intentionally has
    no reload/transfer or operator CLI until seal custody is integrated.
    """

    def __init__(self, parent: Path, operation: str, owner_uid: int):
        operation_id(operation)
        self.parent = parent
        self.operation = operation
        self.owner_uid = owner_uid
        self.path = parent / operation
        self._manifest: dict[str, Any] = {}
        self._seal: str | None = None

    @contextmanager
    def _directory(self) -> Generator[int, None, None]:
        with checked_directory(self.path, self.owner_uid) as fd:
            yield fd

    @classmethod
    def create(cls, parent: Path, operation: str, sources: dict[str, Path],
               *, owner_uid: int | None = None) -> "InputSnapshot":
        owner = os.getuid() if owner_uid is None else owner_uid
        snapshot = cls(parent, operation, owner)
        if not sources or len(sources) > 64:
            raise InputError("Select between one and 64 protected inputs")
        try:
            for name in sources:
                identity(name)
            with checked_directory(parent, owner) as directory:
                os.mkdir(operation, 0o700, dir_fd=directory)
                os.fsync(directory)
            entries: dict[str, Any] = {}
            with snapshot._directory() as directory:
                for name, path in sources.items():
                    if not path.is_absolute() or ".." in path.parts:
                        raise InputError("Protected input paths must be absolute without traversal")
                    with checked_directory(path.parent, owner, private=False) as source:
                        raw, info = read_file(source, path.name, owner, (0o400, 0o600))
                    write_file(directory, name + ".input", raw, owner=owner, group=os.getgid(), mode=0o400)
                    entries[name] = {"source": str(path), "attributes": info, "fingerprint": fingerprint(raw)}
                manifest: dict[str, Any] = {"schema_version": 1, "operation": operation, "inputs": entries}
                raw_manifest = (json.dumps(manifest, sort_keys=True) + "\n").encode()
                write_file(directory, "manifest.json", raw_manifest,
                           owner=owner, group=os.getgid(), mode=0o400)
                snapshot._seal = fingerprint(raw_manifest)
                snapshot._manifest = manifest
            snapshot.verify_sources()
            return snapshot
        except (OSError, FileSafetyError, StateError):
            raise InputError("Cannot create protected snapshot; retain staging and inspect inputs") from None

    def _verify(self, directory: int) -> None:
        raw, _ = read_file(directory, "manifest.json", self.owner_uid, (0o400,))
        if self._seal is None or fingerprint(raw) != self._seal:
            raise InputError("Approved snapshot manifest integrity changed")
        expected = {"manifest.json"} | {name + ".input" for name in self._manifest["inputs"]}
        if set(os.listdir(directory)) != expected:
            raise InputError("Snapshot contains missing or unexpected entries")
        for name, entry in self._manifest["inputs"].items():
            content, _ = read_file(directory, name + ".input", self.owner_uid, (0o400,))
            if fingerprint(content) != entry["fingerprint"]:
                raise InputError("Approved snapshot content changed")

    def verify_sources(self) -> None:
        """Repeat after reservation and again immediately before consuming inputs."""
        try:
            with self._directory() as directory:
                self._verify(directory)
                for entry in self._manifest["inputs"].values():
                    path = Path(entry["source"])
                    with checked_directory(path.parent, self.owner_uid, private=False) as source:
                        raw, info = read_file(source, path.name, self.owner_uid, (0o400, 0o600))
                    if info != entry["attributes"] or fingerprint(raw) != entry["fingerprint"]:
                        raise InputError("Protected source changed; request a new preview and approval")
        except (OSError, FileSafetyError):
            raise InputError("Protected source or snapshot is missing, unreadable or unsafe") from None

    def install_new(self, name: str, destination: Path, *, owner_uid: int, group_gid: int) -> None:
        """Install approved bytes to a stable private path; never replace any file."""
        if name not in self._manifest.get("inputs", {}):
            raise InputError("Unknown protected input identifier")
        if destination == self.parent or self.parent in destination.parents:
            raise InputError("Runtime input must not depend on temporary staging")
        self.verify_sources()
        try:
            with self._directory() as directory:
                self._verify(directory)
                content, _ = read_file(directory, name + ".input", self.owner_uid, (0o400,))
            with checked_directory(destination.parent, owner_uid) as target:
                runtime_traversal(destination, owner_uid, group_gid)
                write_file(target, destination.name, content, owner=owner_uid, group=group_gid, mode=0o400)
            self.verify_installed(name, destination, owner_uid=owner_uid, group_gid=group_gid)
        except (OSError, FileSafetyError):
            raise InputError("Runtime input installation failed; preserve staging and inspect destination") from None

    def verify_installed(self, name: str, destination: Path, *, owner_uid: int, group_gid: int) -> None:
        try:
            if name not in self._manifest.get("inputs", {}):
                raise InputError("Unknown protected input identifier")
            with checked_directory(destination.parent, owner_uid) as target:
                runtime_traversal(destination, owner_uid, group_gid)
                content, info = read_file(target, destination.name, owner_uid, (0o400,))
            if info["gid"] != group_gid or fingerprint(content) != self._manifest["inputs"][name]["fingerprint"]:
                raise InputError("Installed runtime input does not match approved bytes or group")
        except (OSError, FileSafetyError):
            raise InputError("Installed runtime input is missing, unreadable or unsafe") from None

    def cleanup(self, *, operation_resolved: bool) -> None:
        """Delete only this operation's known files after independently resolved work."""
        if operation_resolved is not True:
            raise InputError("Unresolved apply or recovery still requires private staging")
        try:
            with checked_directory(self.parent, self.owner_uid) as parent:
                with self._directory() as directory:
                    self._verify(directory)
                    for name in self._manifest["inputs"]:
                        os.unlink(name + ".input", dir_fd=directory)
                    os.unlink("manifest.json", dir_fd=directory)
                    os.fsync(directory)
                os.rmdir(self.operation, dir_fd=parent)
                os.fsync(parent)
        except (OSError, FileSafetyError):
            raise InputError("Staging cleanup is uncertain; inspect only this operation's files") from None
