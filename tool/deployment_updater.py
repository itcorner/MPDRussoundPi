"""Updater service on the Pi: web-triggered Docker image updates through the reviewed tooling.

Stage 2 of docs/plans/general_new_user-triggered-image-updates.md. The service runs
as root from the trusted tooling checkout (/opt/mpdrussoundpi-deploy). The web
container reaches it only through a Unix socket in a directory mounted read-only
into the container; the socket is group-restricted to the container's numeric
user. Requests are a fixed vocabulary, one JSON object per connection:

- ``status``: the current or last operation, without secrets.
- ``review``: resolve the newest image candidate of the installation's channel
  (pinned by digest) and start the reviewed transaction, which waits for approval.
- ``approve``: confirm exactly the pending review (operation id and digest) within
  its window. The web application authenticates the administrator first; the
  socket proves the caller is the web container, not which person approved.
- ``cancel``: decline a pending review; nothing has been interrupted yet.
- ``restore``: after a failed update, restore the previous version from the
  backup kept on this Pi, through the reviewed recovery and its own approval.

The transaction is the operator's own: ``controller()`` here and ``serve`` in a
root child process, connected by pipes instead of SSH. Reservation, pull by
digest, quiescence, backup and its verification, old-image export, apply, checks,
recovery and evidence are unchanged code. The backup stays on this Pi (plan
decision 3: not disaster recovery). The operator lock excludes a concurrent SSH
operator run.

Supported layout: one host running the Docker containers (see the plan). Only the
development channel exists so far, because the tooling resolves only ``dev``.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from mpdrussoundpi.common.atomicjson import write_json
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import UPDATER_SOCKET_DIR, ComposeError, private_read
from tool.deployment_files import checked_directory
from tool.deployment_state import StateError, StateStore, identity, operation_id

TOOLS = Path("/opt/mpdrussoundpi-deploy")
CONFIG = Path("/etc/mpdrussoundpi-updater/updater.json")
ROOT = Path("/var/lib/mpdrussoundpi-updater")
SOCKET_NAME = "updater.sock"
REVIEW_SECONDS = 120
MAX_REQUEST = 64 * 1024
ACTIVE_PHASES = ("resolving", "awaiting_approval", "approved", "running", "restore_awaiting_approval",
                 "restoring")
INTERRUPTION = ("The web application and the serial gateway stop and restart; the page loses its connection "
                "for a few minutes and reconnects by itself.")
BACKUP_NOTE = "The backup is kept on this Pi. It is not disaster recovery: it shares the Pi's storage."

logger = logging.getLogger("mpdrussoundpi.updater")


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
                 resolve: Callable[[str, str], dict[str, Any]] | None = None,
                 committed: Callable[[], dict[str, Any] | None] | None = None,
                 transaction: Callable[..., dict[str, Any]] = run_transaction,
                 clock: Callable[[], float] = time.time) -> None:
        identity(spec["deployment"])
        self.spec = spec
        self.evidence = evidence
        self.state_path = state_path
        self._resolve = resolve or self._resolve_registry
        self._committed = committed or self._committed_from_store
        self._transaction = transaction
        self._clock = clock
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
                         error="The updater restarted during the operation; inspect the deployment state.")
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

    def _resolve_registry(self, gateway: str, platform: str) -> dict[str, Any]:
        from tool.deployment_images import resolve
        return resolve(gateway, platform)

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
        raise UpdaterError("Unknown request")

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self.state)
        review = state.get("review")
        if isinstance(review, dict):
            review = dict(review, expires_in_seconds=max(0, int(review["expires_at"] - self._clock())))
        committed = None
        try:
            current = self._committed()
            committed = current.get("revision") if isinstance(current, dict) else None
        except (OSError, ValueError, KeyError, StateError):
            committed = None
        return {
            "api_version": 1, "phase": state["phase"], "operation": state.get("operation"),
            "kind": state.get("kind"), "installed_revision": committed,
            "candidate_revision": state.get("candidate_revision"), "review": review,
            "result": state.get("result"), "error": state.get("error"),
            "restore_available": bool(state.get("restore")) and state["phase"] in ("failed", "outcome_unknown"),
            "updated_at": state.get("updated_at"), "busy": state["phase"] in ACTIVE_PHASES,
        }

    def _start(self, target: Callable[[], None]) -> None:
        self._thread = threading.Thread(target=target, daemon=True, name="updater-operation")
        self._thread.start()

    def review(self, actor: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            if self.state["phase"] in ACTIVE_PHASES:
                raise UpdaterError("Another update operation is in progress")
            operation = str(uuid4())
            self.state = {"phase": "resolving", "kind": "update", "operation": operation, "actor": actor,
                          "started_at": _now_text(self._clock()), "review": None, "result": None,
                          "error": None, "restore": None, "candidate_revision": None}
            self._save()
        self._start(lambda: self._run_update(operation))
        return {"accepted": True, "operation": operation}

    def _run_update(self, operation: str) -> None:
        try:
            candidate = self._resolve(self.spec["gateway"], self.spec["platform"])
            installed = self._committed()
        except (OSError, ValueError, KeyError, StateError) as error:
            logger.warning("Update candidate could not be resolved: %s", error)
            self._set(phase="failed_before_interruption", error="The newest image could not be resolved on Docker Hub.")
            return
        self._set(candidate_revision=candidate.get("revision"))
        if isinstance(installed, dict) and installed.get("revision") == candidate.get("revision"):
            self._set(phase="up_to_date", error=None)
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
                                      approved_by=decision["actor"], approved_at=_now_text(self._clock()))
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
            "images": {name: item.get("reference") for name, item in images.items() if isinstance(item, dict)},
            "services": scope.get("services") or [],
            "layout_change": scope.get("layout_change"),
            "interruption": INTERRUPTION,
            "backup": BACKUP_NOTE,
        }

    def _execute(self, request: dict[str, Any], operation: str, *, restoring: bool,
                 recovery: Path | None = None, checksum: str | None = None) -> None:
        approve = self._bridge(operation, restoring)
        try:
            result = self._transaction(request, approve, self.evidence,
                                       **({"recovery": recovery, "recovery_checksum": checksum} if restoring else {}))
        except (ChannelError, ComposeError, StateError, OSError, ValueError, subprocess.SubprocessError) as error:
            logger.warning("Update operation %s ended with an error: %s", operation, error)
            with self._lock:
                if self.state["phase"] == "cancelled":
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
        checks = result.get("required") if isinstance(result, dict) else None
        self._set(phase="restored" if restoring else "succeeded", review=None, error=None,
                  result={"event": result.get("event"), "checks": checks, "finished_at": _now_text(self._clock())},
                  restore=None)

    def _backup_identity(self, operation: str) -> dict[str, str] | None:
        try:
            value = json.loads(private_read(self.evidence / (operation + ".backup-identity.json")))
        except (OSError, ValueError, ComposeError):
            return None
        if value.get("operation") != operation or not isinstance(value.get("manifest_sha256"), str):
            return None
        return {"operation": operation, "manifest_sha256": value["manifest_sha256"]}

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
        with self._lock:
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
    updater = Updater(spec, evidence, ROOT / "state.json")
    with UpdaterServer(socket_path, updater, config["client_gid"]) as server:
        logger.info("Updater listening on %s for deployment %s", socket_path, spec["deployment"])
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
