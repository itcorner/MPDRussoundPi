"""Internal transaction ordering for a reviewed native-to-Docker handover.

Platform hooks must independently validate and perform the described boundaries.
This module is not an operator entry point and is not sufficient for a cutover.
"""

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, TypeVar

from tool.deployment_compose import encoded, private_read, private_write
from tool.deployment_native_source import NativeError
from tool.deployment_pi_handover import prepare
from tool.deployment_runner import void_result
from tool.deployment_state import StateStore, identity, operation_id


CHECKS = ("containers", "http", "backend", "playback")
T = TypeVar("T")


@dataclass(frozen=True)
class HandoverHooks:
    preflight: Callable[[], None]
    receive_images: Callable[[str], None]
    stop_native: Callable[[], None]
    capture_backup: Callable[[], None]
    load_images: Callable[[str], dict[str, str]]
    apply: Callable[[dict[str, str]], None]
    checks: Callable[[dict[str, str]], dict[str, str]]


def execute(store: StateStore, storage: dict[str, Path], deployment: str, operation: str, *,
            committed: dict[str, Any], native_spec: dict[str, Any], selection: dict[str, Any],
            engine: str, hooks: HandoverHooks,
            approve: Callable[[dict[str, Any]], str | None],
            offhost_free_bytes_required: int = 0, playback_subnet: str | None = None) -> dict[str, Any]:
    """Run one approved transaction; failures retain all existing state and evidence."""
    identity(deployment)
    operation_id(operation)
    committed = copy.deepcopy(committed)
    native_spec = copy.deepcopy(native_spec)
    selection = copy.deepcopy(selection)
    if (store.owner_uid != os.getuid() or not engine
            or type(offhost_free_bytes_required) is not int or offhost_free_bytes_required < 0):
        raise NativeError("Handover requires the state owner and a reviewed Docker daemon")
    initial = store.status()
    if (initial["blocked"] or initial["deployments"].get(deployment, {}).get("committed") != committed
            or native_spec.get("deployment") != deployment):
        raise NativeError("Handover requires the unblocked reviewed native baseline")
    project = "pi-handover-" + operation.replace("-", "")
    spec, config = prepare(native_spec, committed["selection"]["candidate"], selection,
                           project=project, compose_path=str(storage["runtime"] / operation / "compose.json"),
                           playback_subnet=playback_subnet)
    plan: dict[str, Any] = {
        "kind": "pi-handover-transaction", "deployment": deployment, "operation": operation,
        "committed": copy.deepcopy(committed), "selection": copy.deepcopy(selection),
        "native_spec": copy.deepcopy(native_spec), "compose_spec": spec, "compose_config": config,
        "engine": engine, "interrupt_native": True, "preserve_data_in_place": True,
        "offhost_backup_required": True, "automatic_rollback": False,
        "offhost_free_bytes_required": offhost_free_bytes_required,
    }
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()

    def unchanged() -> None:
        if hashlib.sha256(encoded(plan)).hexdigest() != digest.removeprefix("sha256:"):
            raise NativeError("Reviewed handover scope changed")

    def timed(name: str, action: Callable[[], T]) -> T:
        start = time.monotonic()
        try:
            return action()
        finally:
            print(f"handover timing: {name} {time.monotonic() - start:.3f}s", file=sys.stderr, flush=True)

    def pending_at(phase: str) -> dict[str, Any]:
        state = store.status()
        record = state["deployments"].get(deployment)
        if (state["reservation"] != {"schema_version": 1, "deployment": deployment, "operation": operation}
                or record is None or record["committed"] != committed or record["pending"] is None
                or record["pending"]["operation"] != operation or record["pending"]["candidate"] != selection
                or record["pending"]["intent_digest"] != digest or record["pending"]["phase"] != phase):
            raise NativeError("Handover state changed during the transaction")
        return state

    void_result(timed("preflight", hooks.preflight), "Handover preflight")
    unchanged()
    if approve({"plan": copy.deepcopy(plan), "digest": digest}) != digest:
        raise NativeError("Handover was declined; services were not changed")
    unchanged()
    void_result(timed("post-approval preflight", hooks.preflight), "Handover preflight")
    if store.status() != initial:
        raise NativeError("Handover state changed after review")
    private_write(storage["evidence"] / (operation + ".intent.json"), plan)
    store.begin(deployment, operation, selection, digest, ["apply", *CHECKS], expected_committed=committed)
    store.advance(deployment, operation, "inputs-verified")
    store.start_image_transfer(deployment, operation, selection, digest)
    void_result(timed("image transfer", lambda: hooks.receive_images(digest)), "Handover image transfer")
    store.complete_image_transfer(deployment, operation, selection, digest)
    if not pending_at("inputs-verified")["image_transfer_verified"]:
        raise NativeError("Handover image transfer was not verified")
    void_result(timed("native fencing", hooks.stop_native), "Native fencing")
    pending_at("inputs-verified")
    void_result(timed("backup and off-host custody", hooks.capture_backup), "Handover backup")
    state = pending_at("backup-complete")
    pending = state["deployments"][deployment]["pending"]
    if pending is None or pending["phase"] != "backup-complete" or pending["backup"] != operation:
        raise NativeError("Handover backup did not record the matching verified backup")
    images = timed("image load", lambda: hooks.load_images(digest))
    state = pending_at("backup-complete")
    pending = state["deployments"][deployment]["pending"]
    if (pending is None or pending["loaded_images"] != images or not state["image_load_started"]
            or pending["phase"] != "backup-complete"):
        raise NativeError("Handover image load did not record the verified identities")
    runtime = storage["runtime"] / operation
    runtime.mkdir(mode=0o700)
    private_write(runtime / "compose.json", config)
    private_write(storage["evidence"] / (operation + ".handover-activation.json"),
                  {"operation": operation, "intent_digest": digest, "project": project,
                   "engine": engine, "images": images,
                   "config_digest": "sha256:" + hashlib.sha256(encoded(config)).hexdigest()})
    private_write(storage["evidence"] / (operation + ".apply-attempt.json"),
                  {"operation": operation, "intent_digest": digest})
    void_result(timed("Compose activation", lambda: hooks.apply(copy.deepcopy(images))), "Handover apply")
    pending_at("backup-complete")
    active = copy.deepcopy(config)
    for service, component in (("russound-web", "web"), ("russound-ser2net", "ser2net")):
        active["services"][service]["image"] = images[component]
    if json.loads(private_read(runtime / "active.json")) != active:
        raise NativeError("Activated Compose scope differs from the verified images")
    store.advance(deployment, operation, "apply-complete")
    checked = timed("runtime acceptance", lambda: hooks.checks(copy.deepcopy(images)))
    if checked != dict.fromkeys(CHECKS, "passed"):
        raise NativeError("Handover runtime checks are incomplete")
    unchanged()
    pending_at("apply-complete")
    if json.loads(private_read(runtime / "active.json")) != active:
        raise NativeError("Activated Compose scope changed during checks")
    store.advance(deployment, operation, "checks-complete")
    receipt: dict[str, Any] = {"operation": operation, "intent_digest": digest,
                               "invocation": "verified-complete",
                               "required": {"apply": "passed", **checked}}
    store.finalize(deployment, operation, receipt)
    return {"event": "complete", "operation": operation, "deployment": deployment,
            "images": images, "required": receipt["required"]}
