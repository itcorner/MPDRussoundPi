"""Sealed, same-host native releases; staging never changes live services."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any
from uuid import UUID, uuid4

from tool.deployment_files import checked_directory, is_mapping


NATIVE_CANDIDATE = "native-source-candidate"
RELEASES = Path("/opt/mpdrussoundpi-native")
PYTHON = Path("/usr/bin/python3")
SER2NET = Path("/usr/sbin/ser2net")
REVISION = re.compile(r"[a-f0-9]{40}")
SHA256 = re.compile(r"[a-f0-9]{64}")
MAX_RELEASE_BYTES = 512 * 1024**2


class NativeError(ValueError):
    """Native work is incomplete or unsafe; retain evidence and inspect state."""


def validate_candidate(value: Any) -> None:
    if (not is_mapping(value) or set(value) != {
            "schema_version", "kind", "release", "revision", "sha256", "runtime"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != NATIVE_CANDIDATE
            or not isinstance(value["revision"], str) or not REVISION.fullmatch(value["revision"])
            or not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"])):
        raise NativeError("Invalid native source/environment candidate")
    try:
        if not isinstance(value["release"], str) or str(UUID(value["release"])) != value["release"]:
            raise ValueError
    except ValueError:
        raise NativeError("Native releases require a canonical UUID") from None
    runtime = value["runtime"]
    if (not is_mapping(runtime) or set(runtime) != {"python", "ser2net", "version", "machine"}
            or any(not isinstance(runtime[key], str) or not SHA256.fullmatch(runtime[key])
                   for key in ("python", "ser2net"))
            or not isinstance(runtime["version"], str) or not re.fullmatch(r"3\.\d+\.\d+", runtime["version"])
            or runtime["machine"] not in ("aarch64", "x86_64")):
        raise NativeError("Invalid native same-host runtime identity")
    if tuple(int(part) for part in runtime["version"].split(".")) < (3, 11):
        raise NativeError("Native application runtime requires Python 3.11+")


def file_digest(path: Path) -> str:
    from tool.deployment_inputs import attributes
    with path.open("rb") as source:
        before = attributes(os.fstat(source.fileno()))
        result = hashlib.file_digest(source, "sha256").hexdigest()
        if before != attributes(os.fstat(source.fileno())):
            raise NativeError("Native release changed during verification")
    return result


def runtime_identity() -> dict[str, str]:
    result = subprocess.run([str(PYTHON), "-c",
                             "import platform,sys; assert sys.version_info >= (3,11); print(platform.python_version())"],
                            check=True, capture_output=True, text=True, timeout=15)
    if platform.machine() not in ("aarch64", "x86_64"):
        raise NativeError("Prepared native releases support Linux ARM64/AMD64 only")
    return {"python": file_digest(PYTHON), "ser2net": file_digest(SER2NET),
            "version": result.stdout.strip(), "machine": platform.machine()}


def tree_digest(root: Path, *, owner: int = 0) -> str:
    """Reject links and unsupported metadata so shared backups preserve every byte."""
    from tool.deployment_backup import extended_metadata, source_info
    from tool.deployment_inputs import attributes
    digest = hashlib.sha256()
    total = 0
    with checked_directory(root, owner, private=False):
        pass
    paths = [root, *sorted(root.rglob("*"))]
    if len(paths) > 8000:
        raise NativeError("Native release exceeds the backup entry budget")
    for path in paths:
        before = path.lstat()
        source_info(before, owner)
        if before.st_uid != owner:
            raise NativeError("Native release must be wholly owned by its staging identity")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            extended_metadata(fd)
            entry: dict[str, Any] = {"path": str(path.relative_to(root)),
                                     "mode": stat.S_IMODE(before.st_mode)}
            if stat.S_ISREG(before.st_mode):
                total += before.st_size
                if total > MAX_RELEASE_BYTES:
                    raise NativeError("Native release exceeds its byte budget")
                entry["sha256"] = file_digest(path)
            digest.update((json.dumps(entry, sort_keys=True) + "\n").encode())
            if attributes(before) != attributes(os.fstat(fd)):
                raise NativeError("Native release changed during verification")
        finally:
            os.close(fd)
    if [root, *sorted(root.rglob("*"))] != paths:
        raise NativeError("Native release membership changed during verification")
    return digest.hexdigest()


def verify_release(selection: dict[str, Any], *, releases: Path = RELEASES) -> Path:
    validate_candidate(selection)
    release = releases / selection["release"]
    if runtime_identity() != selection["runtime"] or tree_digest(release) != selection["sha256"]:
        raise NativeError("Sealed native release or system Python/ser2net changed; stage a new candidate")
    return release


def publish_release(release: Path, *, owner: int = 0) -> None:
    """Only public code/dependencies live here; make them readable under umask 077."""
    for path in (release, *release.rglob("*")):
        info = path.lstat()
        if (info.st_uid != owner or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise NativeError("Native releases cannot publish links, special files or foreign-owned assets")
        path.chmod(0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o644)


def stage(source: Path, revision: str, *, releases: Path = RELEASES) -> dict[str, Any]:
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise NativeError("Native release staging requires Linux root")
    if not REVISION.fullmatch(revision):
        raise NativeError("Select an explicitly reviewed full application commit")
    with checked_directory(source, 0, private=False):
        pass
    object_type = subprocess.run(["git", "-C", str(source), "cat-file", "-t", revision],
                                 check=True, capture_output=True, timeout=30)
    if object_type.stdout.strip() != b"commit":
        raise NativeError("Native source identity must select a commit, not a tree or tag object")
    if not releases.exists():
        releases.mkdir(mode=0o755)
        releases.chmod(0o755)
    with checked_directory(releases, 0, private=False):
        pass
    if stat.S_IMODE(releases.stat().st_mode) != 0o755:
        raise NativeError("Native release storage must already be root-owned mode 0755")
    runtime = runtime_identity()
    release_id = str(uuid4())
    release = releases / release_id
    release.mkdir(mode=0o755)
    release.chmod(0o755)
    code = release / "source"
    # git archive reads the selected commit, not mutable checkout contents.
    from tool.deployment_build import unpack_source
    with tempfile.TemporaryDirectory(prefix="native-source-") as directory:
        archive = Path(directory) / "source.tar"
        with archive.open("xb") as output:
            subprocess.run(["git", "-C", str(source), "archive", "--format=tar", revision,
                            "web", "mpdrussoundpi"], stdout=output, check=True, timeout=60)
        unpack_source(archive, code)
    environment = release / "venv"
    subprocess.run([str(PYTHON), "-m", "venv", "--copies", str(environment)], check=True, timeout=120)
    lib64 = environment / "lib64"
    if lib64.is_symlink() and os.readlink(lib64) == "lib":
        lib64.unlink()
    subprocess.run([str(environment / "bin/python"), "-m", "pip", "install",
                    "-r", str(code / "web/requirements.txt")], check=True, timeout=600,
                   stdout=sys.stderr, stderr=sys.stderr)
    subprocess.run([str(environment / "bin/python"), "-m", "pip", "check"], check=True, timeout=60,
                   stdout=sys.stderr, stderr=sys.stderr)
    subprocess.run([str(environment / "bin/python"), "-c", "import web.russound_server"],
                   cwd=code, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True, timeout=30)
    publish_release(release)
    result: dict[str, Any] = {"schema_version": 1, "kind": NATIVE_CANDIDATE, "release": release_id,
                              "revision": revision, "runtime": runtime, "sha256": tree_digest(release)}
    validate_candidate(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(stage(Path("/opt/mpdrussoundpi-deploy"), args.revision), sort_keys=True))
    except NativeError as error:
        print(f"Native staging refused: {error}. Retain any candidate for inspection.", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Native staging failed: {type(error).__name__}. Retain the candidate for inspection.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
