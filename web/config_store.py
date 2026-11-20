"""Single in-process owner of the web configuration and state files.

The web application is the only writer of both files while it runs; deployment
restores stop it first, and operators may still edit the config by hand. Each
store therefore keeps the parsed file in memory and revalidates it with one
``stat()`` per access, rereading only when the file may have changed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Literal, cast

LoadState = Literal["missing", "valid", "invalid", "unreadable"]

# Timestamps can be coarse (a few milliseconds on many Linux file systems), so a
# same-size rewrite within one tick keeps its signature. Like git's "racy clean"
# check, files modified this recently are compared byte for byte.
RACY_WINDOW_NS = 2_000_000_000

_Signature = tuple[int, int, int, int]


def _signature(path: Path) -> _Signature | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class FileSnapshot:
    """What one JSON file held when it was last read or written."""

    load_state: LoadState
    value: Any
    error: str | None
    raw: bytes | None
    signature: _Signature | None
    generation: int


class JsonFileStore:
    """Cache one JSON file; ``validate`` may raise ``ValueError`` for invalid content."""

    def __init__(self, path: Path, validate: Callable[[Any], None] | None = None,
                 error_prefix: str = "Cannot load file") -> None:
        self.path = path
        self._validate = validate
        self._error_prefix = error_prefix
        self._lock = threading.Lock()
        self._snapshot: FileSnapshot | None = None
        self._generation = 0

    def current(self) -> FileSnapshot:
        with self._lock:
            signature = _signature(self.path)
            snapshot = self._snapshot
            if snapshot is not None and signature == snapshot.signature and (
                    signature is None or time.time_ns() - signature[3] > RACY_WINDOW_NS):
                return snapshot
            return self._reload_locked(signature)

    def value(self) -> Any:
        """A private deep copy of the current parsed value (None unless valid)."""
        snapshot = self.current()
        return deepcopy(snapshot.value) if snapshot.load_state == "valid" else None

    def record_write(self) -> FileSnapshot:
        """Refresh after this process replaced the file; the new bytes are reread once."""
        with self._lock:
            return self._reload_locked(_signature(self.path))

    def _reload_locked(self, signature: _Signature | None) -> FileSnapshot:
        previous = self._snapshot
        if signature is None:
            return self._publish(previous, "missing", None, None, None, None)
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return self._publish(previous, "missing", None, None, None, None)
        except OSError as exc:
            return self._publish(previous, "unreadable", None, f"{self._error_prefix}: {exc}", None, signature)
        if previous is not None and previous.raw == raw:
            # Same bytes (touched or racy-checked): keep the parsed value and generation.
            self._snapshot = FileSnapshot(previous.load_state, previous.value, previous.error, raw, signature,
                                          previous.generation)
            return self._snapshot
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return self._publish(previous, "unreadable", None, f"{self._error_prefix}: {exc}", raw, signature)
        try:
            value = json.loads(text)
            if self._validate is not None:
                self._validate(value)
        except ValueError as exc:
            return self._publish(previous, "invalid", None, f"{self._error_prefix}: {exc}", raw, signature)
        return self._publish(previous, "valid", value, None, raw, signature)

    def _publish(self, previous: FileSnapshot | None, load_state: LoadState, value: Any, error: str | None,
                 raw: bytes | None, signature: _Signature | None) -> FileSnapshot:
        unchanged = previous is not None and previous.load_state == load_state and previous.raw == raw
        if not unchanged:
            self._generation += 1
        self._snapshot = FileSnapshot(load_state, value, error, raw, signature, self._generation)
        return self._snapshot


class ConfigStore(JsonFileStore):
    """The configuration file plus its opaque, process-keyed revision."""

    def __init__(self, path: Path, validate: Callable[[Any], None], revision_key: bytes) -> None:
        super().__init__(path, validate, "Cannot load configuration")
        self._revision_key = revision_key

    def revision(self, snapshot: FileSnapshot | None = None) -> str:
        snapshot = snapshot or self.current()
        if snapshot.load_state == "missing":
            material = b"absent"
        elif snapshot.raw is None:
            material = b"unreadable"
        else:
            material = b"present\0" + snapshot.raw
        target = str(self.path).encode("utf-8", "surrogateescape")
        return hmac.new(self._revision_key, target + b"\0" + material, hashlib.sha256).hexdigest()[:32]

    def config(self) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self.value())
