"""Internal target operations for a reserved Pi handover.

The transaction coordinator must bind these operations to a reviewed
reservation before stopping services or activating containers.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import sys
from typing import Any

from tool import deployment_native as native
from tool.deployment_backup import MAX_BYTES
from tool.deployment_build import local_engine
from tool.deployment_channel import Channel, ChannelError
from tool.deployment_compose import apply, encoded, private_read, runtime_checks, validate_runtime_files
from tool.deployment_files import is_list, is_mapping
from tool.deployment_images import MAX_METADATA
from tool.deployment_load import load_reserved_bundle, select_bundle
from tool.deployment_native_source import NativeError
from tool.deployment_pi_handover import (
    HANDOVER_CAPTURE_ATTEMPTS, HANDOVER_STORAGE_MARGIN, capture_backup, check_docker_conflicts,
    fence_native, inspect_candidate_containers, prepare,
    send_handover_backup,
)
from tool.deployment_preflight import check_data, check_port, mode_access, overlap, safe_path
from tool.deployment_state import StateStore, identity, operation_id


class HandoverTarget:
    """Validate the exact committed native baseline without changing it."""

    def __init__(self, store: StateStore, storage: dict[str, Path], deployment: str,
                 operation: str, selection: dict[str, Any]):
        identity(deployment)
        operation_id(operation)
        if store.owner_uid != os.getuid():
            raise NativeError("Handover target must run as the deployment-state owner")
        self.store = store
        self.storage = storage
        self.deployment = deployment
        self.operation = operation
        self.selection = copy.deepcopy(selection)
        state = store.status()
        committed = state["deployments"].get(deployment, {}).get("committed")
        if state["blocked"] or committed is None:
            raise NativeError("Handover requires an unblocked committed native deployment")
        self.initial_state = state
        self.committed = copy.deepcopy(committed)
        previous = committed["selection"]
        self.asset = storage["assets"] / previous["operation"]
        self.native_spec = native.specification(json.loads(private_read(self.asset / "spec.json")))
        if (self.native_spec["deployment"] != deployment
                or json.loads(private_read(self.asset / "selection.json")) != previous["candidate"]):
            raise NativeError("Committed native assets do not match deployment state")
        self.project = "pi-handover-" + operation.replace("-", "")
        self.compose_spec, self.config = prepare(
            self.native_spec, previous["candidate"], self.selection, project=self.project,
            compose_path=str(storage["runtime"] / operation / "compose.json"))
        self.spec = self.compose_spec
        self.services = {"russound-web": "web", "russound-ser2net": "ser2net"}
        self.engine: str | None = None
        self.backup_estimate: int | None = None

    def preflight(self) -> None:
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise NativeError("Handover preflight requires Linux root")
        if self.store.status() != self.initial_state:
            raise NativeError("Native deployment state changed during handover review")
        previous = self.committed["selection"]
        spec = self.native_spec
        if (json.loads(private_read(self.asset / "spec.json")) != spec
                or json.loads(private_read(self.asset / "selection.json")) != previous["candidate"]):
            raise NativeError("Committed native assets changed during handover review")
        if pwd.getpwuid(spec["uid"]).pw_gid != spec["gid"]:
            raise NativeError("Native account primary group changed")
        if any(overlap(Path(spec["data_dir"]), self.storage[name])
               for name in ("state", "incoming", "staging", "runtime", "backups", "evidence", "assets", "restores")):
            raise NativeError("Handover data and transaction storage must not overlap")
        check_data(native.data_spec(spec))
        validate_runtime_files(native.data_spec(spec), native.environment(spec, native=False))
        from web.config_types import validate_russound_config
        configuration = json.loads((Path(spec["data_dir"]) / "config/russound_config.json").read_bytes())
        validate_russound_config(configuration)
        if not configuration.get("zones"):
            raise NativeError("Handover requires explicit controller zone mappings")
        device = safe_path(Path(spec["serial_device"]), device=True)
        if (not stat.S_ISCHR(device.st_mode) or device.st_gid != spec["serial_gid"]
                or not mode_access(device, spec["uid"], {spec["gid"], spec["serial_gid"]}, 6)):
            raise NativeError("Native serial device identity or permissions changed")
        distribution = native.unit_status("ser2net.service")
        if (distribution["ActiveState"] != "inactive"
                or distribution.get("UnitFileState", "") not in ("disabled", "", "static")):
            raise NativeError("Distribution ser2net must already be inactive")
        native.runtime_checks(spec, previous["candidate"], previous["operation"])
        native.check_host_writers(Path(spec["data_dir"]), check_files=False)
        engine = check_docker_conflicts(spec, expected_engine=self.engine, handover_project=self.project)
        docker = ["docker", "--context", local_engine()]
        info: Any = json.loads(native.command(docker + ["info", "--format", "{{json .}}"]))
        if not is_mapping(info):
            raise NativeError("Docker daemon metadata is invalid")
        architecture = info.get("Architecture")
        if not isinstance(architecture, str):
            raise NativeError("Docker daemon architecture is invalid")
        arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(architecture, architecture)
        security = info.get("SecurityOptions")
        if (info.get("ID") != engine or f'{info.get("OSType")}/{arch}' != self.compose_spec["platform"]
                or not is_list(security)
                or any(not isinstance(option, str) or "rootless" in option or "userns" in option
                       for option in security)):
            raise NativeError("Docker daemon identity, platform or namespace mode differs from the reviewed scope")
        compose: Any = json.loads(native.command(docker + ["compose", "version", "--format", "json"]))
        if not is_mapping(compose) or not isinstance(compose.get("version"), str):
            raise NativeError("Docker Compose version could not be established")
        roots = (Path(spec["data_dir"]), self.asset,
                 native.RELEASES / previous["candidate"]["release"], native.RUNTIME / previous["operation"])
        size = sum(path.stat().st_size for root in roots for path in root.rglob("*") if path.is_file())
        if size > MAX_BYTES - 64 * 1024**2:
            raise NativeError("Handover recovery contents exceed the backup size limit")
        estimate = 2 * size + 64 * 1024**2
        if self.backup_estimate is not None and 2 * size > self.backup_estimate:
            raise NativeError("Handover recovery contents outgrew the reviewed capacity")
        incoming = sum(item["bytes"] for item in self.selection["manifest"]["images"].values())
        if shutil.disk_usage(self.storage["backups"]).free < (
                (HANDOVER_CAPTURE_ATTEMPTS + 2) * size + incoming + HANDOVER_STORAGE_MARGIN):
            raise NativeError("Insufficient space for image transfer, backup and isolated restore")
        if (Path(spec["data_dir"]).parent.stat().st_dev != self.storage["restores"].stat().st_dev
                or native.RELEASES.stat().st_dev != self.storage["restores"].stat().st_dev
                or native.RUNTIME.stat().st_dev != self.storage["restores"].stat().st_dev):
            raise NativeError("Native recovery requires isolated storage on the same filesystem")
        check_docker_conflicts(spec, expected_engine=engine, handover_project=self.project)
        if self.store.status() != self.initial_state:
            raise NativeError("Native deployment state changed during handover preflight")
        self.engine = engine
        if self.backup_estimate is None:
            self.backup_estimate = estimate

    def reviewed_engine(self) -> str:
        if self.engine is None or self.backup_estimate is None:
            raise NativeError("Handover target requires a completed preflight")
        return self.engine

    def receive_images(self, channel: Channel, digest: str) -> None:
        self.reviewed_engine()
        state = self.store.status()
        record = state["deployments"].get(self.deployment)
        if (state["reservation"] != {"schema_version": 1, "deployment": self.deployment,
                                     "operation": self.operation}
                or record is None or record["committed"] != self.committed or record["pending"] is None
                or record["pending"]["operation"] != self.operation
                or record["pending"]["candidate"] != self.selection
                or record["pending"]["intent_digest"] != digest
                or record["pending"]["phase"] != "inputs-verified"
                or not state["image_transfer_started"] or state["image_transfer_verified"]):
            raise NativeError("Image transfer requires the matching reserved handover")
        channel.send({"event": "images", "operation": self.operation, "intent_digest": digest})
        path = self.storage["incoming"] / self.operation
        path.mkdir(mode=0o700)
        manifest = self.selection["manifest"]
        channel.receive_file(path, "manifest.json", maximum=MAX_METADATA,
                             checksum=self.selection["manifest_sha256"].removeprefix("sha256:"))
        for component in sorted(manifest["images"]):
            image = manifest["images"][component]
            channel.receive_file(path, image["archive"], size=image["bytes"],
                                 checksum=image["sha256"].removeprefix("sha256:"))
        if select_bundle(path, self.selection["manifest_sha256"], manifest["gateway"],
                         manifest["platform"]) != self.selection:
            raise ChannelError("Transferred image bundle differs from the reviewed selection")
        if self.store.status() != state:
            raise NativeError("Handover state changed during image transfer")

    def stop_native(self) -> None:
        engine = self.reviewed_engine()
        state = self.store.status()
        record = state["deployments"].get(self.deployment)
        if (state["reservation"] != {"schema_version": 1, "deployment": self.deployment,
                                     "operation": self.operation}
                or record is None or record["committed"] != self.committed or record["pending"] is None
                or record["pending"]["operation"] != self.operation
                or record["pending"]["candidate"] != self.selection
                or record["pending"]["phase"] != "inputs-verified"
                or not state["image_transfer_verified"]):
            raise NativeError("Native fencing requires the matching verified image transfer")
        check_docker_conflicts(self.native_spec, expected_engine=engine, handover_project=self.project)
        fence_native(self.store, self.storage, self.deployment, self.operation,
                     committed=self.committed, native_spec=self.native_spec,
                     image_selection=self.selection)

    def quiescent(self) -> None:
        previous = self.committed["selection"]
        native.verify_units(native.unit_files(self.native_spec, previous["candidate"], previous["operation"]))
        if any(unit.get("UnitFileState") != "disabled" for unit in native.system_units().values()):
            raise NativeError("Native handover units are no longer disabled")
        native.check_no_writers(self.native_spec)
        check_docker_conflicts(self.native_spec, expected_engine=self.reviewed_engine(),
                               handover_project=self.project)

    def capture_backup(self, channel: Channel) -> None:
        capture_backup(self.store, self.storage, self.deployment, self.operation,
                       committed=self.committed, native_spec=self.native_spec,
                       image_selection=self.selection, native_asset=self.asset,
                       verify_quiescent=self.quiescent,
                       verify_offhost=lambda backup: send_handover_backup(channel, backup))

    def load_images(self, digest: str) -> dict[str, str]:
        self.quiescent()
        images = load_reserved_bundle(
            self.store, self.deployment, self.operation, digest, self.selection,
            self.storage["incoming"] / self.operation, self.storage["evidence"],
            expected_engine=self.reviewed_engine())
        self.quiescent()
        return images

    def command(self, args: list[str], *, timeout: int = 60) -> bytes:
        docker = ["docker", "--context", local_engine()]
        info: Any = json.loads(native.command(docker + ["info", "--format", "{{json .}}"]))
        if not is_mapping(info) or info.get("ID") != self.reviewed_engine():
            raise NativeError("Docker daemon changed during runtime verification")
        return native.command(docker + args, timeout=timeout)

    def inspect_all(self) -> list[dict[str, Any]]:
        ids = self.command(["container", "ls", "--all", "--quiet", "--no-trunc"]).decode().split()
        containers: Any = json.loads(self.command(["container", "inspect", *ids])) if ids else []
        if not is_list(containers) or len(containers) != len(ids):
            raise NativeError("Cannot establish runtime container identities")
        return containers

    def own(self, containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in containers:
            config = item.get("Config")
            if not is_mapping(config):
                raise NativeError("Invalid runtime container configuration")
            labels: Any = config.get("Labels") or {}
            if not is_mapping(labels):
                raise NativeError("Invalid runtime container labels")
            if labels.get("com.docker.compose.project") == self.project:
                result.append(item)
        return result

    def activate(self, images: dict[str, str]) -> None:
        state = self.store.status()
        record = state["deployments"].get(self.deployment)
        if (state["reservation"] != {"schema_version": 1, "deployment": self.deployment,
                                     "operation": self.operation}
                or record is None or record["committed"] != self.committed or record["pending"] is None
                or record["pending"]["operation"] != self.operation
                or record["pending"]["phase"] != "backup-complete"
                or record["pending"]["candidate"] != self.selection
                or record["pending"]["loaded_images"] != images or not state["image_load_started"]
                or record["pending"]["backup"] != self.operation):
            raise NativeError("Activation requires the matching backup and fully loaded image identities")
        pending = record["pending"]
        runtime = self.storage["runtime"] / self.operation
        if json.loads(private_read(runtime / "compose.json")) != self.config:
            raise NativeError("Activation Compose scope changed")
        marker = json.loads(private_read(self.storage["evidence"] / (self.operation + ".apply-attempt.json")))
        activation = json.loads(private_read(self.storage["evidence"] / (self.operation + ".handover-activation.json")))
        if (marker != {"operation": self.operation, "intent_digest": pending["intent_digest"]}
                or activation != {"operation": self.operation, "intent_digest": pending["intent_digest"],
                                  "project": self.project, "engine": self.reviewed_engine(), "images": images,
                                  "config_digest": "sha256:" + hashlib.sha256(encoded(self.config)).hexdigest()}):
            raise NativeError("Activation evidence differs from the reserved handover")
        self.quiescent()
        check_port(self.native_spec["publish_host"], self.native_spec["publish_port"])
        if self.store.status() != state:
            raise NativeError("Handover state changed before activation")
        apply(runtime / "compose.json", images)
        self.command(["info", "--format", "{{json .}}"])

    def checks(self, images: dict[str, str]) -> dict[str, str]:
        previous = self.committed["selection"]
        native.verify_units(native.unit_files(self.native_spec, previous["candidate"], previous["operation"]))
        if any(unit.get("UnitFileState") != "disabled" or unit.get("ActiveState") not in ("inactive", "failed")
               or unit.get("MainPID") != "0" for unit in native.system_units().values()):
            raise NativeError("Native services are not fenced during candidate checks")
        candidate = inspect_candidate_containers(self.project, images, self.reviewed_engine())
        if len(candidate) != len(self.services) or any(
                not item["running"] or item["restarting"] for item in candidate):
            raise NativeError("Candidate container set is incomplete or not running")
        containers = self.inspect_all()
        for item in containers:
            if not is_mapping(item) or not isinstance(item.get("Id"), str):
                raise NativeError("Invalid container identity during acceptance")
            if item.get("Id") in {entry["id"] for entry in candidate}:
                continue
            mounts = item.get("Mounts")
            host = item.get("HostConfig")
            if not is_list(mounts) or not is_mapping(host):
                raise NativeError("Invalid foreign container metadata during acceptance")
            devices: Any = host.get("Devices") or []
            if (not is_list(devices) or any(not is_mapping(device) for device in devices)
                    or any(not is_mapping(mount) for mount in mounts)):
                raise NativeError("Invalid foreign container resources during acceptance")
            if (any(mount.get("Type") == "bind" and
                    (not isinstance(mount.get("Source"), str) or
                     overlap(Path(mount["Source"]), Path(self.native_spec["data_dir"]))) for mount in mounts)
                    or any(device.get("PathOnHost") == self.native_spec["serial_device"] for device in devices)):
                raise NativeError("A foreign container references handover data or serial hardware")
        lines = self.command(["compose", "--env-file", "/dev/null", "--project-name", self.project,
                              "-f", str(self.storage["runtime"] / self.operation / "active.json"),
                              "config", "--hash", "*"]).decode().splitlines()
        hashes: dict[str, str] = {}
        for line in lines:
            parts = line.split()
            if len(parts) != 2 or parts[0] in hashes:
                raise NativeError("Cannot establish candidate Compose configuration hashes")
            hashes[parts[0]] = parts[1]
        if set(hashes) != set(self.services):
            raise NativeError("Candidate Compose service hashes are incomplete")
        for item in self.own(containers):
            labels = item["Config"]["Labels"]
            service = labels.get("com.docker.compose.service")
            if service not in hashes or labels.get("com.docker.compose.config-hash") != hashes[service]:
                raise NativeError("Candidate container configuration differs from the reviewed Compose scope")
        result = runtime_checks(self, images)
        if inspect_candidate_containers(self.project, images, self.reviewed_engine()) != candidate:
            raise NativeError("Candidate container identities changed during runtime checks")
        if any(unit.get("UnitFileState") != "disabled" or unit.get("ActiveState") not in ("inactive", "failed")
               or unit.get("MainPID") != "0" for unit in native.system_units().values()):
            raise NativeError("Native services changed during candidate checks")
        return result
