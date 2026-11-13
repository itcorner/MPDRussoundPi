"""Private persistent state primitives for the future deployment runner.

These primitives do not run deployment commands or establish that remote work
has finished. Only the status command is exposed to operators until the
completion, snapshot and recovery runners are implemented.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Generator, TypeGuard
from uuid import UUID

from tool.deployment_images import DIGEST, MAX_METADATA, SelectionError, validate_selection
from tool.deployment_files import FileSafetyError, checked_directory, directory_info


STATE_ROOT = Path("/var/lib/mpdrussoundpi-deploy")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
PHASES = ("prepared", "inputs-verified", "backup-complete", "apply-complete", "checks-complete")


class StateError(ValueError):
    """State is blocked, invalid or uncertain; manual investigation is required."""


def is_mapping(value: object) -> TypeGuard[dict[Any, Any]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def identity(value: Any) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StateError("Invalid deployment or completion item identifier")


def operation_id(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise StateError("Operation identity must be a canonical UUID") from None


def candidate(value: Any) -> None:
    if not is_mapping(value):
        raise StateError("Invalid development image candidate")
    gateway: Any = value.get("gateway")
    platform: Any = value.get("platform")
    if not isinstance(gateway, str) or not isinstance(platform, str):
        raise StateError("Image candidate gateway and platform must be strings")
    try:
        validate_selection(value, gateway, platform)
    except SelectionError:
        raise StateError("Invalid development image candidate") from None


def requirements(value: Any) -> None:
    if not is_list(value) or not value or len(value) != len(set_identities(value)):
        raise StateError("Expected completion items must be a nonempty unique list")


def set_identities(value: list[Any]) -> set[str]:
    for item in value:
        identity(item)
    return set(value)


def pending_record(value: Any) -> None:
    fields = {"operation", "phase", "candidate", "intent_digest", "required", "backup"}
    if not is_mapping(value) or set(value) != fields:
        raise StateError("Invalid pending deployment record")
    operation_id(value["operation"])
    if value["phase"] not in PHASES:
        raise StateError("Invalid pending deployment phase")
    candidate(value["candidate"])
    if not isinstance(value["intent_digest"], str) or not DIGEST.fullmatch(value["intent_digest"]):
        raise StateError("Invalid approved non-secret intent digest")
    requirements(value["required"])
    if value["backup"] is not None:
        operation_id(value["backup"])


def completion_record(value: Any, pending: dict[str, Any]) -> None:
    fields = {"operation", "intent_digest", "invocation", "required"}
    if not is_mapping(value) or set(value) != fields:
        raise StateError("Missing or invalid verified completion receipt")
    if (value["operation"] != pending["operation"]
            or value["intent_digest"] != pending["intent_digest"]
            or value["invocation"] != "verified-complete"):
        raise StateError("Completion identity or invocation outcome mismatch")
    outcomes: Any = value["required"]
    if (not is_mapping(outcomes) or set(outcomes) != set(pending["required"])
            or any(outcome != "passed" for outcome in outcomes.values())):
        raise StateError("Every expected completion item must have one verified passing outcome")


class StateStore:
    """Short durable transitions; a reservation is never released by process exit.

    The runner must serialize transitions for an operation. Completion receipts
    are internal inputs, not authorization tokens or evidence by themselves.
    """

    def __init__(self, root: Path = STATE_ROOT, *, owner_uid: int = 0):
        self.root = root
        self.owner_uid = owner_uid

    def _directory(self, info: os.stat_result, *, private: bool) -> None:
        try:
            directory_info(info, self.owner_uid, private=private)
        except FileSafetyError as error:
            raise StateError(str(error)) from None

    @contextmanager
    def _open_directory(self, path: Path, *, private: bool = True) -> Generator[int, None, None]:
        try:
            with checked_directory(path, self.owner_uid, private=private) as fd:
                yield fd
        except FileSafetyError as error:
            raise StateError(str(error)) from None

    def initialize(self) -> None:
        """Provision only the state directory, never repair unsafe existing state."""
        try:
            with self._open_directory(self.root.parent, private=False) as parent:
                try:
                    os.mkdir(self.root.name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                os.fsync(parent)
            with self._open_directory(self.root):
                pass
        except OSError:
            raise StateError("Cannot initialize durable deployment state; inspect before retrying") from None

    def _read(self, directory: int, name: str) -> dict[str, Any]:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != self.owner_uid or stat.S_IMODE(info.st_mode) != 0o600):
                raise StateError("Deployment record must be a private single-link regular file")
            raw = source.read(MAX_METADATA + 1)
        if len(raw) > MAX_METADATA:
            raise StateError("Deployment record exceeds the size limit")
        try:
            value: dict[str, Any] = json.loads(raw)
        except (ValueError, UnicodeError):
            raise StateError("Deployment record is malformed; manual investigation required") from None
        if type(value) is not dict:
            raise StateError("Deployment record must be an object")
        return value

    def _write(self, directory: int, name: str, value: dict[str, Any], *, replace: bool = False) -> None:
        raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        if len(raw) > MAX_METADATA:
            raise StateError("Deployment record exceeds the size limit")
        # A leftover staging file blocks retries after an interrupted write.
        staging = name + ".new" if replace else name
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        if replace:
            os.replace(staging, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)

    def _record(self, directory: int, deployment: str) -> dict[str, Any]:
        value = self._read(directory, deployment + ".json")
        if (set(value) != {"schema_version", "deployment", "committed", "pending"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["deployment"] != deployment):
            raise StateError("Deployment record schema or identity mismatch")
        if value["pending"] is not None:
            pending_record(value["pending"])
        committed: dict[str, Any] | None = value["committed"]
        if committed is not None:
            if type(committed) is not dict or set(committed) != {"selection", "completion"}:
                raise StateError("Invalid committed deployment record")
            pending_record(committed["selection"])
            if committed["selection"]["phase"] != "checks-complete":
                raise StateError("Committed deployment did not complete its checks")
            completion_record(committed["completion"], committed["selection"])
        if committed is None and value["pending"] is None:
            raise StateError("Empty deployment record requires investigation")
        return value

    def _scan(self, directory: int) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for name in os.listdir(directory):
            if name == "reservation":
                continue
            if not name.endswith(".json"):
                raise StateError("Unexpected deployment state entry; investigate partial writes")
            deployment = name[:-5]
            identity(deployment)
            records[deployment] = self._record(directory, deployment)
        return records

    @contextmanager
    def _owned(self, directory: int, deployment: str, operation: str) -> Generator[int, None, None]:
        identity(deployment)
        operation_id(operation)
        reservation = os.open("reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
        try:
            self._directory(os.fstat(reservation), private=True)
            owner = self._read(reservation, "owner.json")
            if (type(owner.get("schema_version")) is not int
                    or owner != {"schema_version": 1, "deployment": deployment, "operation": operation}):
                raise StateError("Reservation identity mismatch")
            if os.listdir(reservation) != ["owner.json"]:
                raise StateError("Interrupted or repeated finalization requires investigation")
            yield reservation
        finally:
            os.close(reservation)

    def begin(self, deployment: str, operation: str, selection: dict[str, Any],
              intent_digest: str, required: list[str]) -> None:
        identity(deployment)
        operation_id(operation)
        pending: dict[str, Any] = {
            "operation": operation, "phase": "prepared", "candidate": selection,
            "intent_digest": intent_digest, "required": required, "backup": None,
        }
        pending_record(pending)
        try:
            with self._open_directory(self.root) as directory:
                try:
                    os.mkdir("reservation", 0o700, dir_fd=directory)
                except FileExistsError:
                    raise StateError("Host has an existing reservation; no automatic retry or stale cleanup") from None
                os.fsync(directory)
                reservation = os.open("reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=directory)
                try:
                    self._write(reservation, "owner.json",
                                {"schema_version": 1, "deployment": deployment, "operation": operation})
                finally:
                    os.close(reservation)
                records = self._scan(directory)
                if any(record["pending"] is not None for record in records.values()):
                    raise StateError("An unresolved pending record blocks all deployment IDs")
                if any(record["committed"] is not None
                       and record["committed"]["selection"]["operation"] == operation
                       for record in records.values()):
                    raise StateError("Operation identity was already committed; do not retry it")
                record = records.get(deployment, {
                    "schema_version": 1, "deployment": deployment, "committed": None, "pending": None,
                })
                record["pending"] = pending
                self._write(directory, deployment + ".json", record, replace=deployment in records)
        except OSError:
            raise StateError("Reservation or pending persistence is uncertain; stop and inspect state") from None

    def advance(self, deployment: str, operation: str, phase: str, *, backup: str | None = None) -> None:
        """Record a completed phase, never run it or infer its success."""
        if phase not in PHASES[1:]:
            raise StateError("Invalid next deployment phase")
        if backup is not None:
            operation_id(backup)
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation):
                    record = self._record(directory, deployment)
                    pending = record["pending"]
                    if (pending is None or pending["operation"] != operation
                            or PHASES.index(phase) != PHASES.index(pending["phase"]) + 1):
                        raise StateError("Pending identity or sequential phase mismatch")
                    if backup is not None and phase != "backup-complete":
                        raise StateError("Backup reference is allowed only at the backup boundary")
                    pending["phase"] = phase
                    if phase == "backup-complete":
                        if record["committed"] is not None and backup is None:
                            raise StateError("An existing deployment requires a backup reference")
                        pending["backup"] = backup
                    self._write(directory, deployment + ".json", record, replace=True)
        except OSError:
            raise StateError("Deployment phase persistence is uncertain; stop and inspect state") from None

    def finalize(self, deployment: str, operation: str, completion: dict[str, Any]) -> None:
        """Internal runner boundary; call once after independently verified completion.

        This must not be exposed as an operator unlock command. The controller
        runner remains responsible for verifying callback and independent checks.
        """
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    record = self._record(directory, deployment)
                    pending = record["pending"]
                    if (pending is None or pending["operation"] != operation
                            or pending["phase"] != "checks-complete"):
                        raise StateError("Finalization requires a matching checked pending operation")
                    completion_record(completion, pending)
                    self._write(reservation, "finalizing.json", completion)
                    record["committed"] = {"selection": pending, "completion": completion}
                    record["pending"] = None
                    self._write(directory, deployment + ".json", record, replace=True)
                    os.unlink("finalizing.json", dir_fd=reservation)
                    os.unlink("owner.json", dir_fd=reservation)
                    os.fsync(reservation)
                os.rmdir("reservation", dir_fd=directory)
                os.fsync(directory)
        except OSError:
            raise StateError("Finalization outcome is uncertain; reservation may be absent; do not retry") from None

    def status(self) -> dict[str, Any]:
        """Return non-secret state, never assert that services or daemons are idle."""
        try:
            with self._open_directory(self.root) as directory:
                records = self._scan(directory)
                reserved = "reservation" in os.listdir(directory)
                owner = None
                if reserved:
                    fd = os.open("reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=directory)
                    try:
                        self._directory(os.fstat(fd), private=True)
                        owner = self._read(fd, "owner.json")
                        if (set(owner) != {"schema_version", "deployment", "operation"}
                                or type(owner["schema_version"]) is not int or owner["schema_version"] != 1):
                            raise StateError("Invalid reservation owner record")
                        identity(owner["deployment"])
                        operation_id(owner["operation"])
                    finally:
                        os.close(fd)
                unresolved = any(record["pending"] is not None for record in records.values())
                return {"schema_version": 1, "blocked": reserved or unresolved, "reservation": owner,
                        "deployments": records, "runtime_verified": False}
        except OSError:
            raise StateError("Cannot inspect deployment state; treat the outcome as unknown") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status",))
    parser.parse_args()
    try:
        print(json.dumps(StateStore().status(), indent=2))
    except StateError as error:
        print(f"Deployment state blocked or unknown: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
