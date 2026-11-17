"""Preparation, backup and recovery primitives for a Pi native-to-Compose handover.

Service fencing, backup capture and isolated recovery preparation require an
existing reservation and verified inputs. Their output is not authorization
for activation or live restoration.
"""

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from tool.deployment_backup import FileBackup, quiescent
from tool.deployment_build import local_engine
from tool.deployment_channel import Channel, ChannelError, portable_backup, receive_backup, send_backup
from tool.deployment_compose import encoded, private_read, private_write, specification as compose_specification
from tool.deployment_files import is_list, is_mapping
from tool.deployment_local import LOCAL_CANDIDATE
from tool import deployment_native as native
from tool.deployment_native import environment, specification as native_specification
from tool.deployment_native_source import NativeError, validate_candidate as native_candidate
from tool.deployment_preflight import check_data, check_port, overlap, safe_path
from tool.deployment_state import StateStore, candidate, completion_record, identity, operation_id, pending_record


def prepare(native_spec: dict[str, Any], native_selection: dict[str, Any],
            image_selection: dict[str, Any], *, project: str,
            compose_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive a same-revision managed layout; callers must verify its provenance."""
    spec = native_specification(native_spec)
    native_candidate(native_selection)
    candidate(image_selection)
    identity(project)
    if image_selection["kind"] != LOCAL_CANDIDATE:
        raise NativeError("The one-time handover requires a verified local image bundle")
    manifest = image_selection["manifest"]
    platform = {"aarch64": "linux/arm64", "x86_64": "linux/amd64"}[
        native_selection["runtime"]["machine"]]
    if (manifest["gateway"] != "managed" or manifest["platform"] != platform
            or manifest["revision"] != native_selection["revision"]):
        raise NativeError("Handover images must match the native source revision and host platform")
    # Validate that the existing native secret references have the same bind mapping.
    environment(spec, native=True)
    for agent in spec["playback_agents"]:
        url: str = agent["url"]
        host = urlsplit(url).hostname
        if host is None or host.lower() == "localhost":
            raise NativeError("Loopback playback agents are not reachable from a bridge container")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_loopback or address.is_unspecified):
            raise NativeError("Loopback playback agents are not reachable from a bridge container")
    compose_spec = compose_specification({
        "schema_version": 1, "deployment": spec["deployment"], "project": project,
        "compose": compose_path, "data_dir": spec["data_dir"], "uid": spec["uid"], "gid": spec["gid"],
        "gateway": "managed", "platform": platform, "publish_host": spec["publish_host"],
        "publish_port": spec["publish_port"], "serial_device": spec["serial_device"],
        "serial_gid": spec["serial_gid"], "require_controller": True,
        "require_playback": True, "adopt": False,
    })
    images = manifest["images"]
    root = Path(spec["data_dir"])
    mounts: list[dict[str, Any]] = [
        {"type": "bind", "source": str(root / child), "target": target,
         "read_only": child == "secrets", "bind": {"create_host_path": False}}
        for child, target in (("config", "/config"), ("data", "/data"),
                              ("logs", "/logs"), ("secrets", "/run/secrets"))
    ]
    common = {"platform": platform, "restart": "unless-stopped",
              "user": f'{spec["uid"]}:{spec["gid"]}'}
    config: dict[str, Any] = {
        "name": project,
        "services": {
            "russound-web": {
                **common, "image": images["web"]["reference"],
                "stop_grace_period": "30s",
                "ports": [{"host_ip": spec["publish_host"], "target": 8000,
                           "published": str(spec["publish_port"]), "protocol": "tcp"}],
                "volumes": mounts,
                "environment": {
                    "RUSSOUND_BACKEND_HOST": "russound-ser2net",
                    "RUSSOUND_BACKEND_PORT": "6666",
                    "RUSSOUND_PLAYBACK_AGENTS": json.dumps(spec["playback_agents"],
                                                           sort_keys=True, separators=(",", ":")),
                    "RUSSOUND_DEBUG": "false",
                },
                "depends_on": {"russound-ser2net": {"condition": "service_healthy"}},
            },
            "russound-ser2net": {
                **common, "image": images["ser2net"]["reference"],
                "group_add": [str(spec["serial_gid"])],
                "devices": [{"source": spec["serial_device"], "target": spec["serial_device"],
                             "permissions": "rwm"}],
                "environment": {"RUSSOUND_SERIAL_DEVICE": spec["serial_device"],
                                "RUSSOUND_SERIAL_OPTIONS": spec["serial_options"],
                                "RUSSOUND_SER2NET_BIND": "0.0.0.0", "RUSSOUND_SER2NET_PORT": "6666"},
            },
        },
    }
    if "${" in json.dumps(config):
        raise NativeError("Handover inputs must not contain Compose interpolation expressions")
    return copy.deepcopy(compose_spec), config


def verify_backup(path: Path, operation: str, checksum: str, *,
                  committed: dict[str, Any], native_spec: dict[str, Any],
                  image_selection: dict[str, Any], native_asset: Path) -> dict[str, Any]:
    """Verify a fresh handover backup against independently retained review inputs."""
    operation_id(operation)
    if set(committed) != {"selection", "completion"}:
        raise NativeError("Handover backup requires an independently retained committed native record")
    pending_record(committed["selection"])
    completion_record(committed["completion"], committed["selection"])
    previous = committed["selection"]
    if previous["phase"] != "checks-complete":
        raise NativeError("Handover backup requires a completed native baseline")
    selected = previous["candidate"]
    native_candidate(selected)
    spec = native_specification(native_spec)
    prepare(spec, selected, image_selection, project=spec["deployment"], compose_path="/handover-compose.json")
    previous_operation = previous["operation"]
    expected_sources: dict[str, str] = {
        "data": spec["data_dir"],
        "native-asset": str(native_asset),
        "previous-release": str(native.RELEASES / selected["release"]),
        "previous-runtime": str(native.RUNTIME / previous_operation),
    }
    if (not native_asset.is_absolute() or ".." in native_asset.parts
            or native_asset.name != previous_operation):
        raise NativeError("Handover backup native asset path differs from the committed operation")
    manifest = portable_backup(path, operation, checksum)
    if manifest["sources"] != expected_sources:
        raise NativeError("Handover backup roots differ from the independently retained native baseline")
    entries = {item["path"]: item for item in manifest["entries"] if item["type"] == "file"}

    def content(name: str) -> bytes:
        entry = entries.get(name)
        if entry is None:
            raise NativeError("Handover backup is missing native recovery assets")
        return (path / entry["blob"]).read_bytes()

    if (json.loads(content("native-asset/spec.json")) != spec
            or json.loads(content("native-asset/selection.json")) != selected):
        raise NativeError("Backed-up native inputs differ from the committed baseline")
    for name, value in native.unit_files(spec, selected, previous_operation).items():
        if content("native-asset/" + name) != value:
            raise NativeError("Backed-up native unit differs from the committed baseline")
    if content("previous-runtime/ser2net.yaml") != native.gateway_config(spec):
        raise NativeError("Backed-up native gateway configuration differs from the committed baseline")
    inventory: list[dict[str, Any]] = []
    for item in sorted((item for item in manifest["entries"]
                        if item["path"].split("/")[0] == "previous-release"),
                       key=lambda item: Path(item["path"]).parts[1:]):
        entry: dict[str, Any] = {"path": "/".join(Path(item["path"]).parts[1:]) or ".", "mode": item["mode"]}
        if item["type"] == "file":
            entry["sha256"] = item["sha256"]
        inventory.append(entry)
    digest = hashlib.sha256(b"".join((json.dumps(item, sort_keys=True) + "\n").encode()
                                     for item in inventory)).hexdigest()
    if digest != selected["sha256"]:
        raise NativeError("Backed-up native release differs from the committed source/environment")
    return manifest


def fence_native(store: StateStore, storage: dict[str, Path], deployment: str, operation: str, *,
                 committed: dict[str, Any], native_spec: dict[str, Any],
                 image_selection: dict[str, Any]) -> None:
    """Disable the exact reserved native units; failures retain the reservation.

    The caller must approve the complete scope and check Docker and other host
    conflicts independently before calling this one-shot internal boundary.
    """
    identity(deployment)
    operation_id(operation)
    spec = native_specification(native_spec)
    state = store.status()
    record = state["deployments"].get(deployment)
    if (spec["deployment"] != deployment or not state["blocked"]
            or state["reservation"] != {"schema_version": 1, "deployment": deployment, "operation": operation}
            or record is None or record["committed"] != committed
            or record["pending"] is None or record["pending"]["operation"] != operation
            or record["pending"]["phase"] != "inputs-verified"
            or record["pending"]["candidate"] != image_selection):
        raise NativeError("Native fencing requires the matching reserved handover and committed baseline")
    previous = committed["selection"]
    selected = previous["candidate"]
    prepare(spec, selected, image_selection, project=deployment, compose_path="/handover-compose.json")
    asset = storage["assets"] / previous["operation"]
    if (json.loads(private_read(asset / "spec.json")) != spec
            or json.loads(private_read(asset / "selection.json")) != selected):
        raise NativeError("Native fencing inputs differ from the committed assets")
    native.runtime_checks(spec, selected, previous["operation"])
    native.check_host_writers(Path(spec["data_dir"]), check_files=False)
    if store.status() != state:
        raise NativeError("Handover state changed before native fencing")
    private_write(storage["evidence"] / (operation + ".fence-attempt.json"),
                  {"operation": operation, "native_operation": previous["operation"],
                   "intent_digest": record["pending"]["intent_digest"]})
    native.command(["systemctl", "disable", "--now", native.WEB, native.GATEWAY], timeout=90)
    native.verify_units(native.unit_files(spec, selected, previous["operation"]))
    units = native.system_units()
    if any(item.get("UnitFileState") != "disabled" for item in units.values()):
        raise NativeError("Native units are not disabled; inspect before recovery")
    native.check_no_writers(spec)
    check_port(spec["publish_host"], spec["publish_port"])
    check_port("127.0.0.1", spec["backend_port"])
    if store.status() != state:
        raise NativeError("Handover state changed while native units were fenced")


def check_docker_conflicts(spec: dict[str, Any], *, expected_engine: str | None = None) -> str:
    """Reject conflicting containers and bind inspection to one Docker daemon."""
    docker = ["docker", "--context", local_engine()]

    def engine_id() -> str:
        info: Any = json.loads(native.command(docker + ["info", "--format", "{{json .}}"]))
        if not is_mapping(info) or not isinstance(info.get("ID"), str) or not info["ID"]:
            raise NativeError("Cannot establish Docker daemon identity")
        return info["ID"]

    engine = engine_id()
    if expected_engine is not None and engine != expected_engine:
        raise NativeError("Docker daemon changed during native recovery")
    ids = native.command(docker + ["ps", "--all", "--quiet", "--no-trunc"]).decode().split()
    containers: Any = json.loads(native.command(docker + ["inspect", *ids])) if ids else []
    if not is_list(containers) or len(containers) != len(ids) or len(set(ids)) != len(ids):
        raise NativeError("Cannot establish Docker container identities")
    inspected: set[str] = set()
    for item in containers:
        if (not is_mapping(item) or not is_mapping(item.get("HostConfig"))
                or not is_mapping(item.get("State")) or not is_list(item.get("Mounts"))
                or not is_mapping(item["HostConfig"].get("RestartPolicy"))
                or not isinstance(item.get("Id"), str) or item["Id"] not in ids
                or item["Id"] in inspected
                or any(type(item["State"].get(key)) is not bool for key in ("Running", "Restarting", "Paused"))
                or not isinstance(item["HostConfig"]["RestartPolicy"].get("Name"), str)):
            raise NativeError("Invalid Docker container metadata blocks native resume")
        inspected.add(item["Id"])
        mounts: list[Any] = item["Mounts"]
        devices: Any = item["HostConfig"].get("Devices")
        if devices is None:
            devices = []
        if (not is_list(devices) or any(not is_mapping(mount) for mount in mounts)
                or any(not is_mapping(device) for device in devices)
                or any(mount.get("Type") == "bind" and not isinstance(mount.get("Source"), str)
                       for mount in mounts)
                or any(not isinstance(device.get("PathOnHost"), str) for device in devices)):
            raise NativeError("Invalid Docker mounts or devices block native resume")
        related = (any(mount.get("Type") == "bind" and
                       overlap(Path(mount["Source"]), Path(spec["data_dir"])) for mount in mounts)
                   or any(device["PathOnHost"] == spec["serial_device"] for device in devices))
        if related and (item["State"]["Running"] or item["State"]["Restarting"]
                        or item["State"]["Paused"] or item["HostConfig"]["RestartPolicy"]["Name"] == "always"):
            raise NativeError("A Docker container can own native data or the serial adapter")
    if engine_id() != engine:
        raise NativeError("Docker daemon changed during conflict inspection")
    return engine


def resume_before_backup(storage: dict[str, Path], deployment: str, operation: str,
                         approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """Resume the captured native runtime without replacing data before image activation."""
    identity(deployment)
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    state = store.status()
    record = state["deployments"].get(deployment)
    if (not state["blocked"] or state["reservation"] != {
            "schema_version": 1, "deployment": deployment, "operation": operation}
            or record is None or record["committed"] is None or record["pending"] is None
            or record["pending"]["operation"] != operation
            or record["pending"]["phase"] != "inputs-verified"
            or state["image_load_started"]
            or (storage["evidence"] / (operation + ".apply-attempt.json")).exists()):
        raise NativeError("Native handover resume requires the matching pre-backup operation without image activation")
    previous = record["committed"]["selection"]
    asset = storage["assets"] / previous["operation"]
    spec = native_specification(json.loads(private_read(asset / "spec.json")))
    selected = json.loads(private_read(asset / "selection.json"))
    if spec["deployment"] != deployment or selected != previous["candidate"]:
        raise NativeError("Native handover resume differs from the committed assets")
    prepare(spec, selected, record["pending"]["candidate"], project=deployment,
            compose_path="/handover-compose.json")
    native.verify_units(native.unit_files(spec, selected, previous["operation"]))
    native.verify_release(selected)
    gateway = native.RUNTIME / previous["operation"] / "ser2net.yaml"

    def verify_gateway() -> None:
        info = safe_path(gateway)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or gateway.read_bytes() != native.gateway_config(spec):
            raise NativeError("Native gateway configuration changed; resume requires investigation")

    verify_gateway()
    check_data(native.data_spec(spec))
    engine = check_docker_conflicts(spec)
    recovery = str(uuid4())
    plan: dict[str, Any] = {
        "kind": "pi-handover-resume-original", "deployment": deployment, "operation": operation,
        "recovery": recovery, "native_operation": previous["operation"], "restore_data": False,
        "preserve_operator_edits": True, "audio_agent_proxy_and_os": "untouched",
        "data_dir": spec["data_dir"], "uid": spec["uid"], "gid": spec["gid"],
        "serial_device": spec["serial_device"], "serial_options": spec["serial_options"],
        "publish_host": spec["publish_host"], "publish_port": spec["publish_port"],
        "backend_port": spec["backend_port"], "revision": selected["revision"],
        "native_selection_digest": "sha256:" + hashlib.sha256(encoded(selected)).hexdigest(),
        "native_spec_digest": "sha256:" + hashlib.sha256(encoded(spec)).hexdigest(),
        "pending_intent_digest": record["pending"]["intent_digest"],
    }
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"plan": plan, "digest": digest}) != digest:
        raise NativeError("Native handover resume was declined; services were not changed")
    if store.status() != state:
        raise NativeError("Handover state changed after resume approval")
    if (json.loads(private_read(asset / "spec.json")) != spec
            or json.loads(private_read(asset / "selection.json")) != selected):
        raise NativeError("Native handover assets changed after resume approval")
    native.verify_units(native.unit_files(spec, selected, previous["operation"]))
    native.verify_release(selected)
    verify_gateway()
    check_data(native.data_spec(spec))
    check_docker_conflicts(spec, expected_engine=engine)
    native.check_host_writers(Path(spec["data_dir"]), check_files=False)
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": record})
    native.stop_native(spec, selected, previous["operation"], {"previous_native": None})
    native.check_no_writers(spec)
    check_docker_conflicts(spec, expected_engine=engine)
    native.command(["systemctl", "enable", "--now", native.GATEWAY, native.WEB], timeout=90)
    native.runtime_checks(spec, selected, previous["operation"])
    check_docker_conflicts(spec, expected_engine=engine)
    if store.status() != state:
        raise NativeError("Handover state changed during native resume checks")
    receipt: dict[str, Any] = {"operation": operation, "review_digest": digest,
                               "required": {"original-runtime": "passed", "data-preserved": "passed"}}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, operation, receipt, restore_native=True,
                           native_previous=record["committed"], handover_resume=True)
    return {"event": "recovered", "operation": operation, "recovery": recovery, "required": receipt["required"]}


def capture_backup(store: StateStore, storage: dict[str, Path], deployment: str, operation: str, *,
                   committed: dict[str, Any], native_spec: dict[str, Any],
                   image_selection: dict[str, Any], native_asset: Path,
                   verify_quiescent: Callable[[], None],
                   verify_offhost: Callable[[FileBackup], None]) -> FileBackup:
    """Capture and acknowledge a handover backup after callers have stopped writers.

    The caller must fence services before calling and independently verify the
    transferred backup in verify_offhost. This function never stops services,
    performs recovery, or releases a reservation.
    """
    identity(deployment)
    operation_id(operation)
    spec = native_specification(native_spec)
    if spec["deployment"] != deployment:
        raise NativeError("Handover backup deployment differs from the reviewed scope")

    def check_state() -> None:
        state = store.status()
        record = state["deployments"].get(deployment)
        if (not state["blocked"] or state["reservation"] != {
                "schema_version": 1, "deployment": deployment, "operation": operation}
                or record is None or record["committed"] != committed
                or record["pending"] is None or record["pending"]["operation"] != operation
                or record["pending"]["candidate"] != image_selection
                or record["pending"]["phase"] != "inputs-verified"):
            raise NativeError("Handover backup requires the matching reserved native baseline and image candidate")

    check_state()
    previous = committed["selection"]
    prepare(spec, previous["candidate"], image_selection, project=deployment,
            compose_path="/handover-compose.json")
    if native_asset != storage["assets"] / previous["operation"]:
        raise NativeError("Handover backup must use the recorded native asset directory")
    sources: dict[str, Path] = {"data": Path(spec["data_dir"]), "native-asset": native_asset,
                               "previous-release": native.RELEASES / previous["candidate"]["release"],
                               "previous-runtime": native.RUNTIME / previous["operation"]}
    quiescent(verify_quiescent)
    backup = FileBackup.capture(storage["backups"], operation, sources,
                                verify_quiescent=verify_quiescent, source_owner_uid=spec["uid"])
    verify_backup(backup.path, operation, backup.manifest_sha256, committed=committed,
                  native_spec=spec, image_selection=image_selection, native_asset=native_asset)
    if verify_offhost(backup) is not None:
        raise NativeError("Off-host verification must raise on failure, not return a status")
    backup.verify()
    quiescent(verify_quiescent)
    check_state()
    private_write(storage["evidence"] / (operation + ".backup.json"),
                  {"operation": operation, "manifest_sha256": backup.manifest_sha256})
    store.advance(deployment, operation, "backup-complete", backup=operation)
    return backup


def prepare_backup_restore(store: StateStore, storage: dict[str, Path], deployment: str, operation: str, *,
                           backup_path: Path, checksum: str, committed: dict[str, Any],
                           native_spec: dict[str, Any], image_selection: dict[str, Any],
                           native_asset: Path, recovery: str,
                           verify_quiescent: Callable[[], None]) -> Path:
    """Verify a matching backup and restore only to a new isolated tree.

    The caller must independently retain the checksum, fence all writers, and
    keep them fenced until any separately approved live restoration completes.
    No live paths, services or deployment state are modified here.
    """
    identity(deployment)
    operation_id(operation)
    operation_id(recovery)
    spec = native_specification(native_spec)
    if spec["deployment"] != deployment or native_asset != storage["assets"] / committed["selection"]["operation"]:
        raise NativeError("Restore inputs differ from the committed native deployment")

    original_state = store.status()

    def check_state() -> None:
        state = store.status()
        record = state["deployments"].get(deployment)
        if (state != original_state
                or state["reservation"] != {"schema_version": 1, "deployment": deployment, "operation": operation}
                or record is None or record["committed"] != committed or record["pending"] is None
                or record["pending"]["operation"] != operation
                or record["pending"]["candidate"] != image_selection
                or record["pending"]["backup"] != operation
                or record["pending"]["phase"] not in ("backup-complete", "apply-complete", "checks-complete")):
            raise NativeError("Isolated restore requires the matching reserved handover and backup")

    def quiet() -> None:
        check_state()
        quiescent(verify_quiescent)

    check_state()
    evidence = json.loads(private_read(storage["evidence"] / (operation + ".backup.json")))
    if evidence != {"operation": operation, "manifest_sha256": checksum}:
        raise NativeError("Restore backup identity differs from captured evidence")
    verify_backup(backup_path, operation, checksum, committed=committed, native_spec=spec,
                  image_selection=image_selection, native_asset=native_asset)
    if backup_path.name != operation:
        raise NativeError("Restore backup path differs from the reserved operation")
    destination = storage["restores"] / recovery
    FileBackup(backup_path.parent, operation, checksum).restore_new(destination, verify_quiescent=quiet)
    quiet()
    return destination


def recover_before_activation(storage: dict[str, Path], deployment: str, operation: str, *,
                              backup_path: Path, checksum: str,
                              approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """Restore a reviewed backup before any candidate image load or activation."""
    identity(deployment)
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    state = store.status()
    record = state["deployments"].get(deployment)
    if (state["reservation"] != {"schema_version": 1, "deployment": deployment, "operation": operation}
            or record is None or record["committed"] is None or record["pending"] is None
            or record["pending"]["operation"] != operation
            or record["pending"]["phase"] != "backup-complete"
            or record["pending"]["backup"] != operation
            or state["image_load_started"]
            or (storage["evidence"] / (operation + ".apply-attempt.json")).exists()):
        raise NativeError("Backup recovery requires the matching handover before image load and activation")
    committed = record["committed"]
    previous = committed["selection"]
    selected = previous["candidate"]
    asset = storage["assets"] / previous["operation"]
    spec = native_specification(json.loads(private_read(asset / "spec.json")))
    if (spec["deployment"] != deployment
            or json.loads(private_read(asset / "selection.json")) != selected):
        raise NativeError("Backup recovery differs from the committed native assets")
    images = record["pending"]["candidate"]
    backup_evidence = {"operation": operation, "manifest_sha256": checksum}
    if json.loads(private_read(storage["evidence"] / (operation + ".backup.json"))) != backup_evidence:
        raise NativeError("Restore backup identity differs from captured evidence")
    manifest = verify_backup(backup_path, operation, checksum, committed=committed, native_spec=spec,
                             image_selection=images, native_asset=asset)
    if (shutil.disk_usage(storage["restores"]).free < manifest["total_bytes"] + 64 * 1024**2
            or Path(spec["data_dir"]).parent.stat().st_dev != storage["restores"].stat().st_dev):
        raise NativeError("Backup recovery requires sufficient isolated space on the live-data filesystem")
    native.verify_units(native.unit_files(spec, selected, previous["operation"]))
    native.verify_release(selected)
    gateway = native.RUNTIME / previous["operation"] / "ser2net.yaml"
    info = safe_path(gateway)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or gateway.read_bytes() != native.gateway_config(spec):
        raise NativeError("Native gateway configuration changed; backup recovery requires investigation")
    engine = check_docker_conflicts(spec)
    recovery = str(uuid4())
    plan: dict[str, Any] = {
        "kind": "pi-handover-backup-recovery", "deployment": deployment, "operation": operation,
        "recovery": recovery, "native_operation": previous["operation"],
        "backup_manifest_sha256": checksum, "data_dir": spec["data_dir"],
        "restore_matching_data": True, "preserve_post_backup_tree": True,
        "audio_agent_proxy_and_os": "untouched",
    }
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"plan": plan, "digest": digest}) != digest:
        raise NativeError("Native backup recovery was declined; services were not changed")
    if store.status() != state:
        raise NativeError("Handover state changed after backup recovery approval")
    if (json.loads(private_read(asset / "spec.json")) != spec
            or json.loads(private_read(asset / "selection.json")) != selected
            or json.loads(private_read(storage["evidence"] / (operation + ".backup.json"))) != backup_evidence):
        raise NativeError("Native assets changed after backup recovery approval")
    verify_backup(backup_path, operation, checksum, committed=committed, native_spec=spec,
                  image_selection=images, native_asset=asset)
    native.verify_units(native.unit_files(spec, selected, previous["operation"]))
    native.verify_release(selected)
    info = safe_path(gateway)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or gateway.read_bytes() != native.gateway_config(spec):
        raise NativeError("Native gateway configuration changed after backup recovery approval")
    check_docker_conflicts(spec, expected_engine=engine)
    native.check_host_writers(Path(spec["data_dir"]), check_files=False)
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": record,
                   "backup_manifest_sha256": checksum})
    native.stop_native(spec, selected, previous["operation"], {"previous_native": None})

    def quiet() -> None:
        native.check_no_writers(spec)
        check_docker_conflicts(spec, expected_engine=engine)

    restored = prepare_backup_restore(
        store, storage, deployment, operation, backup_path=backup_path, checksum=checksum,
        committed=committed, native_spec=spec, image_selection=images,
        native_asset=asset, recovery=recovery, verify_quiescent=quiet)
    quiet()
    native.preserve_replace(restored / "data", Path(spec["data_dir"]), recovery, spec["uid"])
    check_data(native.data_spec(spec))
    check_docker_conflicts(spec, expected_engine=engine)
    native.command(["systemctl", "enable", "--now", native.GATEWAY, native.WEB], timeout=90)
    native.runtime_checks(spec, selected, previous["operation"])
    check_docker_conflicts(spec, expected_engine=engine)
    if store.status() != state:
        raise NativeError("Handover state changed during native backup recovery checks")
    receipt: dict[str, Any] = {"operation": operation, "review_digest": digest,
                               "required": {"original-runtime": "passed", "matching-data": "passed"}}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, operation, receipt, restore_native=True,
                           native_previous=committed, handover_native=True)
    return {"event": "recovered", "operation": operation, "recovery": recovery, "required": receipt["required"]}


def send_handover_backup(channel: Channel, backup: FileBackup) -> None:
    """Send captured bytes and require the exact independent custody receipt."""
    send_backup(channel, backup.path, backup.operation, backup.manifest_sha256)
    if channel.receive() != {"event": "backup-verified", "operation": backup.operation,
                             "manifest_sha256": backup.manifest_sha256}:
        raise ChannelError("Independent handover backup verification was not acknowledged")


def receive_handover_backup(channel: Channel, evidence: Path, header: dict[str, Any], *,
                            operation: str, committed: dict[str, Any], native_spec: dict[str, Any],
                            image_selection: dict[str, Any], native_asset: Path) -> tuple[Path, str]:
    """Verify received bytes against the controller's reviewed baseline before acknowledging."""
    operation_id(operation)
    if header.get("operation") != operation:
        raise ChannelError("Handover backup differs from the reviewed operation")
    backup, checksum = receive_backup(channel, evidence, header)
    verify_backup(backup, operation, checksum, committed=committed, native_spec=native_spec,
                  image_selection=image_selection, native_asset=native_asset)
    private_write(evidence / (operation + ".backup-identity.json"),
                  {"operation": operation, "manifest_sha256": checksum})
    channel.send({"event": "backup-verified", "operation": operation, "manifest_sha256": checksum})
    return backup, checksum
