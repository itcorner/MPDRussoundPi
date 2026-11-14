"""Private persistent state primitives for the deployment runner.

These primitives do not run deployment commands or establish that remote work
has finished. This module exposes only status; reviewed deployment/recovery
actions are owned by deployment_operator, never a generic unlock command.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Generator
from uuid import UUID

from tool.deployment_images import DIGEST, MAX_METADATA, SelectionError, validate_selection
from tool.deployment_files import FileSafetyError, checked_directory, directory_info, is_list, is_mapping
from tool.deployment_local import LOCAL_CANDIDATE, validate_candidate, validate_loaded


STATE_ROOT = Path("/var/lib/mpdrussoundpi-deploy")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
PHASES = ("prepared", "inputs-verified", "backup-complete", "apply-complete", "checks-complete")


class StateError(ValueError):
    """State is blocked, invalid or uncertain; manual investigation is required."""


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
    if value.get("kind") == LOCAL_CANDIDATE:
        try:
            validate_candidate(value)
        except SelectionError:
            raise StateError("Invalid local development image candidate") from None
        return
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
    local = (is_mapping(value) and is_mapping(value.get("candidate"))
             and value["candidate"].get("kind") == LOCAL_CANDIDATE)
    if local:
        fields.add("loaded_images")
    if not is_mapping(value) or set(value) != fields:
        raise StateError("Invalid pending deployment record")
    operation_id(value["operation"])
    if value["phase"] not in PHASES:
        raise StateError("Invalid pending deployment phase")
    candidate(value["candidate"])
    if local:
        if value["loaded_images"] is not None:
            try:
                validate_loaded(value["loaded_images"], value["candidate"])
            except SelectionError:
                raise StateError("Invalid loaded local image identities") from None
            if value["phase"] in ("prepared", "inputs-verified"):
                raise StateError("Local images cannot be loaded before the backup boundary")
        elif value["phase"] in ("apply-complete", "checks-complete"):
            raise StateError("Local deployment requires verified loaded images")
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
            entries = set(os.listdir(reservation))
            if not entries <= {"owner.json", "image-load.json", "image-transfer.json"}:
                raise StateError("Interrupted or repeated finalization requires investigation")
            for name in ("image-load.json", "image-transfer.json"):
                if name in entries:
                    marker = self._image_marker(reservation, operation, name)
                    pending = self._record(directory, deployment)["pending"]
                    if (pending is None or pending["operation"] != operation
                            or (name == "image-transfer.json"
                                and pending["candidate"].get("kind") != LOCAL_CANDIDATE)
                            or marker["intent_digest"] != pending["intent_digest"]):
                        raise StateError("Image mutation marker does not match the pending operation")
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
        if is_mapping(selection) and selection.get("kind") == LOCAL_CANDIDATE:
            pending["loaded_images"] = None
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
                    pending_record(pending)
                    self._write(directory, deployment + ".json", record, replace=True)
        except OSError:
            raise StateError("Deployment phase persistence is uncertain; stop and inspect state") from None

    def _image_marker(self, reservation: int, operation: str, name: str = "image-load.json") -> dict[str, Any]:
        marker = self._read(reservation, name)
        fields = {"operation", "intent_digest"}
        if name == "image-transfer.json":
            fields.add("verified")
        if (set(marker) != fields or marker["operation"] != operation
                or not isinstance(marker["intent_digest"], str)
                or not DIGEST.fullmatch(marker["intent_digest"])
                or (name == "image-transfer.json" and type(marker["verified"]) is not bool)):
            raise StateError("Invalid one-shot image mutation intent")
        return marker

    def start_image_transfer(self, deployment: str, operation: str, selection: dict[str, Any],
                             intent_digest: str) -> None:
        """Reserve one incoming artifact stream before receiving any bundle files."""
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    pending = self._record(directory, deployment)["pending"]
                    if (pending is None or pending["operation"] != operation
                            or pending["phase"] not in ("prepared", "inputs-verified")
                            or pending["candidate"] != selection or pending["intent_digest"] != intent_digest
                            or selection.get("kind") != LOCAL_CANDIDATE):
                        raise StateError("Transfer requires the matching reserved local operation before backup")
                    self._write(reservation, "image-transfer.json",
                                {"operation": operation, "intent_digest": intent_digest, "verified": False})
        except OSError:
            raise StateError("Image-transfer intent exists or persistence is uncertain; do not retry") from None

    def complete_image_transfer(self, deployment: str, operation: str, selection: dict[str, Any],
                                intent_digest: str) -> None:
        """Record receiver verification without advancing service phases."""
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    pending = self._record(directory, deployment)["pending"]
                    marker = self._image_marker(reservation, operation, "image-transfer.json")
                    if (pending is None or pending["operation"] != operation
                            or pending["phase"] not in ("prepared", "inputs-verified")
                            or pending["candidate"] != selection or pending["intent_digest"] != intent_digest
                            or marker["intent_digest"] != intent_digest or marker["verified"]):
                        raise StateError("Transfer completion requires the original unverified reserved operation")
                    marker["verified"] = True
                    self._write(reservation, "image-transfer.json", marker, replace=True)
        except OSError:
            raise StateError("Image-transfer verification persistence is uncertain; do not retry") from None

    def _load_record(self, directory: int, deployment: str, operation: str,
                     selection: dict[str, Any], intent_digest: str) -> dict[str, Any]:
        record = self._record(directory, deployment)
        pending = record["pending"]
        if (pending is None or pending["operation"] != operation or pending["phase"] != "backup-complete"
                or pending["intent_digest"] != intent_digest or pending["candidate"] != selection
                or selection.get("kind") != LOCAL_CANDIDATE or pending.get("loaded_images") is not None):
            raise StateError("Image load requires the matching approved local candidate at the backup boundary")
        return record

    def start_image_load(self, deployment: str, operation: str, selection: dict[str, Any],
                         intent_digest: str) -> None:
        """One-shot internal mutation boundary; process loss never permits a retry."""
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    self._load_record(directory, deployment, operation, selection, intent_digest)
                    if ("image-transfer.json" in os.listdir(reservation)
                            and not self._image_marker(reservation, operation, "image-transfer.json")["verified"]):
                        raise StateError("An interrupted artifact transfer blocks image loading")
                    self._write(reservation, "image-load.json",
                                {"operation": operation, "intent_digest": intent_digest})
        except OSError:
            raise StateError("Image-load intent exists or persistence is uncertain; do not retry") from None

    def complete_image_load(self, deployment: str, operation: str, selection: dict[str, Any],
                            intent_digest: str, images: dict[str, str]) -> None:
        """Record independently inspected target identities, not service completion."""
        try:
            validate_loaded(images, selection)
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    record = self._load_record(directory, deployment, operation, selection, intent_digest)
                    if self._image_marker(reservation, operation)["intent_digest"] != intent_digest:
                        raise StateError("Image-load approval identity mismatch")
                    record["pending"]["loaded_images"] = images
                    self._write(directory, deployment + ".json", record, replace=True)
        except SelectionError:
            raise StateError("Invalid loaded image identities") from None
        except OSError:
            raise StateError("Loaded-image persistence is uncertain; retain reservation and inspect") from None

    def start_registry_pull(self, deployment: str, operation: str, selection: dict[str, Any],
                            intent_digest: str) -> None:
        """One approved pinned pull attempt; uncertain pulls are never retried."""
        candidate(selection)
        if selection["kind"] == LOCAL_CANDIDATE:
            raise StateError("Registry pull does not accept a local candidate")
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    pending = self._record(directory, deployment)["pending"]
                    if (pending is None or pending["operation"] != operation
                            or pending["phase"] != "backup-complete"
                            or pending["candidate"] != selection or pending["intent_digest"] != intent_digest):
                        raise StateError("Pull requires the matching approved candidate after backup")
                    self._write(reservation, "image-load.json",
                                {"operation": operation, "intent_digest": intent_digest})
        except OSError:
            raise StateError("Registry pull intent exists or is uncertain; do not retry") from None

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
                    if (pending["candidate"].get("kind") == LOCAL_CANDIDATE
                            and self._image_marker(reservation, operation)["intent_digest"] != pending["intent_digest"]):
                        raise StateError("Finalization requires the original image-load intent")
                    completion_record(completion, pending)
                    self._write(reservation, "finalizing.json", completion)
                    record["committed"] = {"selection": pending, "completion": completion}
                    record["pending"] = None
                    self._write(directory, deployment + ".json", record, replace=True)
                    os.unlink("finalizing.json", dir_fd=reservation)
                    if "image-load.json" in os.listdir(reservation):
                        os.unlink("image-load.json", dir_fd=reservation)
                    if "image-transfer.json" in os.listdir(reservation):
                        os.unlink("image-transfer.json", dir_fd=reservation)
                    os.unlink("owner.json", dir_fd=reservation)
                    os.fsync(reservation)
                os.rmdir("reservation", dir_fd=directory)
                os.fsync(directory)
        except OSError:
            raise StateError("Finalization outcome is uncertain; reservation may be absent; do not retry") from None

    def resolve_recovery(self, deployment: str, operation: str, receipt: dict[str, Any]) -> None:
        """Internal boundary after approved restoration and independently checked runtime.

        The caller must durably retain the original record and recovery evidence.
        This is not an unlock API and is deliberately absent from the state CLI.
        """
        expected = {"containers": "passed", "http": "passed", "backend": "passed", "playback": "passed"}
        absent = {"containers-absent": "passed", "prepared-data-restored": "passed"}
        if (set(receipt) != {"operation", "review_digest", "required"}
                or receipt["operation"] != operation or receipt["required"] not in (expected, absent)
                or not isinstance(receipt["review_digest"], str)
                or not DIGEST.fullmatch(receipt["review_digest"])):
            raise StateError("Recovery requires matching approval and all independent runtime checks")
        try:
            with self._open_directory(self.root) as directory:
                with self._owned(directory, deployment, operation) as reservation:
                    record = self._record(directory, deployment)
                    if record["pending"] is None or record["pending"]["operation"] != operation:
                        raise StateError("Recovery does not own the original pending operation")
                    self._write(reservation, "finalizing.json", receipt)
                    record["pending"] = None
                    if record["committed"] is None:
                        os.unlink(deployment + ".json", dir_fd=directory)
                        os.fsync(directory)
                    else:
                        self._write(directory, deployment + ".json", record, replace=True)
                    os.unlink("finalizing.json", dir_fd=reservation)
                    for name in ("image-load.json", "image-transfer.json", "owner.json"):
                        if name in os.listdir(reservation):
                            os.unlink(name, dir_fd=reservation)
                    os.fsync(reservation)
                os.rmdir("reservation", dir_fd=directory)
                os.fsync(directory)
        except OSError:
            raise StateError("Recovery finalization is uncertain; do not retry or remove state") from None

    def status(self) -> dict[str, Any]:
        """Return non-secret state, never assert that services or daemons are idle."""
        try:
            with self._open_directory(self.root) as directory:
                records = self._scan(directory)
                reserved = "reservation" in os.listdir(directory)
                owner = None
                image_load_started = False
                image_transfer_started = False
                image_transfer_verified = False
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
                        if "image-load.json" in os.listdir(fd):
                            self._image_marker(fd, owner["operation"])
                            image_load_started = True
                        if "image-transfer.json" in os.listdir(fd):
                            marker = self._image_marker(fd, owner["operation"], "image-transfer.json")
                            image_transfer_started = True
                            image_transfer_verified = marker["verified"]
                    finally:
                        os.close(fd)
                unresolved = any(record["pending"] is not None for record in records.values())
                return {"schema_version": 1, "blocked": reserved or unresolved, "reservation": owner,
                        "deployments": records, "runtime_verified": False,
                        "image_load_started": image_load_started,
                        "image_transfer_started": image_transfer_started,
                        "image_transfer_verified": image_transfer_verified}
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
