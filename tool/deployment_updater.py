"""Updater service on the Pi: web-triggered Docker image updates through the reviewed tooling.

Stage 2 of docs/plans/general_open_user-triggered-image-updates.md. The service runs
as root from the trusted tooling checkout (/opt/mpdrussoundpi-deploy). The web
container reaches it only through a Unix socket in a directory mounted read-only
into the container; the socket is group-restricted to the container's numeric
user. Requests are a fixed vocabulary, one JSON object per connection:

- ``status``: the current or last operation, without secrets.
- ``review``: resolve the newest image candidate of the installation's channel
  (pinned by digest) and start the reviewed transaction, which waits for approval.
  On the stable channel a release is offered only when it is newer than the
  installed release, or contains the installed development build
  (docs/plans/general_new_release-channels.md); otherwise ``waiting_for_release``.
- ``channel``: choose ``stable`` or ``development`` for the next check (stored in
  ``channel.json``; without a choice, the channel of the installed version).
- ``approve``: confirm exactly the pending review (operation id and digest) within
  its window. The web application authenticates the administrator first; the
  socket proves the caller is the web container, not which person approved.
- ``cancel``: decline a pending review; nothing has been interrupted yet.
- ``restore``: after a failed update, restore the previous version from the
  backup kept on this Pi, through the reviewed recovery and its own approval.
- ``system_status``, ``system_check``, ``system_review`` (``kind`` install or
  restart), ``system_approve`` and ``system_cancel``: Pi package updates and the
  restart, handled by tool/system_updates.py
  (docs/plans/general_open_pi-system-updates.md). An image operation and a
  system operation never start at the same time.

The transaction is the operator's own: ``controller()`` here and ``serve`` in a
root child process, connected by pipes instead of SSH. Reservation, pull by
digest, quiescence, backup and its verification, old-image export, apply, checks,
recovery and evidence are unchanged code. The backup stays on this Pi (plan
decision 3: not disaster recovery). The operator lock excludes a concurrent SSH
operator run.

Supported layout: one host running the Docker containers (see the plan).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from mpdrussoundpi.common import dataschema, sourcehistory
from mpdrussoundpi.common.atomicjson import write_json
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import UPDATER_SOCKET_DIR, ComposeError, private_read
from tool.deployment_files import checked_directory
from tool.deployment_state import StateError, StateStore, identity, operation_id
from tool.system_updates import SystemRefused, SystemUpdates

TOOLS = Path("/opt/mpdrussoundpi-deploy")
CONFIG = Path("/etc/mpdrussoundpi-updater/updater.json")
ROOT = Path("/var/lib/mpdrussoundpi-updater")
SOCKET_NAME = "updater.sock"
REVIEW_SECONDS = 120
MAX_REQUEST = 64 * 1024
ACTIVE_PHASES = ("resolving", "awaiting_approval", "approved", "running", "restore_awaiting_approval",
                 "restoring")
# docs/plans/general_new_release-channels.md: what the administrator chooses, and the
# image channel the tooling resolves for it.
CHANNELS = {"stable": "stable", "development": "dev"}
# Short lines for the review card on the Status page (label: text).
INTERRUPTION = "Web app and gateway restart; this page reconnects after a few minutes."
BACKUP_NOTE = "Taken first, kept on this Pi only (not disaster recovery)."
CLEANUP_NOTE = "Removed after success; the backup keeps a copy."
RETENTION_NOTE = "Kept for this and the previous update; older ones are removed after success."
# Target phases before any backup was recorded: nothing was applied, the original runtime is unchanged.
BEFORE_BACKUP = ("prepared", "inputs-verified")
NOT_APPLIED = ("The updater restarted before the update changed the installed version, but the services may "
               "still be stopped. Restart them with the SSH operator's resume-original (see the deployment "
               "documentation).")
# Progress of an approved update for the Status page. The Pi's deployment record names the
# last completed phase (tool/deployment_state.PHASES), and with it the step that runs now:
# 1 preparing, 2 stopping the services and taking the backup, 3 loading and starting the new
# images, 4 runtime checks, 5 cleanup. The web application is stopped during steps 2 and 3.
PROGRESS_STEPS = 5
PROGRESS_FROM_PENDING = {"prepared": 1, "inputs-verified": 2, "backup-complete": 3, "apply-complete": 4,
                         "checks-complete": 5}
PROGRESS_INTERVAL = 2.0
SETTLED = ("The updater restarted during the last operation. The Pi has no unfinished operation now (it was "
           "recovered or never changed anything), so you can update again.")

logger = logging.getLogger("mpdrussoundpi.updater")


def _timings(value: Any) -> list[int] | None:
    """Seconds per update step, as recorded after a successful update."""
    if (isinstance(value, list) and len(value) == PROGRESS_STEPS
            and all(type(item) is int and 0 <= item <= 86400 for item in value)):
        return value
    return None


def _cleanup(value: Any) -> dict[str, Any] | None:
    """Only the counts of the operator's image cleanup (decision 4 of the cleanup plan)."""
    if not isinstance(value, dict):
        return None
    counts = {key: value.get(key) if type(value.get(key)) is int and value.get(key) >= 0 else 0
              for key in ("removed", "skipped", "freed_bytes")}
    error = value.get("error")
    return {**counts, "error": error[:200] if isinstance(error, str) else None}



def _retention(value: Any) -> dict[str, Any] | None:
    """Only the counts of the operator's operation file retention (decision 2 of the retention plan)."""
    if not isinstance(value, dict):
        return None
    counts = {key: value.get(key) if type(value.get(key)) is int and value.get(key) >= 0 else 0
              for key in ("operations", "removed", "skipped", "freed_bytes")}
    error = value.get("error")
    return {**counts, "error": error[:200] if isinstance(error, str) else None}


def _restorable_backups(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The backups a restore can use, newest first: today only the failed update's own backup.

    A list so that keeping more backups later adds entries without changing the protocol.
    """
    restore = state.get("restore")
    if not isinstance(restore, dict) or state.get("phase") not in ("failed", "outcome_unknown"):
        return []
    return [{"operation": restore.get("operation"), "taken_at": restore.get("taken_at"),
             "revision": restore.get("revision"), "size_bytes": restore.get("size_bytes"),
             "reason": "before the update that failed"}]

# The application's versioned files below the deployment's data directory (dataschema).
DATA_FILES = {
    "config": "config/russound_config.json",
    "state": "data/russound_state.json",
    "lockout": "data/admin-lockout.json",
}


def _version_key(version: object) -> tuple[int, ...]:
    """Numeric order of a release version ``X.Y.Z``; anything else sorts first."""
    if isinstance(version, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
        return tuple(int(part) for part in version.split("."))
    return (-1,)

class UpdaterError(ValueError):
    """A request the updater refuses; the message is safe to show."""


def _now_text(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def _actor(value: object) -> dict[str, str]:
    """The web's description of the administrator session: a short reference and the client address."""
    if not isinstance(value, dict):
        raise UpdaterError("Missing administrator reference")
    session, client = value.get("session"), value.get("client")
    if (not isinstance(session, str) or not 1 <= len(session) <= 16
            or not isinstance(client, str) or len(client) > 64):
        raise UpdaterError("Invalid administrator reference")
    return {"session": session, "client": client}


def run_transaction(request: dict[str, Any], approve: Callable[[dict[str, Any]], str | None], evidence: Path, *,
                    recovery: Path | None = None, recovery_checksum: str | None = None) -> dict[str, Any]:
    """The operator's controller against a local ``serve`` child, connected by pipes instead of SSH."""
    from tool.deployment_operator import controller
    log_path = evidence / (request["operation"] + (".restore" if recovery else "") + ".serve.log")
    with log_path.open("xb") as log:
        os.fchmod(log.fileno(), 0o600)
        with subprocess.Popen([sys.executable, "-m", "tool.deployment_operator", "serve"], cwd=TOOLS,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log) as process:
            assert process.stdin is not None and process.stdout is not None
            try:
                result = controller(Channel(process.stdout, process.stdin), request, evidence, approve,
                                    recovery=recovery, recovery_checksum=recovery_checksum)
                process.stdin.close()
                if process.wait(timeout=60):
                    raise ChannelError("Target exit was unsuccessful; completion is uncertain")
                return result
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class Updater:
    """One operation at a time, with a durable record that survives restarts."""

    def __init__(self, spec: dict[str, Any], evidence: Path, state_path: Path, *,
                 resolve: Callable[[str, str, str], dict[str, Any]] | None = None,
                 committed: Callable[[], dict[str, Any] | None] | None = None,
                 transaction: Callable[..., dict[str, Any]] = run_transaction,
                 clock: Callable[[], float] = time.time,
                 target: Callable[[], dict[str, Any]] | None = None,
                 system: SystemUpdates | None = None, gate: Any = None,
                 target_schemas: Callable[[dict[str, Any]], dict[str, tuple[int, int]] | None] | None = None,
                 channel_path: Path | None = None,
                 contains: Callable[[str, str], bool] = sourcehistory.contains) -> None:
        identity(spec["deployment"])
        self.spec = spec
        self.evidence = evidence
        self.state_path = state_path
        self._resolve = resolve or self._resolve_registry
        self._committed = committed or self._committed_from_store
        self._transaction = transaction
        self._clock = clock
        self._target = target or (lambda: StateStore().status())
        self._target_schemas = target_schemas or self._target_schemas_registry
        self.channel_path = channel_path or state_path.parent / "channel.json"
        self._contains = contains
        self.system = system
        self.gate = gate or threading.Lock()  # shared with the system part: one operation starts at a time
        self._lock = threading.Condition()
        self._decision: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self.state = self._load()

    # ------------------------------------------------------------ durable state

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("phase", "idle")
        phase = state["phase"]
        if phase in ("resolving", "awaiting_approval", "restore_awaiting_approval"):
            # The transaction ended with the service before approval: nothing was interrupted.
            state.update(phase="cancelled", review=None, error="The updater restarted before approval.")
        elif phase in ("approved", "running", "restoring"):
            # After the dispatch boundary: never retried, never assumed to have worked.
            state.update(phase="outcome_unknown", review=None,
                         error="The updater restarted during the operation, so its outcome is unknown. "
                               "Check the deployment on the Pi before trying again.")
            if phase != "restoring" and isinstance(state.get("operation"), str):
                # A backup verified before the restart can still be restored from the page (its own
                # review, recent password); the target's recovery refuses if its state does not match.
                # Without one, nothing was interrupted yet or recovery needs the SSH operator.
                state["restore"] = self._backup_identity(state["operation"])
                if state["restore"]:
                    state["error"] = ("The updater restarted during the update, so its outcome is unknown. "
                                      "Restore the backup taken before it, or check the deployment on the Pi.")
                # The controller keeps the backup identity before the target records the backup: while the
                # target is still before its backup, nothing was applied and the backup is the wrong recovery.
                record, step = self._target_record(state["operation"])
                if record == "pending" and step in BEFORE_BACKUP:
                    state.update(restore=None, error=NOT_APPLIED)
        self._save(state)
        return state

    def _save(self, state: dict[str, Any] | None = None) -> None:
        value = state if state is not None else self.state
        value["updated_at"] = _now_text(self._clock())
        write_json(self.state_path, value)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self.state.update(changes)
            self._save()
            self._lock.notify_all()

    # --------------------------------------------------------------- candidates

    def _release_waits(self, candidate: dict[str, Any], installed: dict[str, Any]) -> dict[str, Any] | None:
        """State changes when the newest release is not offered, or None when it is (plan decision 2).

        Never backwards: a release is offered over a release only when its version is higher, and over
        a development build only when it contains that build.
        """
        if installed.get("kind") == "release-image-candidate":
            if _version_key(candidate.get("version")) <= _version_key(installed.get("version")):
                return {"phase": "up_to_date", "error": None}
            return None
        try:
            if self._contains(str(candidate.get("revision")), str(installed.get("revision"))):
                return None
        except (sourcehistory.HistoryError, OSError, ValueError) as error:
            logger.warning("Release containment could not be checked: %s", error)
            return {"phase": "failed_before_interruption",
                    "error": "Whether the newest release includes the installed development build could not be "
                             "checked on GitHub. Nothing was interrupted."}
        return {"phase": "waiting_for_release", "error": None}

    def _schema_refusal(self, candidate: dict[str, Any]) -> str | None:
        """Why the candidate's web image cannot read the data on this host, checked before anything stops.

        docs/plans/general_open_persisted-data-schema-version.md. A restore needs no check: it brings
        back the old images with their own backed-up data.
        """
        images = candidate.get("images") if isinstance(candidate.get("images"), dict) else {}
        if "web" not in images:
            return None  # no web application, no application data
        data = self._data_versions()
        if not data:
            return None
        try:
            target = self._target_schemas(candidate)
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("Data schema label could not be read: %s", error)
            return "The new version's data format could not be checked on Docker Hub. Nothing was interrupted."
        problems = dataschema.problems(target, data)
        if not problems:
            return None
        revision = str(candidate.get("revision") or "")[:7]
        return (f"The new version {revision} cannot read the data on this Pi: " + "; ".join(problems)
                + ". Nothing was interrupted.")

    def _data_versions(self) -> dict[str, int]:
        """Schema versions of the application's files in the deployment's data directory."""
        if not isinstance(self.spec.get("data_dir"), str):
            return {}
        root = Path(self.spec["data_dir"])
        found: dict[str, int] = {}
        for name, relative in DATA_FILES.items():
            try:
                found[name] = dataschema.version_of(json.loads((root / relative).read_text(encoding="utf-8")))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                continue  # a broken file is the application's to report; it is not a newer format
        return found

    def _target_schemas_registry(self, candidate: dict[str, Any]) -> dict[str, tuple[int, int]] | None:
        from mpdrussoundpi.common.imageregistry import Registry, platform_config
        web = candidate["images"]["web"]
        found = platform_config(Registry(), web["repository"], web["index_digest"], self.spec["platform"])
        return dataschema.parse_label(found["labels"].get(dataschema.LABEL))

    def _resolve_registry(self, gateway: str, platform: str, channel: str = "dev") -> dict[str, Any]:
        from tool.deployment_images import resolve
        return resolve(gateway, platform, channel=channel)

    def _target_record(self, operation: object) -> tuple[str, str | None]:
        """What the Pi's deployment record says about an interrupted operation.

        ``("clear", None)`` when no operation is reserved or unfinished, ``("pending", phase)`` when this
        operation is still unfinished, otherwise ``("unknown", None)``: then nothing is concluded.
        """
        try:
            status = self._target()
            pending = (status["deployments"].get(self.spec["deployment"]) or {}).get("pending")
            if status["blocked"] is False and status["reservation"] is None and pending is None:
                return "clear", None
            if isinstance(pending, dict) and pending.get("operation") == operation:
                return "pending", pending.get("phase")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StateError):
            pass
        return "unknown", None

    def _settle(self) -> None:
        """An unknown outcome ends once the Pi has no unfinished operation (the SSH operator recovered it).

        It becomes an ordinary failure without a restore, so the page offers updates again; the record
        of the interrupted operation stays. Called with the lock held.
        """
        if self.state["phase"] == "outcome_unknown" and self._target_record(self.state.get("operation"))[0] == "clear":
            self.state.update(phase="failed", restore=None, error=SETTLED)
            self._save()

    def deployment_clear(self) -> bool:
        """No deployment of this Pi is blocked, reserved or unfinished (for the system part)."""
        try:
            status = self._target()
            return (status["blocked"] is False and status["reservation"] is None
                    and all((record or {}).get("pending") is None for record in status["deployments"].values()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StateError):
            return False

    def busy(self) -> bool:
        with self._lock:
            return self.state["phase"] in ACTIVE_PHASES

    def _system_busy(self) -> bool:
        # Also the dpkg lock: an install must block image updates even if its phase was recorded wrongly.
        return self.system is not None and self.system.packages_busy()

    def _committed_from_store(self) -> dict[str, Any] | None:
        status = StateStore().status()
        deployment = status["deployments"].get(self.spec["deployment"], {})
        committed = deployment.get("committed")
        return committed["selection"]["candidate"] if committed else None

    # ----------------------------------------------------------------- requests

    def handle(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            raise UpdaterError("Invalid request")
        operation = request["op"]
        if operation.startswith("system_"):
            if self.system is None:
                raise UpdaterError("System updates are not set up on this host")
            try:
                return self.system.handle(request, lambda: _actor(request.get("actor")))
            except SystemRefused as error:
                raise UpdaterError(str(error)) from None
        if operation == "status":
            return self.status()
        if operation == "review":
            return self.review(_actor(request.get("actor")))
        if operation == "approve":
            return self.approve(request.get("operation"), request.get("digest"), _actor(request.get("actor")))
        if operation == "cancel":
            return self.cancel(request.get("operation"))
        if operation == "restore":
            return self.restore(request.get("operation"), _actor(request.get("actor")))
        if operation == "channel":
            return self.set_channel(request.get("channel"), _actor(request.get("actor")))
        raise UpdaterError("Unknown request")

    # ----------------------------------------------------------------- channel

    def channel(self) -> str:
        """The chosen channel; without a choice, the channel of the installed version."""
        try:
            stored = json.loads(self.channel_path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("channel") in CHANNELS:
                return stored["channel"]
        except (OSError, ValueError):
            pass
        try:
            installed = self._committed()
        except (OSError, ValueError, KeyError, StateError):
            installed = None
        return "stable" if isinstance(installed, dict) and installed.get("kind") == "release-image-candidate" \
            else "development"

    def set_channel(self, channel: object, actor: dict[str, str]) -> dict[str, Any]:
        """Choose what the next check offers; changes no software by itself."""
        if channel not in CHANNELS:
            raise UpdaterError("Channel must be stable or development")
        with self.gate, self._lock:
            if self.state["phase"] in ACTIVE_PHASES:
                raise UpdaterError("Another update operation is in progress")
            write_json(self.channel_path, {"schema_version": 1, "channel": channel,
                                           "changed_at": _now_text(self._clock()), "changed_by": actor})
            if self.state["phase"] in ("up_to_date", "waiting_for_release"):
                # Those results were about the other channel.
                self.state.update(phase="idle", error=None, candidate_revision=None, candidate_version=None)
                self._save()
        return {"accepted": True, "channel": channel}

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._settle()
            state = copy.deepcopy(self.state)
        review = state.get("review")
        if isinstance(review, dict):
            review = dict(review, expires_in_seconds=max(0, int(review["expires_at"] - self._clock())))
        committed = version = None
        try:
            current = self._committed()
            if isinstance(current, dict):
                committed, version = current.get("revision"), current.get("version")
        except (OSError, ValueError, KeyError, StateError):
            committed = None
        return {
            "api_version": 1, "phase": state["phase"], "operation": state.get("operation"),
            "kind": state.get("kind"), "installed_revision": committed, "installed_version": version,
            "channel": self.channel(),
            "candidate_revision": state.get("candidate_revision"),
            "candidate_version": state.get("candidate_version"), "review": review,
            "result": state.get("result"), "error": state.get("error"),
            "restore_available": bool(state.get("restore")) and state["phase"] in ("failed", "outcome_unknown"),
            "backups": _restorable_backups(state),
            "updated_at": state.get("updated_at"), "busy": state["phase"] in ACTIVE_PHASES,
            "progress": self._progress_view(state),
        }

    def _start(self, target: Callable[[], None]) -> None:
        self._thread = threading.Thread(target=target, daemon=True, name="updater-operation")
        self._thread.start()

    def review(self, actor: dict[str, str]) -> dict[str, Any]:
        with self.gate, self._lock:
            if self.state["phase"] in ACTIVE_PHASES:
                raise UpdaterError("Another update operation is in progress")
            if self._system_busy():
                raise UpdaterError("A system update or restart is in progress")
            operation = str(uuid4())
            self.state = {"phase": "resolving", "kind": "update", "operation": operation, "actor": actor,
                          "started_at": _now_text(self._clock()), "review": None, "result": None,
                          "error": None, "restore": None, "candidate_revision": None,
                          # How long each step of the last successful update took: the page's estimate.
                          "timings": _timings(self.state.get("timings"))}
            self._save()
        self._start(lambda: self._run_update(operation))
        return {"accepted": True, "operation": operation}

    def _run_update(self, operation: str) -> None:
        channel = self.channel()
        try:
            candidate = self._resolve(self.spec["gateway"], self.spec["platform"], CHANNELS[channel])
            installed = self._committed()
        except (OSError, ValueError, KeyError, TypeError, StateError) as error:
            logger.warning("Update candidate could not be resolved: %s", error)
            message = ("The newest release could not be resolved on Docker Hub (is a release published yet?)."
                       if channel == "stable" else "The newest image could not be resolved on Docker Hub.")
            self._set(phase="failed_before_interruption", error=message + " Nothing was interrupted.")
            return
        self._set(candidate_revision=candidate.get("revision"), candidate_version=candidate.get("version"))
        if isinstance(installed, dict) and installed.get("revision") == candidate.get("revision"):
            self._set(phase="up_to_date", error=None)
            return
        if channel == "stable" and isinstance(installed, dict):
            waiting = self._release_waits(candidate, installed)
            if waiting is not None:
                self._set(**waiting)
                return
        refusal = self._schema_refusal(candidate)
        if refusal:
            self._set(phase="failed_before_interruption", error=refusal)
            return
        request = {"action": "deploy", "spec": self.spec, "selection": candidate, "operation": operation,
                   "update": True}
        self._execute(request, operation, restoring=False)

    def _bridge(self, operation: str, restoring: bool) -> Callable[[dict[str, Any]], str | None]:
        def approve(event: dict[str, Any]) -> str | None:
            plan, digest = event.get("plan") or {}, event.get("digest")
            if plan.get("operation") != operation or not isinstance(digest, str):
                return None
            expires = self._clock() + REVIEW_SECONDS
            review = {"operation": operation, "digest": digest, "expires_at": expires,
                      "summary": self._summary(plan, restoring)}
            with self._lock:
                self._decision = None
                self.state.update(phase="restore_awaiting_approval" if restoring else "awaiting_approval",
                                  review=review)
                self._save()
                self._lock.notify_all()
                while self._decision is None and self._clock() < expires:
                    self._lock.wait(timeout=min(1.0, max(0.0, expires - self._clock())))
                decision = self._decision or {"approved": False, "reason": "expired"}
                if decision["approved"]:
                    # Dispatch boundary: recorded on disk before the transaction may interrupt anything.
                    self.state.update(phase="restoring" if restoring else "running", review=None,
                                      approved_by=decision["actor"], approved_at=_now_text(self._clock()),
                                      progress=None if restoring else {"operation": operation, "step": 1,
                                                                       "started": {"1": self._clock()}})
                    self._save()
                    return digest
                self.state.update(phase="cancelled", review=None,
                                  error="The review expired." if decision["reason"] == "expired" else "Cancelled.")
                self._save()
                return None
        return approve

    def _summary(self, plan: dict[str, Any], restoring: bool) -> dict[str, Any]:
        selection = plan.get("selection") if isinstance(plan.get("selection"), dict) else {}
        images = selection.get("images") if isinstance(selection.get("images"), dict) else {}
        scope = plan.get("scope") if isinstance(plan.get("scope"), dict) else {}
        return {
            "action": "restore" if restoring else "update",
            "revision": selection.get("revision"),
            "version": selection.get("version"),
            "images": {name: item.get("reference") for name, item in images.items() if isinstance(item, dict)},
            "services": scope.get("services") or [],
            "layout_change": scope.get("layout_change"),
            "interruption": INTERRUPTION,
            "backup": BACKUP_NOTE,
            "cleanup": None if restoring else CLEANUP_NOTE,
            "retention": None if restoring else RETENTION_NOTE,
        }

    def _execute(self, request: dict[str, Any], operation: str, *, restoring: bool,
                 recovery: Path | None = None, checksum: str | None = None) -> None:
        approve = self._bridge(operation, restoring)
        done = threading.Event()
        if not restoring:
            threading.Thread(target=self._watch_progress, args=(operation, done), daemon=True,
                             name="updater-progress").start()
        try:
            result = self._transaction(request, approve, self.evidence,
                                       **({"recovery": recovery, "recovery_checksum": checksum} if restoring else {}))
        except (ChannelError, ComposeError, StateError, OSError, ValueError, subprocess.SubprocessError) as error:
            done.set()
            logger.warning("Update operation %s ended with an error: %s", operation, error)
            with self._lock:
                self.state["progress"] = None
                if self.state["phase"] == "cancelled":
                    self._save()
                    return
                interrupted = self.state["phase"] in ("running", "restoring")
                restore = self._backup_identity(operation) if interrupted and not restoring else self.state.get("restore")
                self.state.update(
                    phase="failed" if interrupted else "failed_before_interruption", review=None, restore=restore,
                    error=("The restore failed; inspect the deployment state on the Pi." if restoring
                           else "The update failed after the services were stopped." if interrupted
                           else "The update stopped before anything was interrupted."))
                self._save()
            return
        done.set()
        checks = result.get("required") if isinstance(result, dict) else None
        with self._lock:
            measured = self._measured(operation)
        self._set(phase="restored" if restoring else "succeeded", review=None, error=None, progress=None,
                  timings=measured or self.state.get("timings"),
                  result={"event": result.get("event"), "checks": checks, "finished_at": _now_text(self._clock()),
                          "cleanup": _cleanup(result.get("cleanup") if isinstance(result, dict) else None),
                          "retention": _retention(result.get("retention") if isinstance(result, dict) else None)},
                  restore=None)

    # ----------------------------------------------------------------- progress

    def _watch_progress(self, operation: str, done: threading.Event) -> None:
        while not done.wait(PROGRESS_INTERVAL):
            self._progress_tick(operation)

    def _progress_tick(self, operation: str) -> None:
        """Advance the step from the Pi's deployment record; never backwards, never concluded otherwise."""
        record, phase = self._target_record(operation)
        with self._lock:
            progress = self.state.get("progress")
            if (self.state.get("phase") != "running" or not isinstance(progress, dict)
                    or progress.get("operation") != operation):
                return
            step = PROGRESS_FROM_PENDING.get(phase or "") if record == "pending" else None
            if record == "clear" and progress["step"] >= 4:
                step = PROGRESS_STEPS  # finalized: only the cleanup is left
            if step is None or step <= progress["step"]:
                return
            for passed in range(progress["step"] + 1, step + 1):
                progress["started"][str(passed)] = self._clock()  # a step seen late or skipped: zero
            progress["step"] = step
            self._save()

    def _measured(self, operation: str) -> list[int] | None:
        """Seconds per step of the update that just succeeded."""
        progress = self.state.get("progress")
        if not isinstance(progress, dict) or progress.get("operation") != operation:
            return None
        now = self._clock()
        starts = [progress["started"].get(str(step)) for step in range(1, PROGRESS_STEPS + 1)]
        ends = [*starts[1:], now]
        result = []
        for index, start in enumerate(starts):
            end = next((value for value in ends[index:] if value is not None), now)
            result.append(max(0, int(end - start)) if start is not None else 0)
        return result

    def _progress_view(self, state: dict[str, Any]) -> dict[str, Any] | None:
        progress = state.get("progress")
        if state.get("phase") != "running" or not isinstance(progress, dict):
            return None
        now, started = self._clock(), progress["started"]
        return {"step": progress["step"], "steps": PROGRESS_STEPS,
                "elapsed_seconds": max(0, int(now - started["1"])),
                "step_elapsed_seconds": max(0, int(now - started[str(progress["step"])])),
                "typical_seconds": _timings(state.get("timings"))}

    def _backup_identity(self, operation: str) -> dict[str, str] | None:
        try:
            value = json.loads(private_read(self.evidence / (operation + ".backup-identity.json")))
        except (OSError, ValueError, ComposeError):
            return None
        if value.get("operation") != operation or not isinstance(value.get("manifest_sha256"), str):
            return None
        identity = {"operation": operation, "manifest_sha256": value["manifest_sha256"],
                    "taken_at": None, "size_bytes": None, "revision": None}
        # When the backup was taken and how big it is: from its own manifest. Its version is
        # the one still recorded as installed, because a failed update is never committed.
        try:
            manifest = json.loads(private_read(self.evidence / operation / "manifest.json"))
            if isinstance(manifest, dict) and manifest.get("operation") == operation:
                if isinstance(manifest.get("created_at"), str):
                    identity["taken_at"] = manifest["created_at"][:40]
                if type(manifest.get("total_bytes")) is int and manifest["total_bytes"] >= 0:
                    identity["size_bytes"] = manifest["total_bytes"]
        except (OSError, ValueError, ComposeError):
            pass
        try:
            current = self._committed()
            identity["revision"] = current.get("revision") if isinstance(current, dict) else None
        except (OSError, ValueError, KeyError, StateError):
            pass
        return identity

    def approve(self, operation: object, digest: object, actor: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            review = self.state.get("review")
            if (self.state["phase"] not in ("awaiting_approval", "restore_awaiting_approval")
                    or not isinstance(review, dict) or operation != review["operation"]):
                raise UpdaterError("There is no pending review for this operation")
            if digest != review["digest"]:
                raise UpdaterError("The confirmation does not match the pending review")
            if self._clock() >= review["expires_at"]:
                raise UpdaterError("The review expired; start a new one")
            self._decision = {"approved": True, "actor": actor}
            self._lock.notify_all()
        return {"accepted": True, "operation": operation}

    def cancel(self, operation: object) -> dict[str, Any]:
        with self._lock:
            review = self.state.get("review")
            if not isinstance(review, dict) or operation != review["operation"]:
                raise UpdaterError("There is no pending review for this operation")
            self._decision = {"approved": False, "reason": "cancelled"}
            self._lock.notify_all()
        return {"accepted": True, "operation": operation}

    def restore(self, operation: object, actor: dict[str, str]) -> dict[str, Any]:
        with self.gate, self._lock:
            if self._system_busy():
                raise UpdaterError("A system update or restart is in progress")
            self._settle()
            restore = self.state.get("restore")
            if (self.state["phase"] not in ("failed", "outcome_unknown") or not isinstance(restore, dict)
                    or operation != restore["operation"]):
                raise UpdaterError("No restore is available for this operation")
            operation_id(restore["operation"])
            self.state.update(phase="resolving", kind="restore", actor=actor, review=None, error=None)
            self._save()
        request = {"action": "recover", "deployment": self.spec["deployment"], "operation": restore["operation"]}
        self._start(lambda: self._execute(request, restore["operation"], restoring=True,
                                          recovery=self.evidence / restore["operation"],
                                          checksum=restore["manifest_sha256"]))
        return {"accepted": True, "operation": restore["operation"]}


# ------------------------------------------------------------------- socket

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        updater: Updater = self.server.updater  # type: ignore[attr-defined]
        line = self.rfile.readline(MAX_REQUEST + 1)
        try:
            if len(line) > MAX_REQUEST:
                raise UpdaterError("Request too large")
            response = {"ok": True, **updater.handle(json.loads(line))}
        except UpdaterError as error:
            response = {"ok": False, "error": str(error)}
        except (ValueError, KeyError, TypeError):
            response = {"ok": False, "error": "Invalid request"}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class UpdaterServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, updater: Updater, client_gid: int | None) -> None:
        if path.exists() or path.is_symlink():
            path.unlink()
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o660)
        if client_gid is not None:
            os.chown(path, 0, client_gid)
        self.updater = updater


def request(path: Path, value: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """One request over the socket; used by the status command and tests."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(json.dumps(value).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    config = json.loads(private_read(path))
    if (not isinstance(config, dict) or set(config) != {"schema_version", "spec", "client_gid"}
            or config["schema_version"] != 1 or not isinstance(config["spec"], str)
            or type(config["client_gid"]) is not int or not 0 < config["client_gid"] < 2 ** 31):
        raise UpdaterError("Updater configuration must contain schema_version 1, spec and client_gid")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("serve", "status"))
    args = parser.parse_args(argv)
    socket_path = Path(UPDATER_SOCKET_DIR) / SOCKET_NAME
    if args.action == "status":
        print(json.dumps(request(socket_path, {"op": "status"}), indent=2))
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if os.geteuid() != 0 or Path(__file__).resolve().parents[1] != TOOLS:
        raise SystemExit("The updater runs as root from the trusted tooling at " + str(TOOLS))
    config = load_config()
    spec = json.loads(private_read(Path(config["spec"])))
    evidence = ROOT / "evidence"
    with checked_directory(evidence, 0):
        pass
    gate = threading.Lock()
    updater = Updater(spec, evidence, ROOT / "state.json", gate=gate)
    updater.system = SystemUpdates(evidence, ROOT / "system.json", deployment_clear=updater.deployment_clear,
                                   other_busy=updater.busy, gate=gate)
    updater.system.start_background()
    with UpdaterServer(socket_path, updater, config["client_gid"]) as server:
        logger.info("Updater listening on %s for deployment %s", socket_path, spec["deployment"])
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
