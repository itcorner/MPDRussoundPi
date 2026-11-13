"""Concrete Linux development deployment for prepared, self-contained Compose inputs.

Only the selected project's web and optional managed gateway are changed.
Native services, networks, credentials and data paths are never migrated.
"""

import copy
from itertools import chain
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from tool.deployment_backup import MAX_BYTES, FileBackup, extended_metadata, source_info
from tool.deployment_build import archive_identity, digest_stream, image_identity, local_engine
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_images import DIGEST
from tool.deployment_inputs import attributes, read_file, write_file
from tool.deployment_local import LOCAL_CANDIDATE
from tool.deployment_preflight import check_data, check_port, check_serial, mode_access, overlap, safe_path
from tool.deployment_runner import DevelopmentRun, RunHooks
from tool.deployment_state import StateStore, candidate, identity, operation_id


class ComposeError(ValueError):
    """Target validation or service work failed; retain state and private evidence."""


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def private_read(path: Path) -> bytes:
    with checked_directory(path.parent, os.getuid(), private=False) as directory:
        return read_file(directory, path.name, os.getuid(), (0o400, 0o600))[0]


def private_write(path: Path, value: Any) -> None:
    with checked_directory(path.parent, os.getuid()) as directory:
        write_file(directory, path.name, encoded(value), owner=os.getuid(), group=os.getgid(), mode=0o400)


def paths(root: Path, *, state_root: Path | None = None) -> dict[str, Path]:
    """Create only explicitly requested private control storage; never repair it."""
    with checked_directory(root, os.getuid()):
        pass
    result: dict[str, Path] = {}
    for name in ("state", "incoming", "staging", "runtime", "backups", "evidence", "assets", "restores"):
        path = state_root if name == "state" and state_root is not None else root / name
        if not path.exists():
            path.mkdir(mode=0o700)
        with checked_directory(path, os.getuid()):
            pass
        result[name] = path
    return result


def specification(value: Any) -> dict[str, Any]:
    required = {"schema_version", "deployment", "project", "compose", "data_dir",
                "uid", "gid", "gateway", "platform", "publish_host", "publish_port",
                "serial_device", "serial_gid", "require_controller", "require_playback", "adopt"}
    if (not is_mapping(value) or set(value) != required or type(value["schema_version"]) is not int
            or value["schema_version"] != 1):
        raise ComposeError("Select exactly the documented development deployment fields")
    from tool.deployment_preflight import validate_request
    validate_request({key: value[key] for key in (
        "project", "data_dir", "uid", "gid", "gateway", "platform", "publish_host",
        "publish_port", "serial_device", "serial_gid")})
    identity(value["deployment"])
    if value["gateway"] == "dummy":
        raise ComposeError("Automated dummy deployment needs a pinned dummy artifact; use the isolated test layout")
    for key in ("require_controller", "require_playback", "adopt"):
        if type(value[key]) is not bool:
            raise ComposeError("Acceptance and adoption choices must be explicit booleans")
    path = Path(value["compose"]) if isinstance(value["compose"], str) else Path()
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ComposeError("Compose input must be an explicit absolute private file")
    return copy.deepcopy(value)


class ComposeDeployment:
    def __init__(self, spec: dict[str, Any], selection: dict[str, Any], storage: dict[str, Path],
                 *, operation: str | None = None):
        self.spec = specification(spec)
        candidate(selection)
        self.selection = copy.deepcopy(selection)
        metadata = selection["manifest"] if selection["kind"] == LOCAL_CANDIDATE else selection
        if any(metadata[key] != spec[key] for key in ("gateway", "platform")):
            raise ComposeError("Image selection and target layout differ")
        self.operation = operation or str(uuid4())
        operation_id(self.operation)
        self.storage = storage
        self.store = StateStore(storage["state"], owner_uid=os.getuid())
        self.store.initialize()
        self.docker = ["docker", "--context", local_engine()]
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("COMPOSE_", "DOCKER_", "RUSSOUND_"))}
        self.env["COMPOSE_DISABLE_ENV_FILE"] = "1"
        self.input = Path(spec["compose"])
        self.raw = private_read(self.input)
        self.input_attributes = attributes(self.input.stat())
        self.config: dict[str, Any] = json.loads(self.raw)
        self.config.setdefault("name", spec["project"])
        self.services = {"russound-web": "web"}
        if spec["gateway"] == "managed":
            self.services["russound-ser2net"] = "ser2net"
        self.committed = self.store.status()["deployments"].get(spec["deployment"], {}).get("committed")
        if self.committed is not None:
            previous_operation = self.committed["selection"]["operation"]
            previous = json.loads(private_read(storage["runtime"] / previous_operation / "active.json"))
            def without_images(value: dict[str, Any]) -> dict[str, Any]:
                result = copy.deepcopy(value)
                for service in result["services"].values():
                    service.pop("image", None)
                return result
            if without_images(previous) != without_images(self.config):
                raise ComposeError("Recorded layout changed; settings/mount/network migration needs separate review")
            self.config = previous
        self.validate_config()
        self.baseline: list[dict[str, Any]] | None = None
        self.engine_id: str | None = None
        self.asset = storage["assets"] / self.operation
        self.asset.mkdir(mode=0o700)
        private_write(self.asset / "compose.json", self.config)
        private_write(self.asset / "spec.json", self.spec)
        self.generated_play = self.asset / "apply.yml"
        self.inventory = self.asset / "inventory.yml"
        self.inventory.write_text("fixture ansible_connection=local ansible_python_interpreter=" + sys.executable + "\n")
        self.inventory.chmod(0o600)
        # The play contains only a fixed invocation of our shipped apply helper.
        self.generated_play.write_text(json.dumps([{
            "hosts": "all", "gather_facts": False,
            "tasks": [{"name": "Apply the approved project",
                       "ansible.builtin.command": {"argv": [
                           sys.executable, "-m", "tool.deployment_compose", "apply",
                           "{{ deployment_inputs.compose }}", "{{ deployment_images | to_json }}"]},
                       "environment": {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                       "tags": ["deployment_required_apply"]}],
        }]))
        self.generated_play.chmod(0o600)
        self.recovery_sources = {"data": Path(spec["data_dir"]), "deployment": self.asset}

    def command(self, args: list[str], *, timeout: int = 60, output: Any = subprocess.PIPE) -> bytes:
        result = subprocess.run(self.docker + args, env=self.env, stdout=output,
                                stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise ComposeError("Docker/Compose action failed; inspect retained target state before retrying")
        return result.stdout or b""

    def compose(self, path: Path | None = None) -> list[str]:
        return ["compose", "--env-file", "/dev/null", "--project-name", self.spec["project"],
                "-f", str(path or self.asset / "compose.json")]

    def inspect_all(self) -> list[dict[str, Any]]:
        ids = self.command(["container", "ls", "--all", "--quiet", "--no-trunc"]).decode().split()
        return json.loads(self.command(["container", "inspect", *ids])) if ids else []

    def own(self, containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in containers:
            labels: dict[str, Any] = item["Config"].get("Labels") or {}
            if labels.get("com.docker.compose.project") == self.spec["project"]:
                result.append(item)
        return result

    def validate_config(self) -> None:
        config, spec = self.config, self.spec
        if (not is_mapping(config) or set(config) - {"name", "services", "networks"}
                or not is_mapping(config.get("services")) or set(config["services"]) != set(self.services)
                or config.get("name", spec["project"]) != spec["project"]):
            raise ComposeError("Use one rendered self-contained web/managed-gateway Compose JSON without extra services")
        if b"${" in self.raw:
            raise ComposeError("Compose inputs must be fully rendered, not interpolated at deployment time")
        for name, service in config["services"].items():
            fields = {
                "image", "platform", "restart", "stop_grace_period", "user", "ports", "environment",
                "volumes", "logging", "healthcheck", "networks", "depends_on", "devices", "group_add",
                "extra_hosts", "command", "entrypoint",
            }
            if (not is_mapping(service) or "build" in service or service.get("privileged")
                    or set(service) - fields
                    or service.get("network_mode") or service.get("pid") or service.get("ipc")
                    or service.get("volumes_from") or service.get("cap_add")
                    or service.get("user") != f'{spec["uid"]}:{spec["gid"]}'
                    or service.get("platform") != spec["platform"]
                    or not isinstance(service.get("image"), str)
                    or service.get("command") is not None or service.get("entrypoint") is not None
                    or service.get("healthcheck", {}).get("disable")):
                raise ComposeError("Unsupported build, privilege, identity, platform or disabled healthcheck")
            if name == "russound-web" and (service.get("devices") or service.get("group_add")):
                raise ComposeError("Web scope must not acquire gateway hardware permissions")
            mounts_value: list[dict[str, Any]] = service.get("volumes", [])
            for mount in mounts_value:
                if (mount.get("type") != "bind" or not Path(mount.get("source", "")).is_absolute()
                        or not overlap(Path(mount["source"]), Path(spec["data_dir"]))
                        or mount.get("bind", {}).get("create_host_path", False)):
                    raise ComposeError("Every mount must be an existing bind strictly inside the approved data root")
                if Path(mount["source"]) == Path(spec["data_dir"]):
                    raise ComposeError("Mount specific data subdirectories, not the entire data root")
            if name != "russound-web" and service.get("ports"):
                raise ComposeError("Managed gateway ports must not be published")
        for network in config.get("networks", {}).values():
            if network.get("driver", "bridge") != "bridge" or set(network) - {
                    "name", "external", "driver", "internal", "enable_ipv6", "ipam", "labels"}:
                raise ComposeError("Only ordinary bridge or explicitly reused networks are supported")
        web = config["services"]["russound-web"]
        expected = {"/config": "config", "/data": "data", "/logs": "logs", "/run/secrets": "secrets"}
        mounts = {mount["target"]: mount for mount in web.get("volumes", [])}
        if set(mounts) != set(expected) or any(
                Path(mounts[target]["source"]) != Path(spec["data_dir"]) / child
                or bool(mounts[target].get("read_only")) != (child == "secrets")
                for target, child in expected.items()):
            raise ComposeError("Web mounts must preserve exactly config/data/logs/secrets")
        ports = web.get("ports", [])
        if (len(ports) != 1 or ports[0].get("target") != 8000
                or str(ports[0].get("published")) != str(spec["publish_port"])
                or ports[0].get("host_ip") != spec["publish_host"]):
            raise ComposeError("Web listener differs from the explicit reviewed host and port")
        agents = web.get("environment", {}).get("RUSSOUND_PLAYBACK_AGENTS", "")
        if bool(agents) != spec["require_playback"]:
            raise ComposeError("Configured playback requires acceptance; disabled playback must have no agent input")
        if spec["gateway"] == "managed":
            gateway = config["services"]["russound-ser2net"]
            devices = gateway.get("devices", [])
            if (len(devices) != 1 or devices[0].get("source") != spec["serial_device"]
                    or devices[0].get("target") != spec["serial_device"]
                    or str(spec["serial_gid"]) not in [str(item) for item in gateway.get("group_add", [])]):
                raise ComposeError("Managed serial device/group differs from reviewed hardware")

    def current_signature(self, containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted([{
            "id": item["Id"], "image": item["Image"], "mounts": item["Mounts"],
            "config": item["Config"], "host": item["HostConfig"],
            "networks": {name: network["NetworkID"] for name, network in item["NetworkSettings"]["Networks"].items()},
        } for item in containers], key=lambda item: item["id"])

    def preflight(self) -> None:
        spec = self.spec
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise ComposeError("Concrete target deployment requires Linux root and a local Docker daemon")
        if private_read(self.input) != self.raw or attributes(self.input.stat()) != self.input_attributes:
            raise ComposeError("Reviewed Compose input changed")
        info = json.loads(self.command(["info", "--format", "{{json .}}"]))
        arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
        if (info["OSType"] + "/" + arch != spec["platform"]
                or any("rootless" in option or "userns" in option for option in info["SecurityOptions"])):
            raise ComposeError("Target platform or Docker namespace mode differs from the reviewed layout")
        if self.engine_id is not None and info["ID"] != self.engine_id:
            raise ComposeError("Docker daemon identity changed after review")
        self.engine_id = info["ID"]
        check_data(spec)
        validate_runtime_files(spec, self.config["services"]["russound-web"].get("environment", {}))
        safe_path(Path(spec["data_dir"]) / "config/russound_config.json")
        # Parsing the actual config through the application validator is required,
        # not just accepting any JSON file with a plausible name.
        from web.config_types import validate_russound_config
        configuration = json.loads((Path(spec["data_dir"]) / "config/russound_config.json").read_bytes())
        validate_russound_config(configuration)
        if spec["require_controller"] and not configuration.get("zones"):
            raise ComposeError("Controller acceptance requires explicit configured zone mappings")
        all_containers = self.inspect_all()
        own = self.own(all_containers)
        for item in all_containers:
            if item in own:
                continue
            if any(mount.get("Type") == "bind" and overlap(Path(mount["Source"]), Path(spec["data_dir"]))
                   for mount in item.get("Mounts", [])):
                raise ComposeError("Another container shares deployment data; no writers were stopped")
            devices: list[dict[str, Any]] = item["HostConfig"].get("Devices") or []
            if spec["gateway"] == "managed" and any(
                    device["PathOnHost"] == spec["serial_device"]
                    for device in devices):
                raise ComposeError("Another container owns the serial adapter")
        if own:
            names: list[Any] = [item["Config"]["Labels"].get("com.docker.compose.service") for item in own]
            if len(names) != len(self.services) or set(names) != set(self.services):
                raise ComposeError("Project contains unexpected, duplicated or missing services")
            # Compare actual Compose generation hashes before adopting/rerunning.
            hashes = {}
            for line in self.command(self.compose() + ["config", "--hash", "*"]).decode().splitlines():
                name, checksum = line.split()
                hashes[name] = checksum
            for item in own:
                labels = item["Config"]["Labels"]
                if labels.get("com.docker.compose.config-hash") != hashes[labels["com.docker.compose.service"]]:
                    raise ComposeError("Running layout differs from supplied baseline; migration needs separate review")
            committed = self.store.status()["deployments"].get(spec["deployment"], {}).get("committed")
            if committed is None and not spec["adopt"]:
                raise ComposeError("Existing unrecorded installation requires explicit adoption approval")
            signature = self.current_signature(own)
            if self.baseline is not None and signature != self.baseline:
                raise ComposeError("Project identity changed after review")
            self.baseline = signature
        elif self.baseline:
            raise ComposeError("Reviewed installation disappeared")
        else:
            check_port(spec["publish_host"], spec["publish_port"])
            if spec["gateway"] == "managed":
                check_serial(spec)
            self.baseline = []
        if Path(spec["data_dir"]).parent.stat().st_dev != self.storage["restores"].stat().st_dev:
            raise ComposeError("Recovery storage and live data must share a filesystem for preserved-tree activation")
        size = sum(path.stat().st_size for path in Path(spec["data_dir"]).rglob("*") if path.is_file())
        # Images are accounted independently by their actual Docker size.
        for image in {item["image"] for item in self.baseline}:
            actual = json.loads(self.command(["image", "inspect", image]))[0]
            if actual["Os"] + "/" + actual["Architecture"] != spec["platform"]:
                raise ComposeError("Previous runtime image platform differs from the declared recovery platform")
            size += actual["Size"]
        if size > MAX_BYTES - 64 * 1024**2:
            raise ComposeError("Recovery contents exceed the verified backup size limit")
        self.backup_estimate = 2 * size + 64 * 1024**2
        incoming = sum(image["bytes"] for image in self.selection.get("manifest", {}).get("images", {}).values())
        if shutil.disk_usage(self.storage["backups"]).free < 3 * size + incoming + 256 * 1024**2:
            raise ComposeError("Insufficient target space for incoming images, backup and isolated restore")

    def scope(self) -> dict[str, Any]:
        if self.baseline is None:
            raise ComposeError("Target scope requires a completed read-only preflight")
        return {"project": self.spec["project"], "services": sorted(self.services),
                "adoption": bool(self.baseline and self.spec["adopt"]),
                "controller_acceptance": self.spec["require_controller"],
                "playback_acceptance": self.spec["require_playback"],
                "listener": {"host": self.spec["publish_host"], "port": self.spec["publish_port"]},
                "offhost_free_bytes_required": self.backup_estimate,
                "backend": {key: self.config["services"]["russound-web"].get("environment", {}).get(key)
                            for key in ("RUSSOUND_BACKEND_HOST", "RUSSOUND_BACKEND_PORT")},
                "native_services": "untouched", "existing_data_and_secrets": "preserved"}

    def stop(self) -> None:
        self.preflight()
        self.command(self.compose() + ["stop", "--timeout", "30"], timeout=90)
        self.quiescent()
        images: dict[str, Any] = {}
        for name, component in self.services.items():
            item = next((entry for entry in self.baseline or []
                         if entry["config"]["Labels"]["com.docker.compose.service"] == name), None)
            if item is None:
                continue
            reference = item["image"]
            tag = f"mpdrussoundpi-recovery/{component}:{self.operation}"
            self.command(["image", "tag", reference, tag])
            archive = self.asset / (component + ".tar")
            with archive.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                self.command(["image", "save", tag], timeout=600, output=output)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            image: dict[str, Any] = {"tag": tag, "reference": reference}
            with archive.open("rb") as source:
                image["config"] = archive_identity(source, image, self.spec["platform"], None)
                source.seek(0)
                image["sha256"], image["bytes"] = digest_stream(source)
            image["archive"] = archive.name
            images[component] = image
            self.quiescent()
        private_write(self.asset / "images.json", images)

    def quiescent(self) -> None:
        info = json.loads(self.command(["info", "--format", "{{json .}}"]))
        if self.engine_id is not None and info["ID"] != self.engine_id:
            raise ComposeError("Docker daemon identity changed while writers should be stopped")
        containers = self.inspect_all()
        own = self.own(containers)
        if any(item["State"]["Running"] or item["State"]["Restarting"] or item["State"]["Paused"] for item in own):
            raise ComposeError("Approved container writers are not stopped")
        if self.baseline is not None:
            if {item["Id"] for item in own} != {item["id"] for item in self.baseline}:
                raise ComposeError("Container identities changed while writers should be stopped")
        for item in containers:
            if item["State"]["Running"] and any(
                    mount.get("Type") == "bind" and overlap(Path(mount["Source"]), Path(self.spec["data_dir"]))
                    for mount in item.get("Mounts", [])):
                raise ComposeError("A container still writes deployment data")
        check_host_writers(Path(self.spec["data_dir"]))
        if self.spec["gateway"] == "managed":
            check_serial(self.spec)

    def checks(self, images: dict[str, str]) -> dict[str, str]:
        return runtime_checks(self, images)

    def unchanged_images(self) -> dict[str, str] | None:
        if self.committed is None or self.committed["selection"]["candidate"] != self.selection:
            return None
        images = {self.services[entry["config"]["Labels"]["com.docker.compose.service"]]: entry["image"]
                  for entry in self.baseline or []}
        if set(images) != set(self.services.values()):
            raise ComposeError("Recorded project is incomplete")
        if self.selection["kind"] == LOCAL_CANDIDATE:
            if images != self.committed["selection"]["loaded_images"]:
                raise ComposeError("Running images differ from the committed pin")
        else:
            for name, reference in images.items():
                image = self.selection["images"][name]
                if reference not in (image["index_digest"], image["platform_digest"], image["config_digest"]):
                    raise ComposeError("Running registry image differs from committed metadata")
                actual = json.loads(self.command(["image", "inspect", reference]))[0]
                image_identity(actual, reference, self.spec["platform"], self.selection["revision"])
        return images

    def runner(self, verify_backup: Callable[[FileBackup], None],
               receive_bundle: Callable[[str, str], Path] | None = None) -> DevelopmentRun:
        self.preflight()
        private_write(self.asset / "baseline.json", self.baseline)
        return DevelopmentRun(
            store=self.store, deployment=self.spec["deployment"], host="fixture",
            bundle=self.storage["incoming"], manifest_sha256="", gateway=self.spec["gateway"],
            platform=self.spec["platform"], ansible=Path(sys.executable).parent / "ansible-playbook",
            playbook=self.generated_play, inventory=self.inventory,
            protected_inputs={"compose": self.asset / "compose.json"},
            recovery_sources=self.recovery_sources,
            **{name: self.storage[name] for name in ("staging", "runtime", "backups", "evidence")},
            required_apply=["apply"], required_checks=["containers", "http", "backend", "playback"],
            hooks=RunHooks(self.preflight, self.stop, self.quiescent, verify_backup, self.checks),
            operation=self.operation, selection=self.selection, receive_bundle=receive_bundle,
            scope=self.scope(), source_owner_uid=self.spec["uid"])


def check_host_writers(root: Path, *, check_files: bool = True) -> None:
    """Reject open native writers and outstanding orchestration, not just containers."""
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            args = (process / "cmdline").read_bytes().split(b"\0")
            if any(b"ansible-playbook" in arg or b"AnsiballZ_" in arg for arg in args):
                raise ComposeError("Ansible work may still be active; investigate before recovery or backup")
            if not check_files:
                continue
            for descriptor in (process / "fd").iterdir():
                try:
                    target = Path(os.readlink(descriptor).removesuffix(" (deleted)"))
                    if not target.is_absolute() or not overlap(root, target):
                        continue
                    info = (process / "fdinfo" / descriptor.name).read_text()
                    flags = next(line.split()[1] for line in info.splitlines() if line.startswith("flags:"))
                    if int(flags, 8) & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                        raise ComposeError("A native process still has deployment data open for writing")
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except PermissionError:
            raise ComposeError("Cannot establish host writer quiescence from /proc") from None


def validate_runtime_files(spec: dict[str, Any], environment: dict[str, Any]) -> None:
    root = Path(spec["data_dir"])
    with checked_directory(root.parent, spec["uid"], private=False):
        pass
    source_info(root.lstat(), spec["uid"])
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        extended_metadata(fd)
    finally:
        os.close(fd)
    count = 0
    for folder in ("config", "data", "logs", "secrets"):
        for path in chain((root / folder,), (root / folder).rglob("*")):
            count += 1
            info = safe_path(path)
            if count > 10000 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ComposeError("Runtime trees exceed limits or contain special objects")
            source_info(info, spec["uid"])
            required = 5 if stat.S_ISDIR(info.st_mode) else 4 if folder == "secrets" else 6
            if not mode_access(info, spec["uid"], {spec["gid"]}, required):
                raise ComposeError("Service identity lacks required access to runtime data or protected inputs")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                extended_metadata(fd)
            finally:
                os.close(fd)
    from web.playback_status import PlaybackAgentConfig, PlaybackStatusClient
    agents = PlaybackAgentConfig.from_json(environment.get("RUSSOUND_PLAYBACK_AGENTS") or None)
    if bool(agents) != spec["require_playback"]:
        raise ComposeError("Playback configuration and required acceptance differ")
    def secret(path: str) -> str:
        value = Path(path)
        if not value.is_relative_to("/run/secrets") or ".." in value.parts:
            raise ComposeError("Playback credentials and CA must use the protected /run/secrets bind")
        result = root / "secrets" / value.relative_to("/run/secrets")
        if not result.is_file():
            raise ComposeError("A required playback credential or CA file is missing")
        return str(result)
    for agent in agents or ():
        mapped = PlaybackAgentConfig(agent.base_url, agent.agent_id, secret(agent.token_file),
                                     secret(agent.tls_ca_file) if agent.tls_ca_file is not None else None)
        PlaybackStatusClient(mapped)


def runtime_checks(target: ComposeDeployment, images: dict[str, str]) -> dict[str, str]:
    deadline = time.monotonic() + 60
    expected = {name: images[component] for name, component in target.services.items()}
    while True:
        own = target.own(target.inspect_all())
        if len(own) != len(expected):
            raise ComposeError("Runtime service set differs from approved scope")
        if any(item["Image"] != expected[item["Config"]["Labels"]["com.docker.compose.service"]]
               or not item["State"]["Running"] or item["RestartCount"] for item in own):
            raise ComposeError("Runtime image, running state or restart count failed verification")
        web = next(item for item in own
                   if item["Config"]["Labels"]["com.docker.compose.service"] == "russound-web")
        script = (
            "import json,urllib.request\n"
            "def get(path):\n"
            " return json.load(urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=5))\n"
            "r=get('/api/rooms'); p=get('/api/playback/health')\n"
            "print(json.dumps({'http':True,'configured':not r.get('config_required',True),"
            "'backend':r.get('backend_status',{}).get('connected',False),"
            "'playback':p.get('status')=='ok','playback_configured':p.get('configured',False)}))")
        try:
            status = json.loads(target.command(["exec", web["Id"], "python", "-c", script], timeout=20))
            passed = (status["configured"]
                      and (status["backend"] or not target.spec["require_controller"])
                      and (status["playback"] if target.spec["require_playback"] else not status["playback_configured"])
                      and all(item["State"].get("Health", {}).get("Status") == "healthy" for item in own))
            if passed:
                # Check the published host route too, not only container loopback.
                import urllib.request
                host = target.spec["publish_host"]
                host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
                url_host = "[" + host + "]" if ":" in host else host
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'http://{url_host}:{target.spec["publish_port"]}/api/rooms', timeout=5) as response:
                    if response.status != 200:
                        raise ComposeError("Published host HTTP route did not return 200")
                return dict.fromkeys(("containers", "http", "backend", "playback"), "passed")
        except (ComposeError, OSError, ValueError):
            if time.monotonic() >= deadline:
                raise ComposeError("Runtime health/routing failed; pending state and backup are retained") from None
        if time.monotonic() >= deadline:
            raise ComposeError("Runtime health/routing deadline exceeded; pending state is retained")
        time.sleep(1)


def apply(path: Path, images: dict[str, str]) -> None:
    """Fixed Ansible consumer: replace images only, never build/pull/change data."""
    value: dict[str, Any] = json.loads(private_read(path))
    components = {"russound-web": "web", "russound-ser2net": "ser2net"}
    if (not is_mapping(images) or set(images) != {components[name] for name in value["services"]}
            or any(not isinstance(image, str) or not DIGEST.fullmatch(image) for image in images.values())):
        raise ComposeError("Apply requires the exact verified target image-ID set")
    for name, service in value["services"].items():
        service["image"] = images[components[name]]
    output = path.parent / "active.json"
    private_write(output, value)
    docker: list[str] = ["docker", "--context", local_engine(), "compose", "--env-file", "/dev/null",
              "-p", value["name"], "-f", str(output)]
    env = {key: item for key, item in os.environ.items() if not key.startswith(("COMPOSE_", "DOCKER_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    result = subprocess.run(docker + ["up", "--detach", "--no-build", "--pull", "never",
                                     "--wait", "--wait-timeout", "60"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if result.returncode:
        raise ComposeError("Approved Compose apply failed; no rollback was attempted")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "apply":
        raise SystemExit("Internal apply is invoked only by the controlled deployment runner")
    apply(Path(sys.argv[2]), json.loads(sys.argv[3]))
