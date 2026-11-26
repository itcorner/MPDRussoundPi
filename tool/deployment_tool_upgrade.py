#!/usr/bin/env python3
"""Guarded atomic switch between reviewed deployment-tool checkouts."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from uuid import UUID, uuid4


TOOLS = Path("/opt/mpdrussoundpi-deploy")
STATE_ROOT = Path("/var/lib/mpdrussoundpi-deploy")
REVISION = re.compile(r"[a-f0-9]{40}")
MAX_OWNER_BYTES = 4096


class UpgradeError(ValueError):
    """The tooling upgrade is unsafe or its outcome is uncertain."""


def _revision(value: str) -> None:
    if not isinstance(value, str) or not REVISION.fullmatch(value):
        raise UpgradeError("Tooling revisions must be full lowercase commit IDs")


def _operation(value: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise UpgradeError("Tooling upgrade operation ID is invalid") from None


def _directory(path: Path, owner_uid: int) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError:
        raise UpgradeError("A required tooling path is missing or inaccessible") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise UpgradeError("Tooling directories must be real, owner-controlled, and not shared-writable")
    return info


def _tools_parent(path: Path, owner_uid: int) -> None:
    if path.parent != TOOLS.parent:
        raise UpgradeError("Tooling checkouts must be direct children of /opt")
    _directory(path.parent, owner_uid)


def _git(path: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise UpgradeError("Cannot verify a tooling checkout; inspect it before retrying") from None
    return result.stdout.strip()


def _checkout(path: Path, expected: str, owner_uid: int) -> None:
    _directory(path, owner_uid)
    actual = _git(path, "rev-parse", "--verify", "HEAD")
    if actual != expected:
        raise UpgradeError("Tooling checkout does not match the explicitly approved revision")
    if _git(path, "status", "--porcelain", "--untracked-files=all"):
        raise UpgradeError("Tooling checkout has local changes; preserve and inspect them")


def _state_status(tools: Path, state_root: Path) -> dict:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tools)
    snippet = (
        "import json, sys; from pathlib import Path; "
        "from tool.deployment_state import StateStore; "
        "print(json.dumps(StateStore(Path(sys.argv[1]), owner_uid=int(sys.argv[2])).status()))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", snippet, str(state_root), str(os.geteuid())],
            cwd=tools,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise UpgradeError("Deployment state is unreadable; retain the tooling lock and investigate") from None
    if (type(status) is not dict or type(status.get("blocked")) is not bool
            or type(status.get("deployments")) is not dict):
        raise UpgradeError("Deployment state has an unexpected schema; retain the tooling lock")
    return status


def _has_pending(status: dict) -> bool:
    return any(type(record) is not dict or record.get("pending") is not None
               for record in status["deployments"].values())


def _state_root_fd(state_root: Path, owner_uid: int) -> int:
    try:
        root_fd = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(root_fd)
    except OSError:
        raise UpgradeError("Deployment state root is missing or unsafe") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        os.close(root_fd)
        raise UpgradeError("Deployment state root must be owner-private")
    return root_fd


def _reservation_owner(state_root: Path, owner_uid: int) -> dict:
    root_fd = _state_root_fd(state_root, owner_uid)
    try:
        reservation_fd = os.open(
            "reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
    except OSError:
        os.close(root_fd)
        raise UpgradeError("Tooling upgrade reservation is absent or unsafe") from None
    try:
        info = os.fstat(reservation_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o700
                or set(os.listdir(reservation_fd)) != {"owner.json"}):
            raise UpgradeError("Tooling reservation contains unexpected state")
        fd = os.open("owner.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=reservation_fd)
        with os.fdopen(fd, "rb") as owner_file:
            owner_info = os.fstat(owner_file.fileno())
            if (not stat.S_ISREG(owner_info.st_mode) or owner_info.st_nlink != 1
                    or owner_info.st_uid != owner_uid or stat.S_IMODE(owner_info.st_mode) != 0o600):
                raise UpgradeError("Tooling reservation owner record is unsafe")
            raw = owner_file.read(MAX_OWNER_BYTES + 1)
        if len(raw) > MAX_OWNER_BYTES:
            raise UpgradeError("Tooling reservation owner record is oversized")
        try:
            owner = json.loads(raw)
        except (ValueError, UnicodeError):
            raise UpgradeError("Tooling reservation owner record is malformed") from None
        if (type(owner) is not dict or set(owner) != {"schema_version", "deployment", "operation"}
                or type(owner["schema_version"]) is not int or owner["schema_version"] != 1
                or owner["deployment"] != "tooling-upgrade"):
            raise UpgradeError("Tooling reservation is not owned by this upgrade workflow")
        _operation(owner["operation"])
        return owner
    finally:
        os.close(reservation_fd)
        os.close(root_fd)


def _verify_lock(tools: Path, state_root: Path, operation: str, owner_uid: int) -> None:
    _operation(operation)
    owner = _reservation_owner(state_root, owner_uid)
    if owner["operation"] != operation:
        raise UpgradeError("Tooling upgrade does not own the host reservation")
    status = _state_status(tools, state_root)
    if (not status["blocked"] or status.get("reservation") != owner or _has_pending(status)):
        raise UpgradeError("Deployment state changed during tooling upgrade; keep the reservation")


def acquire_lock(tools: Path = TOOLS, state_root: Path = STATE_ROOT, *, owner_uid: int = 0) -> str:
    _directory(tools, owner_uid)
    status = _state_status(tools, state_root)
    if status["blocked"] or _has_pending(status):
        raise UpgradeError("An active or unresolved deployment blocks tooling upgrade")
    root_fd = _state_root_fd(state_root, owner_uid)
    operation = str(uuid4())
    try:
        try:
            os.mkdir("reservation", 0o700, dir_fd=root_fd)
        except FileExistsError:
            raise UpgradeError("Another deployment or tooling operation acquired the host reservation") from None
        os.fsync(root_fd)
        reservation_fd = os.open(
            "reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            owner = {"schema_version": 1, "deployment": "tooling-upgrade", "operation": operation}
            fd = os.open(
                "owner.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=reservation_fd,
            )
            with os.fdopen(fd, "wb") as target:
                target.write((json.dumps(owner, sort_keys=True) + "\n").encode())
                target.flush()
                os.fsync(target.fileno())
            os.fsync(reservation_fd)
        finally:
            os.close(reservation_fd)
        os.fsync(root_fd)
        return operation
    except OSError:
        raise UpgradeError("Reservation creation is uncertain; inspect state before retrying") from None
    finally:
        os.close(root_fd)


def _stage_path(path_revision: str, *, owner_uid: int) -> Path:
    _revision(path_revision)
    stage = TOOLS.with_name(f"{TOOLS.name}.upgrade-{path_revision}")
    _tools_parent(stage, owner_uid)
    return stage


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise UpgradeError("Tooling switch durability is uncertain; retain all paths and investigate") from None


def _exchange(left: Path, right: Path) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError):
        raise UpgradeError("Atomic directory exchange is unavailable; no tooling paths were changed") from None
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result != 0:
        error = ctypes.get_errno()
        raise UpgradeError(f"Atomic tooling exchange failed with errno {error}; inspect both paths")


def preflight(tools: Path = TOOLS, state_root: Path = STATE_ROOT, *, owner_uid: int = 0) -> None:
    _directory(tools, owner_uid)
    _directory(tools.parent, owner_uid)
    status = _state_status(tools, state_root)
    if status["blocked"] or _has_pending(status):
        raise UpgradeError("An active or unresolved deployment blocks tooling upgrade")
    try:
        getattr(ctypes.CDLL(None), "renameat2")
    except (OSError, AttributeError):
        raise UpgradeError("Atomic directory exchange is unavailable; no tooling paths were changed") from None


def exchange(
    active_revision: str,
    staged_revision: str,
    path_revision: str,
    operation: str,
    *,
    owner_uid: int = 0,
    tools: Path = TOOLS,
    state_root: Path = STATE_ROOT,
) -> None:
    _revision(active_revision)
    _revision(staged_revision)
    stage = _stage_path(path_revision, owner_uid=owner_uid)
    _tools_parent(tools, owner_uid)
    _verify_lock(tools, state_root, operation, owner_uid)
    _checkout(tools, active_revision, owner_uid)
    _checkout(stage, staged_revision, owner_uid)
    _exchange(tools, stage)
    _fsync_parent(tools)
    if (_git(tools, "rev-parse", "--verify", "HEAD") != staged_revision
            or _git(stage, "rev-parse", "--verify", "HEAD") != active_revision):
        raise UpgradeError("Tooling exchange outcome is uncertain; preserve both checkouts and inspect state")


def release_lock(
    active_revision: str,
    staged_revision: str,
    path_revision: str,
    operation: str,
    *,
    owner_uid: int = 0,
    tools: Path = TOOLS,
    state_root: Path = STATE_ROOT,
) -> None:
    _checkout(tools, active_revision, owner_uid)
    stage = _stage_path(path_revision, owner_uid=owner_uid)
    _checkout(stage, staged_revision, owner_uid)
    _verify_lock(tools, state_root, operation, owner_uid)
    root_fd = _state_root_fd(state_root, owner_uid)
    try:
        reservation_fd = os.open(
            "reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            os.unlink("owner.json", dir_fd=reservation_fd)
            os.fsync(reservation_fd)
        finally:
            os.close(reservation_fd)
        os.rmdir("reservation", dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        raise UpgradeError("Tooling reservation release is uncertain; inspect state before retrying") from None
    finally:
        os.close(root_fd)


def verify_upgrade(active_revision: str, operation: str, *, owner_uid: int = 0) -> None:
    _revision(active_revision)
    _checkout(TOOLS, active_revision, owner_uid)
    _verify_lock(TOOLS, STATE_ROOT, operation, owner_uid)


def reconcile(
    old_revision: str,
    new_revision: str,
    operation: str,
    *,
    owner_uid: int = 0,
    tools: Path = TOOLS,
    state_root: Path = STATE_ROOT,
) -> str:
    _revision(old_revision)
    _revision(new_revision)
    stage = _stage_path(new_revision, owner_uid=owner_uid)
    _verify_lock(tools, state_root, operation, owner_uid)
    active = _git(tools, "rev-parse", "--verify", "HEAD")
    stage_exists = os.path.lexists(stage)
    if active == old_revision:
        if stage_exists:
            _checkout(stage, new_revision, owner_uid)
        _release_owned_lock(state_root, operation, owner_uid)
        return "original-tooling-retained"
    if active == new_revision and stage_exists:
        _checkout(tools, new_revision, owner_uid)
        _checkout(stage, old_revision, owner_uid)
        _exchange(tools, stage)
        _fsync_parent(tools)
        if (_git(tools, "rev-parse", "--verify", "HEAD") != old_revision
                or _git(stage, "rev-parse", "--verify", "HEAD") != new_revision):
            raise UpgradeError("Rollback exchange is uncertain; retain the lock and inspect both paths")
        _release_owned_lock(state_root, operation, owner_uid)
        return "original-tooling-restored"
    raise UpgradeError("Tooling paths do not match either known upgrade state; retain the lock and investigate")


def _release_owned_lock(state_root: Path, operation: str, owner_uid: int) -> None:
    owner = _reservation_owner(state_root, owner_uid)
    if owner["operation"] != operation:
        raise UpgradeError("Cannot release a tooling reservation owned by another operation")
    root_fd = _state_root_fd(state_root, owner_uid)
    try:
        reservation_fd = os.open(
            "reservation", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            os.unlink("owner.json", dir_fd=reservation_fd)
            os.fsync(reservation_fd)
        finally:
            os.close(reservation_fd)
        os.rmdir("reservation", dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        raise UpgradeError("Tooling reservation release is uncertain; inspect state before retrying") from None
    finally:
        os.close(root_fd)


def prune(keep: list[str], tools: Path = TOOLS, state_root: Path = STATE_ROOT, *,
          owner_uid: int = 0) -> dict:
    """Remove retained checkouts except the kept revisions, after a completed upgrade.

    After an upgrade from A to B, ``<tools>.upgrade-<B>`` holds the old A checkout; the
    playbook keeps that one and ``.upgrade-<A>`` (the checkout before A), so the two most
    recent previous checkouts stay. Only real, owner-controlled directories with the exact
    name pattern are removed; anything else is reported and left alone. Refuses while a
    deployment or tooling operation holds the reservation or is unresolved, so a stage that
    is still being fetched is never touched.
    """
    if not keep:
        raise UpgradeError("Name the retained checkouts to keep")
    for revision in keep:
        _revision(revision)
    _directory(tools, owner_uid)
    _directory(tools.parent, owner_uid)
    status = _state_status(tools, state_root)
    if status["blocked"] or _has_pending(status):
        raise UpgradeError("An active or unresolved deployment blocks pruning tooling checkouts")
    pattern = re.compile(re.escape(tools.name) + r"\.upgrade-([a-f0-9]{40})")
    result: dict = {"kept": [], "removed": [], "skipped": [], "freed_bytes": 0}
    for name in sorted(os.listdir(tools.parent)):
        match = pattern.fullmatch(name)
        if not match:
            continue
        path = tools.parent / name
        if match.group(1) in keep:
            result["kept"].append(name)
            continue
        try:
            _directory(path, owner_uid)
        except UpgradeError as error:
            result["skipped"].append({"path": name, "error": str(error)})
            continue
        size = 0
        for root, directories, files in os.walk(path, followlinks=False):
            for entry in files + directories:
                try:
                    size += os.lstat(os.path.join(root, entry)).st_blocks * 512
                except OSError:
                    pass
        try:
            shutil.rmtree(path)
        except OSError as error:
            result["skipped"].append({"path": name, "error": str(error)[:200]})
            continue
        result["removed"].append(name)
        result["freed_bytes"] += size
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("preflight")
    actions.add_parser("acquire")
    exchange_parser = actions.add_parser("exchange")
    verify_parser = actions.add_parser("verify")
    reconcile_parser = actions.add_parser("reconcile")
    release_parser = actions.add_parser("release")
    prune_parser = actions.add_parser("prune")
    prune_parser.add_argument("--keep", action="append", required=True,
                              help="revision in the name of a retained checkout to keep (repeatable)")
    for action in (exchange_parser, verify_parser, reconcile_parser, release_parser):
        action.add_argument("--operation", required=True)
    exchange_parser.add_argument("--active-revision", required=True)
    exchange_parser.add_argument("--staged-revision", required=True)
    exchange_parser.add_argument("--path-revision", required=True)
    verify_parser.add_argument("--active-revision", required=True)
    reconcile_parser.add_argument("--old-revision", required=True)
    reconcile_parser.add_argument("--new-revision", required=True)
    release_parser.add_argument("--active-revision", required=True)
    release_parser.add_argument("--staged-revision", required=True)
    release_parser.add_argument("--path-revision", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if os.geteuid() != 0:
        print("Deployment tooling upgrade requires root", file=sys.stderr)
        return 2
    try:
        if args.action == "preflight":
            preflight()
            result = {"ready": True}
        elif args.action == "acquire":
            result = {"operation": acquire_lock()}
        elif args.action == "exchange":
            exchange(args.active_revision, args.staged_revision, args.path_revision, args.operation)
            result = {"exchanged": True}
        elif args.action == "verify":
            verify_upgrade(args.active_revision, args.operation)
            result = {"verified": True}
        elif args.action == "prune":
            result = prune(args.keep)
        elif args.action == "reconcile":
            result = {"recovery": reconcile(args.old_revision, args.new_revision, args.operation)}
        else:
            release_lock(args.active_revision, args.staged_revision, args.path_revision, args.operation)
            result = {"released": True}
    except UpgradeError as error:
        print(f"Deployment tooling upgrade blocked: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Deployment tooling filesystem operation failed; inspect state before retrying",
              file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
