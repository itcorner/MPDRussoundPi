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


def _retention(value: object) -> dict[str, int] | None:
    """How many older operations' files the update removed and how much space it freed."""
    if not isinstance(value, dict):
        return None
    counts = {key: value.get(key) for key in ("operations", "freed_bytes")}
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
        "retention": _retention(result.get("retention")) if result else None,
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
            "retention": _text(summary.get("retention"), 300),
        }
    return status


SYSTEM_PHASES = {
    "idle", "preparing", "awaiting_approval", "installing", "verifying", "installed", "checks_failed",
    "install_failed", "failed_before_install", "outcome_unknown", "up_to_date", "cancelled", "rebooting",
    "verifying_restart", "restarted", "restart_failed",
}
CHECK_STATES = {"pending", "checking", "ready", "failed"}
MAX_PACKAGES = 600


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and TIMESTAMP.fullmatch(value) else None


def _names(value: object, limit: int) -> list[str]:
    return [name for name in (_text(item, 100) for item in value[:limit]) if name] if isinstance(value, list) else []


def sanitize_system(value: dict[str, Any], *, include_details: bool) -> dict[str, Any]:
    """Pi system updates: counts for everyone; packages, reasons, results and the review for administrators."""
    check = value.get("check") if isinstance(value.get("check"), dict) else {}
    restart = check.get("restart") if isinstance(check.get("restart"), dict) else {}
    phase = value.get("phase") if value.get("phase") in SYSTEM_PHASES else "idle"
    public_check = {
        "state": check.get("state") if check.get("state") in CHECK_STATES else "pending",
        "checked_at": _stamp(check.get("checked_at")),
        "index_updated_at": _stamp(check.get("index_updated_at")),
        "index_stale": check.get("index_stale") is True,
        **{key: _count(check.get(key)) for key in ("upgrades", "new", "removals", "security")},
        "restart_needed": restart.get("needed") is True,
        "error": _text(check.get("error")),
    }
    status: dict[str, Any] = {
        "api_version": 1, "configured": True, "phase": phase,
        "kind": value.get("kind") if value.get("kind") in ("install", "restart") else None,
        "busy": value.get("busy") is True, "error": _text(value.get("error"), 300),
        "updated_at": _text(value.get("updated_at"), 32), "check": public_check,
    }
    if not include_details:
        return status
    packages = []
    for item in (check.get("packages") if isinstance(check.get("packages"), list) else [])[:MAX_PACKAGES]:
        if isinstance(item, dict) and _text(item.get("name"), 100):
            packages.append({"name": _text(item.get("name"), 100), "from": _text(item.get("from"), 100),
                             "to": _text(item.get("to"), 100), "security": item.get("security") is True})
    automatic = check.get("automatic") if isinstance(check.get("automatic"), dict) else None
    public_check.update(
        packages=packages, removed=_names(check.get("removed"), 50),
        restart_reasons=_names(restart.get("reasons"), 4),
        retry_after_seconds=_count(check.get("retry_after_seconds")) or 0,
        automatic={"at": _stamp(automatic.get("at")),
                   "result": automatic.get("result") if automatic.get("result") in
                   ("installed", "nothing", "failed", "running") else None,
                   "packages": _names(automatic.get("packages"), 50)} if automatic else None)
    result = value.get("result") if isinstance(value.get("result"), dict) else None
    status["result"] = {
        "finished_at": _stamp(result.get("finished_at")),
        "checks": result.get("checks") if result.get("checks") in ("passed", "failed") else None,
        "missing": _names(result.get("missing"), 10), "kept_configs": _names(result.get("kept_configs"), 20),
        "restart_needed": result.get("restart_needed") is True,
    } if result else None
    review = value.get("review")
    if isinstance(review, dict) and isinstance(review.get("summary"), dict):
        summary = review["summary"]
        status["review"] = {
            "operation": _text(review.get("operation"), 64), "digest": _text(review.get("digest"), 80),
            "expires_in_seconds": review.get("expires_in_seconds") if isinstance(review.get("expires_in_seconds"), int) else 0,
            "action": "restart" if summary.get("action") == "restart" else "install",
            **{key: _count(summary.get(key)) for key in ("upgrades", "new", "removals", "security")},
            "removed": _names(summary.get("removed"), 10), "reasons": _names(summary.get("reasons"), 4),
            "index_updated_at": _stamp(summary.get("index_updated_at")), "index_stale": summary.get("index_stale") is True,
            "docker": summary.get("docker") is True,
            **{key: _text(summary.get(key), 300) for key in ("restart_after", "interruption", "undo", "config")},
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

    def system_status(self, *, include_details: bool) -> dict[str, Any]:
        if not self.configured():
            return {"api_version": 1, "configured": False, "phase": "idle"}
        return sanitize_system(self.request({"op": "system_status"}), include_details=include_details)
