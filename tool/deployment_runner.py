"""One-shot internal development transaction with explicit platform hooks.

The runner owns ordering, input custody, backup, load, controlled Ansible
completion and finalization. Hooks must implement actual target preflight,
writer control, independent runtime checks and off-host backup verification.
There is deliberately no public apply CLI or automatic rollback.
"""

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from tool.deployment_backup import FileBackup, quiescent
from tool.deployment_completion import run_checked
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_inputs import InputSnapshot, write_file
from tool.deployment_load import load_registry_candidate, load_reserved_bundle, select_bundle
from tool.deployment_local import LOCAL_CANDIDATE
from tool.deployment_state import StateStore, candidate, identity, operation_id, requirements


class RunError(ValueError):
    """The transaction is incomplete or uncertain; no automatic recovery is safe."""


@dataclass(frozen=True)
class RunHooks:
    """Trusted runner integrations, not operator commands or success booleans.

    All void hooks raise on failure and return None. verify_backup must retain
    and independently verify off-host bytes including old runtime image assets.
    apply/check hooks must not alter unapproved components or credentials.
    """

    preflight: Callable[[], None]
    stop_writers: Callable[[], None]
    verify_quiescent: Callable[[], None]
    verify_backup: Callable[[FileBackup], None]
    check_runtime: Callable[[dict[str, str]], dict[str, str]]


def void_result(value: object, boundary: str) -> None:
    if value is not None:
        raise RunError(boundary + " must raise on failure, not return a status value")


class DevelopmentRun:
    """Keep the reviewed object alive through approval and exactly one attempt.

    StateStore must refer to the real target state, not a controller replica.
    This API runs beside that state and its Docker daemon. An optional incoming
    bundle receiver is invoked only after this same operation reserves state.
    It never attaches to another reservation or bootstraps targets itself.
    """

    def __init__(self, *, store: StateStore, deployment: str, host: str, bundle: Path,
                 manifest_sha256: str, gateway: str, platform: str, ansible: Path,
                 playbook: Path, inventory: Path, protected_inputs: dict[str, Path],
                 recovery_sources: dict[str, Path], staging: Path, runtime: Path,
                 backups: Path, evidence: Path, required_apply: list[str],
                 required_checks: list[str], hooks: RunHooks,
                 operation: str | None = None, selection: dict[str, Any] | None = None,
                 receive_bundle: Callable[[str, str], Path] | None = None,
                 scope: dict[str, Any] | None = None, source_owner_uid: int | None = None):
        identity(deployment)
        identity(host)
        requirements(required_apply)
        requirements(required_checks)
        if set(required_apply) & set(required_checks):
            raise RunError("Apply evidence and independent runtime checks need distinct identifiers")
        if store.owner_uid != os.getuid():
            raise RunError("Runner must execute as the target deployment-state owner")
        if {"playbook", "inventory"} & set(protected_inputs):
            raise RunError("Protected inputs cannot replace controlled playbook/inventory identifiers")
        if not recovery_sources:
            raise RunError("Explicit complete recovery sources are required, even for first install")
        self.operation = operation or str(uuid4())
        operation_id(self.operation)
        self._receive_bundle = receive_bundle
        self._source_owner_uid = source_owner_uid
        self._received = receive_bundle is None
        self._store, self._hooks = store, hooks
        self._bundle, self._ansible = bundle, ansible
        self._recovery = copy.deepcopy(recovery_sources)
        self._backups, self._runtime_parent, self._evidence = backups, runtime, evidence
        self._required_apply, self._required_checks = list(required_apply), list(required_checks)
        self._inputs = {"playbook": playbook, "inventory": inventory, **protected_inputs}
        roots = [store.root, bundle, staging, runtime, backups, evidence]
        for index, first in enumerate(roots):
            with checked_directory(first, os.getuid()):
                pass
            if any(first == other or first in other.parents or other in first.parents
                   for other in roots[index + 1:]):
                raise RunError("State, bundle, snapshot, runtime, backup and evidence roots must be separate")
        for name, source in recovery_sources.items():
            identity(name)
            if not source.is_absolute() or source == Path("/") or ".." in source.parts:
                raise RunError("Recovery roots must be explicit absolute paths")
            if any(source == root or source in root.parents or root in source.parents for root in roots):
                raise RunError("Recovery roots must not overlap transaction storage")
        if store.status()["blocked"]:
            raise RunError("Unresolved target state blocks review of another operation")
        selection = copy.deepcopy(selection) if selection is not None else select_bundle(
            bundle, manifest_sha256, gateway, platform)
        candidate(selection)
        metadata = selection["manifest"] if selection["kind"] == LOCAL_CANDIDATE else selection
        if metadata["gateway"] != gateway or metadata["platform"] != platform:
            raise RunError("Candidate layout differs from the reviewed target")
        if selection["kind"] == LOCAL_CANDIDATE and receive_bundle is None:
            if select_bundle(bundle, selection["manifest_sha256"], gateway, platform) != selection:
                raise RunError("Candidate bytes differ from selected artifacts")
        elif selection["kind"] != LOCAL_CANDIDATE and receive_bundle is not None:
            raise RunError("Registry deployments do not receive local image archives")
        void_result(hooks.preflight(), "Preflight")
        self._snapshot = InputSnapshot.create(staging, self.operation, self._inputs)
        self._plan: dict[str, Any] = {
            "schema_version": 1, "kind": "development-run-review", "operation": self.operation,
            "deployment": deployment, "host": host, "selection": selection,
            "required_apply": self._required_apply, "required_checks": self._required_checks,
            "protected_inputs": sorted(protected_inputs), "recovery_roots": sorted(recovery_sources),
            "interrupt_writers": True, "automatic_rollback": False,
            **({"scope": copy.deepcopy(scope)} if scope is not None else {}),
        }
        self._seal = hashlib.sha256(self._encode(self._plan)).hexdigest()
        self._attempted = False

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def review(self) -> dict[str, Any]:
        """Only non-secret scope; sensitive paths/fingerprints remain private."""
        return {"digest": "sha256:" + self._seal, "plan": copy.deepcopy(self._plan)}

    def _verify(self) -> None:
        if (self.operation != self._plan["operation"]
                or hashlib.sha256(self._encode(self._plan)).hexdigest() != self._seal):
            raise RunError("Reviewed deployment intent changed")
        self._snapshot.verify_sources()
        plan = self._plan
        if plan["selection"]["kind"] == LOCAL_CANDIDATE and self._received:
            manifest = plan["selection"]["manifest"]
            if select_bundle(self._bundle, plan["selection"]["manifest_sha256"],
                             manifest["gateway"], manifest["platform"]) != plan["selection"]:
                raise RunError("Reviewed local images changed")

    def execute(self, approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
        """Ask the caller for explicit review approval, then make one attempt.

        A caller accepting review automatically does not constitute human
        authorization. Failure after begin retains reservation/pending, all
        evidence, installed inputs and any backup; no rollback is dispatched.
        """
        if self._attempted:
            raise RunError("This runner already attempted execution; inspect state before any new operation")
        self._verify()
        if approve(self.review()) != "sha256:" + self._seal:
            raise RunError("Deployment was not approved; no reservation or service mutation occurred")
        self._verify()
        void_result(self._hooks.preflight(), "Preflight")
        self._verify()
        self._attempted = True
        plan = self._plan
        deployment, operation, intent = plan["deployment"], self.operation, "sha256:" + self._seal
        selection = plan["selection"]
        with checked_directory(self._evidence, os.getuid()) as directory:
            write_file(directory, operation + ".intent.json", self._encode(plan),
                       owner=os.getuid(), group=os.getgid(), mode=0o400)
        self._store.begin(deployment, operation, selection, intent,
                          self._required_apply + self._required_checks)
        if self._receive_bundle is not None:
            self._bundle = self._receive_bundle(operation, intent)
            self._received = True
        self._verify()
        runtime = self._runtime_parent / operation
        with checked_directory(self._runtime_parent, os.getuid()) as directory:
            os.mkdir(operation, 0o700, dir_fd=directory)
            os.fsync(directory)
        installed: dict[str, Path] = {}
        for name in self._inputs:
            destination = runtime / (name + (".yml" if name in ("playbook", "inventory") else ".input"))
            self._snapshot.install_new(name, destination, owner_uid=os.getuid(), group_gid=os.getgid())
            installed[name] = destination
        self._store.advance(deployment, operation, "inputs-verified")
        self._verify()
        void_result(self._hooks.stop_writers(), "Writer stop")
        quiescent(self._hooks.verify_quiescent)
        self._verify()
        backup = FileBackup.capture(self._backups, operation, self._recovery,
                                    verify_quiescent=self._hooks.verify_quiescent,
                                    source_owner_uid=self._source_owner_uid)
        void_result(self._hooks.verify_backup(backup), "Off-host backup verification")
        backup.verify()
        quiescent(self._hooks.verify_quiescent)
        self._verify()
        # Backup identity is private recovery evidence, never public intent/state.
        with checked_directory(self._evidence, os.getuid()) as directory:
            write_file(directory, operation + ".backup.json",
                       self._encode({"operation": operation, "manifest_sha256": backup.manifest_sha256}),
                       owner=os.getuid(), group=os.getgid(), mode=0o400)
        self._store.advance(deployment, operation, "backup-complete", backup=operation)
        if selection["kind"] == LOCAL_CANDIDATE:
            loaded = load_reserved_bundle(self._store, deployment, operation, intent, selection,
                                          self._bundle, self._evidence)
        else:
            loaded = load_registry_candidate(self._store, deployment, operation, intent, selection,
                                              self._evidence)
        quiescent(self._hooks.verify_quiescent)
        self._verify()
        for name, destination in installed.items():
            self._snapshot.verify_installed(name, destination, owner_uid=os.getuid(), group_gid=os.getgid())
        variables = runtime / "deployment.json"
        with checked_directory(runtime, os.getuid()) as directory:
            write_file(directory, variables.name,
                       self._encode({"deployment_images": loaded, "deployment_operation": operation,
                                     "deployment_inputs": {name: str(path) for name, path in installed.items()
                                                           if name not in ("playbook", "inventory")}}),
                       owner=os.getuid(), group=os.getgid(), mode=0o400)
        with checked_directory(self._evidence, os.getuid()) as directory:
            write_file(directory, operation + ".apply-attempt.json",
                       self._encode({"operation": operation, "intent_digest": intent}),
                       owner=os.getuid(), group=os.getgid(), mode=0o400)
        applied = run_checked(ansible=self._ansible, playbook=installed["playbook"],
                              inventory=installed["inventory"], variables=variables,
                              evidence=self._evidence / (operation + ".completion.jsonl"),
                              operation=operation, host=plan["host"], required=self._required_apply)
        if applied != dict.fromkeys(self._required_apply, "passed"):
            raise RunError("Controlled apply did not verify every required outcome")
        self._store.advance(deployment, operation, "apply-complete")
        checked = self._hooks.check_runtime(copy.deepcopy(loaded))
        if (not is_mapping(checked) or set(checked) != set(self._required_checks)
                or any(result != "passed" for result in checked.values())):
            raise RunError("Independent runtime checks are missing, failed or unexpected")
        self._snapshot.verify_sources()
        for name, destination in installed.items():
            self._snapshot.verify_installed(name, destination, owner_uid=os.getuid(), group_gid=os.getgid())
        self._store.advance(deployment, operation, "checks-complete")
        completion: dict[str, Any] = {
            "operation": operation, "intent_digest": intent, "invocation": "verified-complete",
            "required": {**applied, **dict.fromkeys(self._required_checks, "passed")},
        }
        self._store.finalize(deployment, operation, completion)
        return {"kind": "development-run-complete", "operation": operation,
                "deployment": deployment, "images": loaded, "required": completion["required"]}
