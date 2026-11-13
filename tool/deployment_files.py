"""Descriptor-based path checks shared by private deployment state and inputs."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Generator


class FileSafetyError(ValueError):
    """A deployment path has unsafe ownership, permissions or type."""


def directory_info(info: os.stat_result, owner_uid: int, *, private: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise FileSafetyError("Deployment path is not a directory")
    if private:
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise FileSafetyError("Private deployment directory requires the expected owner and mode 0700")
    elif (info.st_uid not in (0, owner_uid)
          or (info.st_mode & 0o022 and not
              (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
        raise FileSafetyError("Unsafe deployment ancestor ownership or permissions")


@contextmanager
def checked_directory(path: Path, owner_uid: int, *, private: bool = True) -> Generator[int, None, None]:
    if not path.is_absolute() or ".." in path.parts or (private and path == Path("/")):
        raise FileSafetyError("Deployment directory must be absolute without traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            directory_info(os.fstat(fd), owner_uid, private=private and index == len(path.parts) - 2)
        yield fd
    finally:
        os.close(fd)
