"""Private stopped-writer backups and isolated restore for the deployment runner.

No live restore or service control is exposed. The caller must establish and
maintain writer quiescence, select all recovery assets, and keep the manifest
identity separately from the backup. A reservation alone does not stop writers.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Callable, Generator

from tool.deployment_files import FileSafetyError, checked_directory
from tool.deployment_inputs import attributes, write_file
from tool.deployment_state import StateError, identity, is_list, is_mapping, operation_id


MAX_ENTRIES = 10000
MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
CHUNK = 1024 * 1024
SHA256 = re.compile(r"[a-f0-9]{64}")


class BackupError(ValueError):
    """Backup or isolated recovery is incomplete; retain all evidence."""


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise BackupError("Invalid backup member path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise BackupError("Unsafe backup member path")
    identity(path.parts[0])
    return value


def source_info(info: os.stat_result, owner: int) -> None:
    if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or info.st_uid not in (0, owner) or info.st_mode & 0o7000
            or info.st_mode & 0o022 or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
        raise BackupError("Backup source has unsupported type, ownership, permissions or links")


def stable(before: os.stat_result, after: os.stat_result) -> None:
    if attributes(before) != attributes(after):
        raise BackupError("Backup source changed; re-establish writer quiescence and approval")


def extended_metadata(fd: int) -> None:
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        raise BackupError("Backup metadata inspection requires Linux extended-attribute support")
    if listxattr(fd) or getattr(os.fstat(fd), "st_flags", 0):
        raise BackupError("Extended attributes, ACLs or filesystem flags require a fidelity-preserving backup method")


def quiescent(guard: Callable[[], None]) -> None:
    if guard() is not None:
        raise BackupError("Writer-quiescence guard must raise on failure, not return a status value")


def copy_stream(source: int, target: int | None, expected_size: int) -> str:
    if not 0 <= expected_size <= MAX_BYTES:
        raise BackupError("Backup member exceeds the byte limit")
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(os.dup(source), "rb") as reader:
        while True:
            block = reader.read(min(CHUNK, expected_size - copied + 1))
            if not block:
                break
            copied += len(block)
            if copied > expected_size:
                raise BackupError("Backup member changed size or exceeds its recorded length")
            digest.update(block)
            if target is not None:
                view = memoryview(block)
                while view:
                    written = os.write(target, view)
                    if written <= 0:
                        raise BackupError("Backup write did not make progress")
                    view = view[written:]
    if copied != expected_size:
        raise BackupError("Backup member is truncated or changed size")
    return digest.hexdigest()


def manifest_value(value: Any, operation: str) -> dict[str, Any]:
    if not is_mapping(value) or set(value) != {
            "schema_version", "kind", "operation", "created_at", "method", "sources", "entries", "total_bytes"}:
        raise BackupError("Invalid backup manifest schema")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "deployment-file-backup" or value["operation"] != operation
            or value["method"] != "stopped-writers"):
        raise BackupError("Backup manifest identity or capture method mismatch")
    try:
        if not isinstance(value["created_at"], str) or datetime.fromisoformat(value["created_at"]).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise BackupError("Invalid backup capture timestamp") from None
    sources: Any = value["sources"]
    entries: Any = value["entries"]
    if not is_mapping(sources) or not sources or len(sources) > 64:
        raise BackupError("Invalid backup source set")
    for name, source in sources.items():
        identity(name)
        if (not isinstance(source, str) or not source.startswith("/") or "\x00" in source
                or ".." in Path(source).parts):
            raise BackupError("Invalid private backup source identity")
    if not is_list(entries) or not entries or len(entries) > MAX_ENTRIES:
        raise BackupError("Invalid backup entry count")
    seen: dict[str, str] = {}
    blobs: set[str] = set()
    total = 0
    for entry in entries:
        if not is_mapping(entry):
            raise BackupError("Invalid backup entry")
        kind = entry.get("type")
        common = {"path", "type", "uid", "gid", "mode", "mtime_ns"}
        if kind == "file":
            expected = common | {"blob", "size", "sha256"}
        elif kind == "directory":
            expected = common
        else:
            raise BackupError("Unsupported backup entry type")
        if set(entry) != expected:
            raise BackupError("Invalid backup entry fields")
        path = relative_path(entry["path"])
        parent = str(PurePosixPath(path).parent)
        if len(PurePosixPath(path).parts) > 64:
            raise BackupError("Backup entry exceeds the depth limit")
        if path in seen or PurePosixPath(path).parts[0] not in sources:
            raise BackupError("Duplicate or unknown backup entry identity")
        if parent != "." and seen.get(parent) != "directory":
            raise BackupError("Backup entry parent is missing or is not a directory")
        for key in ("uid", "gid", "mode", "mtime_ns"):
            if type(entry[key]) is not int or entry[key] < 0:
                raise BackupError("Invalid backup filesystem metadata")
        if entry["uid"] >= 2**32 - 1 or entry["gid"] >= 2**32 - 1 or entry["mtime_ns"] >= 2**63:
            raise BackupError("Backup filesystem metadata exceeds supported numeric bounds")
        if entry["mode"] > 0o777 or entry["mode"] & 0o022:
            raise BackupError("Unsafe backed-up permissions require explicit migration")
        seen[path] = kind
        if kind == "file":
            blob = entry["blob"]
            if (not isinstance(blob, str) or not re.fullmatch(r"[0-9]{6}\.blob", blob)
                    or blob in blobs or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_BYTES
                    or not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"])):
                raise BackupError("Invalid backup content descriptor")
            blobs.add(blob)
            total += entry["size"]
    if (not set(sources).issubset(seen) or type(value["total_bytes"]) is not int
            or total != value["total_bytes"] or total > MAX_BYTES):
        raise BackupError("Backup source coverage or total byte count mismatch")
    return value


class FileBackup:
    """Verified byte-for-byte trees; source metadata lives only in a private manifest."""

    def __init__(self, parent: Path, operation: str, manifest_sha256: str, *, owner_uid: int | None = None):
        operation_id(operation)
        if type(manifest_sha256) is not str or not SHA256.fullmatch(manifest_sha256):
            raise BackupError("A separately retained backup manifest identity is required")
        self.path = parent / operation
        self.operation = operation
        self.manifest_sha256 = manifest_sha256
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid

    @classmethod
    def capture(cls, parent: Path, operation: str, sources: dict[str, Path], *,
                verify_quiescent: Callable[[], None], owner_uid: int | None = None) -> "FileBackup":
        """Snapshot only after the caller has stopped all relevant writers.

        The guard must raise when quiescence is uncertain. It is called before,
        during and after capture; it is not replaced by content comparisons.
        No automatic retry or cleanup is performed on interruption.
        """
        owner = os.getuid() if owner_uid is None else owner_uid
        try:
            operation_id(operation)
            if not sources or len(sources) > 64:
                raise BackupError("Select between one and 64 explicit backup roots")
            for name, path in sources.items():
                identity(name)
                if not path.is_absolute() or path == Path("/") or ".." in path.parts:
                    raise BackupError("Backup sources must be explicit absolute paths without traversal")
                if path == parent or path in parent.parents or parent in path.parents:
                    raise BackupError("Backup storage must not overlap any source tree")
            roots = list(sources.values())
            for index, first in enumerate(roots):
                if any(first == second or first in second.parents or second in first.parents
                       for second in roots[index + 1:]):
                    raise BackupError("Backup source trees must not overlap")
            quiescent(verify_quiescent)
            with checked_directory(parent, owner) as directory:
                os.mkdir(operation, 0o700, dir_fd=directory)
                os.fsync(directory)
            entries: list[dict[str, Any]] = []
            observed: dict[str, dict[str, int]] = {}
            total = [0]
            with checked_directory(parent / operation, owner) as backup:
                for name, path in sources.items():
                    with checked_directory(path.parent, owner, private=False) as source:
                        cls._capture_entry(source, path.name, name, backup, owner,
                                           entries, observed, total, verify_quiescent)
                cls._verify_sources(sources, entries, observed, owner, verify_quiescent)
                quiescent(verify_quiescent)
                manifest: dict[str, Any] = {
                    "schema_version": 1, "kind": "deployment-file-backup", "operation": operation,
                    "created_at": datetime.now(timezone.utc).isoformat(), "method": "stopped-writers",
                    "sources": {name: str(path) for name, path in sources.items()},
                    "entries": entries, "total_bytes": total[0],
                }
                manifest_value(manifest, operation)
                raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
                if len(raw) > MAX_MANIFEST:
                    raise BackupError("Backup manifest exceeds the size limit")
                write_file(backup, "manifest.json", raw, owner=owner, group=os.getgid(), mode=0o400)
            result = cls(parent, operation, hashlib.sha256(raw).hexdigest(), owner_uid=owner)
            result.verify()
            quiescent(verify_quiescent)
            return result
        except (OSError, FileSafetyError, StateError):
            raise BackupError("Backup capture failed; retain partial evidence and investigate before retrying") from None

    @classmethod
    def _capture_entry(cls, parent: int, name: str, relative: str, backup: int, owner: int,
                       entries: list[dict[str, Any]], observed: dict[str, dict[str, int]],
                       total: list[int], guard: Callable[[], None]) -> None:
        quiescent(guard)
        if len(entries) >= MAX_ENTRIES or len(PurePosixPath(relative).parts) > 64:
            raise BackupError("Backup tree exceeds entry or depth limits")
        relative_path(relative)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        source_info(before, owner)
        observed[relative] = attributes(before)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(before.st_mode):
            flags |= os.O_DIRECTORY
        fd = os.open(name, flags, dir_fd=parent)
        try:
            stable(before, os.fstat(fd))
            extended_metadata(fd)
            entry: dict[str, Any] = {
                "path": relative, "type": "directory" if stat.S_ISDIR(before.st_mode) else "file",
                "uid": before.st_uid, "gid": before.st_gid, "mode": stat.S_IMODE(before.st_mode),
                "mtime_ns": before.st_mtime_ns,
            }
            if stat.S_ISDIR(before.st_mode):
                entries.append(entry)
                children = sorted(os.listdir(fd))
                for child in children:
                    cls._capture_entry(fd, child, relative + "/" + child, backup, owner,
                                       entries, observed, total, guard)
                if sorted(os.listdir(fd)) != children:
                    raise BackupError("Backup tree membership changed during capture")
            else:
                total[0] += before.st_size
                if total[0] > MAX_BYTES:
                    raise BackupError("Backup exceeds the total byte limit")
                blob = f"{len(entries):06d}.blob"
                target = os.open(blob, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=backup)
                try:
                    digest = copy_stream(fd, target, before.st_size)
                    os.fchmod(target, 0o400)
                    os.fsync(target)
                finally:
                    os.close(target)
                os.fsync(backup)
                entry.update(blob=blob, size=before.st_size, sha256=digest)
                entries.append(entry)
            stable(before, os.fstat(fd))
            stable(before, os.stat(name, dir_fd=parent, follow_symlinks=False))
            extended_metadata(fd)
            quiescent(guard)
        finally:
            os.close(fd)

    @staticmethod
    def _verify_sources(sources: dict[str, Path], entries: list[dict[str, Any]],
                        observed: dict[str, dict[str, int]], owner: int, guard: Callable[[], None]) -> None:
        children: dict[str, set[str]] = {}
        for entry in entries:
            path = PurePosixPath(entry["path"])
            children.setdefault(str(path.parent), set()).add(path.name)
        for entry in entries:
            quiescent(guard)
            parts = PurePosixPath(entry["path"]).parts
            path = sources[parts[0]].joinpath(*parts[1:])
            with checked_directory(path.parent, owner, private=False) as parent:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if attributes(info) != observed[entry["path"]]:
                        raise BackupError("Backup source changed after it was copied")
                    extended_metadata(fd)
                    if entry["type"] == "file":
                        if copy_stream(fd, None, entry["size"]) != entry["sha256"]:
                            raise BackupError("Backup source content changed after it was copied")
                    else:
                        if set(os.listdir(fd)) != children.get(entry["path"], set()):
                            raise BackupError("Backup tree membership changed after capture")
                    stable(info, os.fstat(fd))
                    stable(info, os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                finally:
                    os.close(fd)

    @contextmanager
    def _verified(self) -> Generator[tuple[int, dict[str, Any]], None, None]:
        try:
            with checked_directory(self.path, self.owner_uid) as backup:
                extended_metadata(backup)
                raw = self._manifest_bytes(backup)
                if hashlib.sha256(raw).hexdigest() != self.manifest_sha256:
                    raise BackupError("Backup manifest does not match the separately retained identity")
                try:
                    manifest = manifest_value(json.loads(raw), self.operation)
                except (ValueError, UnicodeError, RecursionError) as error:
                    if isinstance(error, (BackupError, StateError)):
                        raise
                    raise BackupError("Backup manifest is malformed") from None
                expected = {"manifest.json"} | {
                    entry["blob"] for entry in manifest["entries"] if entry["type"] == "file"
                }
                if set(os.listdir(backup)) != expected:
                    raise BackupError("Backup has missing, unexpected or partially written files")
                for entry in manifest["entries"]:
                    if entry["type"] == "file":
                        with self._blob(backup, entry) as fd:
                            if copy_stream(fd, None, entry["size"]) != entry["sha256"]:
                                raise BackupError("Backup content checksum mismatch")
                yield backup, manifest
        except (OSError, FileSafetyError, StateError):
            raise BackupError("Backup verification or isolated recovery failed; retain evidence") from None

    def _manifest_bytes(self, backup: int) -> bytes:
        fd = os.open("manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=backup)
        with os.fdopen(fd, "rb") as source:
            self._private_file(os.fstat(source.fileno()))
            extended_metadata(source.fileno())
            raw = source.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST:
                raise BackupError("Backup manifest exceeds the size limit")
            return raw

    def _private_file(self, info: os.stat_result) -> None:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1):
            raise BackupError("Backup payload requires a private single-link regular file")

    @contextmanager
    def _blob(self, directory: int, entry: dict[str, Any]) -> Generator[int, None, None]:
        fd = os.open(entry["blob"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._private_file(os.fstat(fd))
            extended_metadata(fd)
            if os.fstat(fd).st_size != entry["size"]:
                raise BackupError("Backup payload size mismatch")
            yield fd
        finally:
            os.close(fd)

    def verify(self) -> dict[str, Any]:
        """Return a redacted inventory; do not emit source paths or content hashes."""
        with self._verified() as (_, manifest):
            return {"kind": "verified-file-backup", "operation": self.operation,
                    "entries": len(manifest["entries"]), "total_bytes": manifest["total_bytes"],
                    "method": manifest["method"], "live_restore_supported": False}

    def restore_new(self, destination: Path, *, verify_quiescent: Callable[[], None]) -> dict[str, Any]:
        """Restore into a new isolated tree, never onto original paths or state.

        Ownership restoration requires suitable privilege; no ownership fallback
        is permitted. The caller must validate code/data compatibility and service
        acceptance separately before any approved live cutover.
        """
        with self._verified() as (backup, manifest):
            if destination == self.path.parent or self.path.parent in destination.parents:
                raise BackupError("Restore destination must be outside backup storage")
            for source in manifest["sources"].values():
                path = Path(source)
                if destination == path or destination in path.parents or path in destination.parents:
                    raise BackupError("Isolated restore must not overlap original source paths")
            quiescent(verify_quiescent)
            with checked_directory(destination.parent, self.owner_uid) as parent:
                extended_metadata(parent)
                os.mkdir(destination.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            with checked_directory(destination, self.owner_uid) as target:
                directories: list[dict[str, Any]] = []
                for entry in manifest["entries"]:
                    quiescent(verify_quiescent)
                    parts = PurePosixPath(entry["path"]).parts
                    with self._restore_parent(target, parts[:-1]) as directory:
                        if entry["type"] == "directory":
                            os.mkdir(parts[-1], 0o700, dir_fd=directory)
                            directories.append(entry)
                            os.fsync(directory)
                        else:
                            fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                            try:
                                with self._blob(backup, entry) as source:
                                    digest = copy_stream(source, fd, entry["size"])
                                if digest != entry["sha256"]:
                                    raise BackupError("Backup content changed during restore")
                                self._metadata(fd, entry)
                                os.lseek(fd, 0, os.SEEK_SET)
                                if copy_stream(fd, None, entry["size"]) != entry["sha256"]:
                                    raise BackupError("Restored file failed independent byte verification")
                            finally:
                                os.close(fd)
                            os.fsync(directory)
                # Set directory permissions only after their contents are complete.
                for entry in reversed(directories):
                    with self._restore_parent(target, PurePosixPath(entry["path"]).parts) as directory:
                        self._metadata(directory, entry)
                os.fsync(target)
            quiescent(verify_quiescent)
            return {"kind": "isolated-file-restore", "operation": self.operation,
                    "entries": len(manifest["entries"]), "live_cutover": False,
                    "service_acceptance": "not-tested", "reservation_cleared": False}

    @staticmethod
    @contextmanager
    def _restore_parent(root: int, parts: tuple[str, ...]) -> Generator[int, None, None]:
        fd = os.dup(root)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _metadata(fd: int, entry: dict[str, Any]) -> None:
        extended_metadata(fd)
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid) != (entry["uid"], entry["gid"]):
            os.fchown(fd, entry["uid"], entry["gid"])
        os.fchmod(fd, entry["mode"])
        os.utime(fd, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        os.fsync(fd)
        restored = os.fstat(fd)
        if (restored.st_uid != entry["uid"] or restored.st_gid != entry["gid"]
                or stat.S_IMODE(restored.st_mode) != entry["mode"] or restored.st_mtime_ns != entry["mtime_ns"]):
            raise BackupError("Restored file metadata does not match the backup")
