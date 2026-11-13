"""Explicit restoration of a failed deployment from its independently held backup."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable
from uuid import uuid4

from tool.deployment_backup import FileBackup
from tool.deployment_build import archive_identity, digest_stream
from tool.deployment_channel import portable_backup
from tool.deployment_compose import (
    ComposeDeployment, ComposeError, apply, check_host_writers, encoded, private_read,
    private_write, runtime_checks, specification,
)
from tool.deployment_files import checked_directory
from tool.deployment_state import StateStore, operation_id
from tool.deployment_images import DIGEST


def recovery_assets(path: Path, operation: str, checksum: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Independently verify previous image archives even on an offline controller."""
    manifest = portable_backup(path, operation, checksum)
    entries = {entry["path"]: entry for entry in manifest["entries"] if entry["type"] == "file"}
    def value(name: str) -> dict[str, Any]:
        if name not in entries:
            raise ComposeError("Recovery backup lacks deployment identity assets")
        return json.loads((path / entries[name]["blob"]).read_bytes())
    spec = specification(value("deployment/spec.json"))
    images = value("deployment/images.json")
    baseline = json.loads((path / entries["deployment/baseline.json"]["blob"]).read_bytes())
    expected = {"web", "ser2net"} if spec["gateway"] == "managed" else {"web"}
    if bool(images) != bool(baseline) or (images and set(images) != expected):
        raise ComposeError("Recovery image pair is incomplete")
    services = {"russound-web": "web", "russound-ser2net": "ser2net"}
    original = {services[item["config"]["Labels"]["com.docker.compose.service"]]: item["image"] for item in baseline}
    for name, image in images.items():
        if set(image) != {"tag", "reference", "config", "sha256", "bytes", "archive"}:
            raise ComposeError("Recovery image metadata has unexpected fields")
        if image["archive"] != name + ".tar":
            raise ComposeError("Recovery archive name differs from selected component")
        if (image["tag"] != f"mpdrussoundpi-recovery/{name}:{operation}"
                or not DIGEST.fullmatch(image["reference"]) or not DIGEST.fullmatch(image["config"])
                or image["reference"] != original.get(name)):
            raise ComposeError("Recovery archive differs from the reviewed old container identity")
        entry = entries.get("deployment/" + image["archive"])
        if entry is None:
            raise ComposeError("Recovery image archive is missing")
        with (path / entry["blob"]).open("rb") as source:
            config = archive_identity(source, image, spec["platform"], None)
            source.seek(0)
            digest, size = digest_stream(source)
        if config != image["config"] or digest != image["sha256"] or size != image["bytes"]:
            raise ComposeError("Recovery archive identity differs from captured runtime")
    return spec, images


def recover(storage: dict[str, Path], deployment: str, operation: str, backup_path: Path,
            checksum: str, approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """One explicit failed-operation recovery; never overwrite newer data in place.

    The operator must first establish that remote orchestration has ended.
    Outstanding local Ansible/writer activity is also rejected by the target.
    """
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    state = store.status()
    record = state["deployments"].get(deployment)
    if (not state["blocked"] or record is None or record["pending"] is None
            or record["pending"]["operation"] != operation):
        raise ComposeError("Recovery requires the matching failed operation")
    original = json.loads(json.dumps(record))
    spec, images = recovery_assets(backup_path, operation, checksum)
    manifest = portable_backup(backup_path, operation, checksum)
    if (shutil.disk_usage(storage["restores"]).free < manifest["total_bytes"] + 64 * 1024**2
            or Path(spec["data_dir"]).parent.stat().st_dev != storage["restores"].stat().st_dev):
        raise ComposeError("Recovery needs sufficient isolated space on the live-data filesystem")
    if spec["deployment"] != deployment:
        raise ComposeError("Recovery backup belongs to another deployment")
    recovery_id = str(uuid4())
    plan: dict[str, Any] = {"kind": "failed-deployment-recovery", "deployment": deployment, "operation": operation,
            "recovery": recovery_id, "project": spec["project"], "services": sorted(images),
            "restore_matching_image_and_data": True, "preserve_post_backup_tree": True,
            "previous_runtime": "running" if images else "absent",
            "require_controller": spec["require_controller"], "require_playback": spec["require_playback"]}
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"digest": digest, "plan": plan}) != digest:
        raise ComposeError("Recovery was not approved; live data was not changed")
    if store.status()["deployments"].get(deployment) != original:
        raise ComposeError("Pending operation changed after recovery review")
    # One durable attempt marker outside state; no automatic retry after a crash.
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": original,
                   "backup_manifest_sha256": checksum})
    check_host_writers(Path(spec["data_dir"]), check_files=False)
    restored = storage["restores"] / recovery_id
    # Read baseline config from the verified backup without executing it.
    entry = next(item for item in manifest["entries"] if item["path"] == "deployment/compose.json")
    baseline_file = storage["evidence"] / (recovery_id + ".baseline.json")
    private_write(baseline_file, json.loads((backup_path / entry["blob"]).read_bytes()))
    recovery_spec: dict[str, Any] = {**spec, "compose": str(baseline_file), "adopt": True}
    pending = record["pending"]
    target = ComposeDeployment(recovery_spec, pending["candidate"], storage, operation=recovery_id)
    current = target.own(target.inspect_all())
    if any(item["Config"]["Labels"].get("com.docker.compose.service") not in target.services
           for item in current):
        raise ComposeError("Unexpected project services block recovery")
    target.baseline = target.current_signature(current)
    target.command(target.compose() + ["stop", "--timeout", "30"], timeout=90)
    target.quiescent()
    backup = FileBackup(backup_path.parent, operation, checksum)
    backup.restore_new(restored, verify_quiescent=target.quiescent)
    loaded: dict[str, str] = {}
    for name, image in images.items():
        archive = restored / "deployment" / image["archive"]
        with archive.open("rb") as source:
            archive_identity(source, image, spec["platform"], None)
            source.seek(0)
            result = subprocess.run(target.docker + ["image", "load"], stdin=source,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        if result.returncode:
            raise ComposeError("Recovery image loading failed; keep all evidence and live services stopped")
        actual = json.loads(target.command(["image", "inspect", image["tag"]]))[0]
        if actual["Id"] not in (image["reference"], image["config"]):
            raise ComposeError("Restored Docker image does not match previous root/config identity")
        loaded[name] = actual["Id"]
    target.quiescent()
    # Rename both trees without deleting or overlaying post-backup operator edits.
    live = Path(spec["data_dir"])
    retained = live.with_name(live.name + ".failed-" + recovery_id)
    with checked_directory(live.parent, spec["uid"], private=False) as parent:
        if retained.exists():
            raise ComposeError("Post-failure preservation destination already exists")
        os.rename(live.name, retained.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        os.rename(restored / "data", live.name, dst_dir_fd=parent)
        os.fsync(parent)
    if images:
        apply(target.asset / "compose.json", loaded)
        results = runtime_checks(target, loaded)
    else:
        # A first-install rollback restores prepared data and the original
        # absence of containers; it must not pretend HTTP/hardware were checked.
        ids = [item["Id"] for item in target.own(target.inspect_all())]
        if ids:
            target.command(["container", "rm", *ids])
        if target.own(target.inspect_all()):
            raise ComposeError("First-install recovery could not verify project absence")
        results = {"containers-absent": "passed", "prepared-data-restored": "passed"}
    receipt: dict[str, Any] = {"operation": operation, "review_digest": digest, "required": results}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, operation, receipt)
    return {"event": "recovered", "operation": operation, "recovery": recovery_id, "required": results}


def resume_original(storage: dict[str, Path], deployment: str, operation: str,
                    approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """Resolve a pre-apply failure only when the reviewed old runtime still matches."""
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    record = store.status()["deployments"].get(deployment)
    if (record is None or record["pending"] is None or record["pending"]["operation"] != operation
            or (storage["evidence"] / (operation + ".apply-attempt.json")).exists()):
        raise ComposeError("Original-runtime resolution requires a matching operation with no apply attempt")
    asset = storage["assets"] / operation
    spec = specification(json.loads(private_read(asset / "spec.json")))
    spec["compose"] = str(asset / "compose.json")
    baseline = json.loads(private_read(asset / "baseline.json"))
    if spec["deployment"] != deployment or not baseline:
        raise ComposeError("Pre-apply resolution requires an identifiable previous runtime")
    recovery_id = str(uuid4())
    target = ComposeDeployment(spec, record["pending"]["candidate"], storage, operation=recovery_id)
    current = target.own(target.inspect_all())
    if target.current_signature(current) != baseline:
        raise ComposeError("Runtime differs from the reviewed pre-apply baseline; use verified backup recovery")
    plan: dict[str, Any] = {
        "kind": "resume-reviewed-original-runtime", "deployment": deployment, "operation": operation,
        "recovery": recovery_id, "project": spec["project"], "restore_data": False,
        "services": sorted(target.services), "restart_original_images_only": True,
    }
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"digest": digest, "plan": plan}) != digest:
        raise ComposeError("Original runtime restart was not approved")
    if store.status()["deployments"].get(deployment) != record:
        raise ComposeError("Pending operation changed after approval")
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": record})
    check_host_writers(Path(spec["data_dir"]), check_files=False)
    # Writers may still be healthy; stop only this matching runtime before
    # restarting its exact IDs. Never restore files or discard operator edits.
    target.baseline = baseline
    target.command(target.compose() + ["stop", "--timeout", "30"], timeout=90)
    target.quiescent()
    images = {target.services[item["config"]["Labels"]["com.docker.compose.service"]]: item["image"]
              for item in baseline}
    apply(target.asset / "compose.json", images)
    results = runtime_checks(target, images)
    receipt: dict[str, Any] = {"operation": operation, "review_digest": digest, "required": results}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, operation, receipt)
    return {"event": "recovered", "operation": operation, "recovery": recovery_id, "required": results}
