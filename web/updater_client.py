"""Client for the updater service's Unix socket (stage 3 of the web-triggered update plan).

The socket is mounted read-only into the web container at
``/run/mpdrussoundpi-updater`` (``RUSSOUND_UPDATER_SOCKET`` overrides the path).
When it is absent, updates from the page are simply not set up: single-host
Docker installations with the updater service only. Responses are reduced to the
documented fields before they reach a browser.
"""

from __future__ import annotations

import json
import re
import os
from pathlib import Path
import socket
from typing import Any

DEFAULT_SOCKET = "/run/mpdrussoundpi-updater/updater.sock"
TIMEOUT_SECONDS = 10.0
MAX_RESPONSE = 256 * 1024
PHASES = {
    "idle", "resolving", "awaiting_approval", "approved", "running", "succeeded", "up_to_date", "cancelled",
    "failed", "failed_before_interruption", "outcome_unknown", "restore_awaiting_approval", "restoring",
    "restored",
}


class UpdaterUnavailable(RuntimeError):
    """The updater service cannot be reached."""


class UpdaterRefused(ValueError):
    """The updater refused the request; the message is safe to show."""


def _text(value: object, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned[:limit] or None


def _revision(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def _cleanup(value: object) -> dict[str, int] | None:
    """How many old images the update removed and how much space it freed."""
    if not isinstance(value, dict):
        return None
    counts = {key: value.get(key) for key in ("removed", "freed_bytes")}
    if any(type(count) is not int or count < 0 for count in counts.values()):
        return None
    return counts


TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})\Z")
MAX_BACKUPS = 5


def _backups(value: object) -> list[dict[str, Any]]:
    """Restorable backups (newest first) with when they were taken, their version and size."""
    if not isinstance(value, list):
        return []
    backups = []
    for item in value[:MAX_BACKUPS]:
        if not isinstance(item, dict) or not _text(item.get("operation"), 64):
            continue
        taken = item.get("taken_at")
        size = item.get("size_bytes")
        backups.append({
            "operation": _text(item.get("operation"), 64),
            "taken_at": taken if isinstance(taken, str) and TIMESTAMP.fullmatch(taken) else None,
            "revision": _revision(item.get("revision")),
            "size_bytes": size if type(size) is int and size >= 0 else None,
            "reason": _text(item.get("reason"), 80),
        })
    return backups


def sanitize_status(value: dict[str, Any], *, include_review: bool) -> dict[str, Any]:
    """Only the documented fields; the review (with its confirmation digest) only for administrators."""
    phase = value.get("phase") if value.get("phase") in PHASES else "idle"
    result = value.get("result") if isinstance(value.get("result"), dict) else None
    status: dict[str, Any] = {
        "api_version": 1, "configured": True, "phase": phase,
        "kind": value.get("kind") if value.get("kind") in ("update", "restore") else None,
        "operation": _text(value.get("operation"), 64),
        "installed_revision": _revision(value.get("installed_revision")),
        "candidate_revision": _revision(value.get("candidate_revision")),
        "error": _text(value.get("error")),
        "finished_at": _text(result.get("finished_at"), 32) if result else None,
        "cleanup": _cleanup(result.get("cleanup")) if result else None,
        "restore_available": value.get("restore_available") is True,
        "backups": _backups(value.get("backups")),
        "busy": value.get("busy") is True,
        "updated_at": _text(value.get("updated_at"), 32),
    }
    review = value.get("review")
    if include_review and isinstance(review, dict) and isinstance(review.get("summary"), dict):
        summary = review["summary"]
        images = summary.get("images") if isinstance(summary.get("images"), dict) else {}
        status["review"] = {
            "operation": _text(review.get("operation"), 64), "digest": _text(review.get("digest"), 80),
            "expires_in_seconds": review.get("expires_in_seconds") if isinstance(review.get("expires_in_seconds"), int) else 0,
            "action": "restore" if summary.get("action") == "restore" else "update",
            "revision": _revision(summary.get("revision")),
            "images": {str(name)[:16]: _text(reference, 160) for name, reference in list(images.items())[:4]},
            "layout_change": _text(summary.get("layout_change"), 400),  # up to two named changes
            "interruption": _text(summary.get("interruption"), 300),
            "backup": _text(summary.get("backup"), 300),
            "cleanup": _text(summary.get("cleanup"), 300),
        }
    return status


class UpdaterClient:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("RUSSOUND_UPDATER_SOCKET") or DEFAULT_SOCKET)

    def configured(self) -> bool:
        try:
            return self.path.is_socket()
        except OSError:
            return False

    def request(self, value: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            raise UpdaterUnavailable("The updater service is not installed on this host.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(TIMEOUT_SECONDS)
                client.connect(str(self.path))
                client.sendall(json.dumps(value).encode() + b"\n")
                data = b""
                while not data.endswith(b"\n") and len(data) <= MAX_RESPONSE:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            response = json.loads(data)
        except (OSError, ValueError) as error:
            raise UpdaterUnavailable("The updater service did not answer.") from error
        if not isinstance(response, dict):
            raise UpdaterUnavailable("The updater service answered unexpectedly.")
        if response.get("ok") is not True:
            raise UpdaterRefused(_text(response.get("error")) or "The updater refused the request.")
        return response

    def status(self, *, include_review: bool) -> dict[str, Any]:
        if not self.configured():
            return {"api_version": 1, "configured": False, "phase": "idle"}
        return sanitize_status(self.request({"op": "status"}), include_review=include_review)
