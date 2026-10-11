from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import logging
import os
import stat
import sys


@contextmanager
def setup_lock(lock_name: str = 'audio_setup.lock') -> Generator[None, None, None]:
    """Hold a per-user kernel lock throughout a setup operation."""
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    if not runtime_dir or not os.path.isabs(runtime_dir):
        raise RuntimeError("XDG_RUNTIME_DIR must name an absolute private runtime directory")
    directory = os.stat(runtime_dir)
    if (not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700):
        raise RuntimeError("XDG_RUNTIME_DIR must be owned by the current user with mode 0700")
    if os.path.basename(lock_name) != lock_name or lock_name in ('', '.', '..'):
        raise ValueError("Setup lock name must be a filename")

    lock_path = os.path.join(runtime_dir, lock_name)
    # Keep the inode in place: unlinking it can create independent locks.
    with open(lock_path, 'a+') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = f"Another setup instance holds {lock_name}. Exiting."
            logging.error(message)
            print(message, file=sys.stderr)
            raise SystemExit(1)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
