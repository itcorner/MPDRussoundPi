"""Read-only, first-install Linux preflight, also executable over Ansible stdin.

No repository imports: the controller passes these exact source bytes to the
target Python interpreter. This check never provisions paths or starts services.
"""

import errno
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
from typing import Any, TypeGuard


class PreflightError(ValueError):
    """A target prerequisite failed without exposing private target details."""


def is_mapping(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def validate_request(value: Any) -> dict[str, Any]:
    fields = {"gateway", "project", "platform", "data_dir", "uid", "gid",
              "publish_host", "publish_port", "serial_device", "serial_gid"}
    if not is_mapping(value) or set(value) != fields:
        raise PreflightError("Invalid target preflight request")
    if (value["gateway"] not in ("managed", "external", "dummy")
            or value["platform"] not in ("linux/arm64", "linux/amd64")
            or not isinstance(value["project"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value["project"])):
        raise PreflightError("Unsupported target layout, project or platform")
    for key in ("uid", "gid", "publish_port"):
        if type(value[key]) is not int or value[key] < 0:
            raise PreflightError("Target IDs and port must be nonnegative integers")
    if not 1 <= value["publish_port"] <= 65535:
        raise PreflightError("Target publish port must be between 1 and 65535")
    if not isinstance(value["publish_host"], str):
        raise PreflightError("Publish host must be an explicit IPv4 or IPv6 address")
    try:
        family = socket.AF_INET6 if ":" in value["publish_host"] else socket.AF_INET
        socket.inet_pton(family, value["publish_host"])
    except OSError:
        raise PreflightError("Publish host must be an explicit IPv4 or IPv6 address") from None
    for key in ("data_dir", "serial_device"):
        if not isinstance(value[key], str) or not value[key].startswith("/") or ".." in Path(value[key]).parts:
            raise PreflightError("Target data and device paths must be absolute without traversal")
    if value["data_dir"] == "/":
        raise PreflightError("Filesystem root cannot be a deployment data directory")
    if value["gateway"] == "managed" and (type(value["serial_gid"]) is not int or value["serial_gid"] < 0):
        raise PreflightError("Managed gateway requires a numeric serial group")
    return value


def docker_json(arguments: list[str]) -> Any:
    try:
        result = subprocess.run(["docker", *arguments], capture_output=True, timeout=30)
        if result.returncode:
            raise PreflightError("Docker inspection failed; check daemon access and retry explicitly")
        if arguments[:2] == ["container", "ls"]:
            return [json.loads(line) for line in result.stdout.splitlines()]
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise PreflightError("Docker inspection unavailable, timed out or returned invalid metadata") from None


def safe_path(path: Path, *, device: bool = False) -> os.stat_result:
    """Reject symlinks and writable-by-others ancestors; never create the path."""
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PreflightError("Target path contains a symlink; explicit path review is required")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise PreflightError("Target path ancestor is not a directory")
        forbidden = 0o002 if device and current == path and stat.S_ISCHR(info.st_mode) else 0o022
        if info.st_mode & forbidden and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise PreflightError("Target path is writable by other users or groups")
    return path.lstat()


def mode_access(info: os.stat_result, uid: int, groups: set[int], required: int) -> bool:
    # Do not assume container root can bypass permissions/capability restrictions.
    shift = 6 if info.st_uid == uid else 3 if info.st_gid in groups else 0
    return (stat.S_IMODE(info.st_mode) >> shift) & required == required


def check_data(request: dict[str, Any]) -> dict[str, Any]:
    root = Path(request["data_dir"])
    safe_path(root)
    for ancestor in (root, *root.parents):
        if not mode_access(ancestor.stat(), request["uid"], {request["gid"]}, 1):
            raise PreflightError("Service identity lacks data-path ancestor traversal permissions")
    names = ("config", "data", "logs", "secrets") + (("dummy-data",) if request["gateway"] == "dummy" else ())
    for name in names:
        path = root / name
        info = safe_path(path)
        if not stat.S_ISDIR(info.st_mode):
            raise PreflightError("A required bind mount is not a directory")
        if name == "secrets" and (info.st_uid != request["uid"] or stat.S_IMODE(info.st_mode) != 0o700):
            raise PreflightError("Secrets directory requires the service owner and mode 0700")
        required = 5 if name == "secrets" else 7
        if not mode_access(info, request["uid"], {request["gid"]}, required):
            raise PreflightError("Service identity lacks required bind-directory mode permissions")
        try:
            getxattr = getattr(os, "getxattr", None)
            if getxattr is None:
                raise PreflightError("Linux extended-attribute inspection is unavailable")
            if getxattr(path, "system.posix_acl_access"):
                raise PreflightError("Bind-directory ACLs require separate access validation")
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
    return {"directories": "passed-mode-only", "free_bytes": shutil.disk_usage(root).free,
            "runtime_write_access": "not-tested", "existing_config": "not-validated"}


def overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def check_containers(request: dict[str, Any], containers: Any) -> None:
    if not is_list(containers):
        raise PreflightError("Invalid Docker container metadata")
    for container in containers:
        labels: dict[str, Any] = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == request["project"]:
            raise PreflightError("Existing Compose project requires recorded rerun or explicit adoption")
        for mount in container.get("Mounts", []):
            if mount.get("Type") == "bind" and overlap(Path(mount["Source"]), Path(request["data_dir"])):
                raise PreflightError("Deployment data overlaps an existing container mount")
        devices: list[dict[str, Any]] = container.get("HostConfig", {}).get("Devices") or []
        for device in devices:
            if request["gateway"] == "managed" and device.get("PathOnHost") == request["serial_device"]:
                raise PreflightError("Serial adapter is assigned to an existing container")


def check_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind((host, port))
        except OSError:
            raise PreflightError("Requested host listener is unavailable; no service was stopped") from None


def check_serial(request: dict[str, Any]) -> None:
    if os.geteuid() != 0:
        raise PreflightError("Managed serial ownership inspection requires root; preflight does not escalate itself")
    path = Path(request["serial_device"])
    info = safe_path(path, device=True)
    if not stat.S_ISCHR(info.st_mode):
        raise PreflightError("Managed serial path must be a character device")
    if info.st_gid != request["serial_gid"] or not mode_access(
            info, request["uid"], {request["gid"], request["serial_gid"]}, 6):
        raise PreflightError("Managed serial group or read/write mode permissions do not match")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    opened = descriptor.stat()
                    if stat.S_ISCHR(opened.st_mode) and opened.st_rdev == info.st_rdev:
                        raise PreflightError("Managed serial device is open by an existing process")
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except PermissionError:
            raise PreflightError("Cannot establish serial ownership for every process") from None


def preflight(request: Any) -> dict[str, Any]:
    request = validate_request(request)
    if not sys.platform.startswith("linux"):
        raise PreflightError("Target preflight supports Linux only")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise PreflightError("Remote or environment-selected Docker endpoints are unsupported")
    context = docker_json(["context", "inspect"])
    endpoint = context[0]["Endpoints"]["docker"]["Host"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
        raise PreflightError("Docker daemon must use a local Unix socket")
    info = docker_json(["info", "--format", "{{json .}}"])
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
    if f'{info["OSType"]}/{architecture}' != request["platform"]:
        raise PreflightError("Docker daemon platform does not match the selected images")
    if any("rootless" in option or "userns" in option for option in info.get("SecurityOptions", [])):
        raise PreflightError("User-namespace remapping requires separate service identity validation")
    compose = docker_json(["compose", "version", "--format", "json"])
    if not is_mapping(compose) or not isinstance(compose.get("version"), str):
        raise PreflightError("Docker Compose version could not be established")
    ids = docker_json(["container", "ls", "--all", "--no-trunc", "--format", "{{json .ID}}"])
    if not is_list(ids) or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
                              for item in ids):
        raise PreflightError("Invalid Docker container listing")
    containers: Any = docker_json(["container", "inspect", *ids]) if ids else []
    check_containers(request, containers)
    data = check_data(request)
    check_port(request["publish_host"], request["publish_port"])
    if request["gateway"] == "managed":
        check_serial(request)
    return {
        "kind": "development-target-preflight", "action": "read-only-first-install",
        "platform": request["platform"], "project": "absent", "data": data,
        "compose_version": compose["version"],
        "publish_listener": "available-at-check", "serial": "unclaimed-at-check" if request["gateway"] == "managed"
        else "not-managed", "proxy": "not-checked-playback-unsupported",
        "container_routing": "not-tested", "safe_to_apply": False,
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise PreflightError("Target preflight request exceeds size limit")
        result = preflight(json.loads(raw))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        message = str(error) if isinstance(error, PreflightError) else "Target inspection failed; no deployment was attempted"
        print("Target preflight failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
