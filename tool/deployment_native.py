"""Reviewed native web/gateway transactions on a prepared, unchanged Linux host.

Audio, the agent, proxy, distribution ser2net unit and OS packages are not managed.
The same host reservation as Compose prevents concurrent deployment operations.
"""

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from tool.deployment_backup import FileBackup
from tool.deployment_channel import portable_backup
from tool.deployment_compose import (
    ComposeDeployment, check_host_writers, encoded, private_read, private_write,
    validate_runtime_files,
)
from tool.deployment_files import checked_directory, is_mapping
from tool.deployment_native_source import (
    NATIVE_CANDIDATE, RELEASES, SER2NET, NativeError, validate_candidate,
    verify_release,
)
from tool.deployment_preflight import check_data, check_port, check_serial, overlap, safe_path
from tool.deployment_state import StateStore, identity, operation_id


RUNTIME = Path("/opt/mpdrussoundpi-native-runtime")
UNITS = Path("/etc/systemd/system")
WEB = "russound-web.service"
GATEWAY = "mpdrussoundpi-serial.service"
CHECKS = ("units", "http", "backend", "playback")
RECOVERY_CHECKS = {"original-runtime": "passed", "matching-data": "passed"}
SAFE_PATH = re.compile(r"/[a-zA-Z0-9_./-]+")


def command(argv: list[str], *, timeout: int = 60) -> bytes:
    result = subprocess.run(argv, capture_output=True, timeout=timeout)
    if result.returncode:
        raise NativeError("Native command failed; inspect private evidence and the service journal before recovery")
    return result.stdout


def specification(value: Any) -> dict[str, Any]:
    fields = {"schema_version", "deployment", "data_dir", "uid", "gid", "publish_host", "publish_port",
              "serial_device", "serial_gid", "serial_options", "backend_port", "playback_agents", "from_compose"}
    if (not is_mapping(value) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise NativeError("Select exactly the documented prepared native deployment fields")
    identity(value["deployment"])
    for key in ("data_dir", "serial_device"):
        path = value[key]
        if (not isinstance(path, str) or not SAFE_PATH.fullmatch(path)
                or ".." in Path(path).parts or str(Path(path)) != path or path == "/"):
            raise NativeError("Native paths must be explicit, normalized absolute paths without expansion tokens")
    if not value["serial_device"].startswith("/dev/"):
        raise NativeError("Native serial device must be below /dev")
    for key in ("uid", "gid", "serial_gid"):
        if type(value[key]) is not int or not 0 <= value[key] < 2**31:
            raise NativeError("Native account and device groups must be explicit numeric identities")
    if value["uid"] == 0:
        raise NativeError("Native web and gateway must not run as root")
    for key in ("publish_port", "backend_port"):
        if type(value[key]) is not int or not 1024 <= value[key] <= 65535:
            raise NativeError("Native listeners require unprivileged ports in 1024..65535")
    if value["publish_port"] == value["backend_port"]:
        raise NativeError("Native web and serial listeners must differ")
    try:
        address = ipaddress.ip_address(value["publish_host"])
        if address.version != 4 or address.is_multicast or address.is_link_local:
            raise ValueError
    except (ValueError, TypeError):
        raise NativeError("Native web listener must be an explicit IPv4 address, wildcard or loopback") from None
    if value["serial_options"] not in ("19200n81", "9600n81"):
        raise NativeError("Select an explicitly supported serial framing")
    from web.playback_status import PlaybackAgentConfig
    agents = PlaybackAgentConfig.from_json(json.dumps(value["playback_agents"]))
    if not agents:
        raise NativeError("Prepared native acceptance requires the existing playback registry")
    if value["from_compose"] is not None:
        from tool.deployment_compose import specification as compose_specification
        previous = compose_specification(value["from_compose"])
        if (previous["gateway"] != "managed" or previous["deployment"] != value["deployment"]
                or any(previous[key] != value[key] for key in
                       ("data_dir", "uid", "gid", "publish_host", "publish_port", "serial_device", "serial_gid"))
                or not previous["require_controller"] or not previous["require_playback"]):
            raise NativeError("Cutover must explicitly select the matching prepared managed Compose deployment")
    return copy.deepcopy(value)


def data_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {**spec, "gateway": "managed", "require_playback": True}


def environment(spec: dict[str, Any], *, native: bool) -> dict[str, str]:
    agents = copy.deepcopy(spec["playback_agents"])
    if native:
        for agent in agents:
            for key in ("token_file", "tls_ca_file"):
                if key not in agent:
                    continue
                path = Path(agent[key])
                if not path.is_relative_to("/run/secrets") or ".." in path.parts:
                    raise NativeError("Playback credentials must use the existing protected secrets bind")
                agent[key] = str(Path(spec["data_dir"]) / "secrets" / path.relative_to("/run/secrets"))
    return {"RUSSOUND_BACKEND_HOST": "127.0.0.1", "RUSSOUND_BACKEND_PORT": str(spec["backend_port"]),
            "RUSSOUND_PLAYBACK_AGENTS": json.dumps(agents, sort_keys=True, separators=(",", ":")),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


def unit_files(spec: dict[str, Any], selection: dict[str, Any], operation: str) -> dict[str, bytes]:
    release = RELEASES / selection["release"]
    root = Path(spec["data_dir"])
    env = "\n".join("Environment=" + json.dumps(key + "=" + value).replace("%", "%%")
                    for key, value in environment(spec, native=True).items())
    common = (f"User={spec['uid']}\nGroup={spec['gid']}\n"
              "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n"
              "NoNewPrivileges=true\nUMask=0077\n")
    web = (
        "[Unit]\nDescription=Reviewed native Russound web\n"
        f"After=network-online.target {GATEWAY}\nWants=network-online.target\nRequires={GATEWAY}\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={release}/source\n"
        f"ExecStart={release}/venv/bin/python -m web.russound_server "
        f"--config {root}/config/russound_config.json --state {root}/data/russound_state.json "
        f"--host {spec['publish_host']} --port {spec['publish_port']} --no-debug\n"
        f"{common}{env}\n[Install]\nWantedBy=multi-user.target\n")
    gateway = (
        "[Unit]\nDescription=Reviewed native loopback Russound serial gateway\nAfter=network.target\n"
        "[Service]\nType=simple\n"
        f"ExecStart={SER2NET} -n -c {RUNTIME}/{operation}/ser2net.yaml\n"
        f"{common}SupplementaryGroups={spec['serial_gid']}\n"
        "[Install]\nWantedBy=multi-user.target\n")
    return {WEB: web.encode(), GATEWAY: gateway.encode()}


def gateway_config(spec: dict[str, Any]) -> bytes:
    return (
        "connection: &russound\n"
        f"  accepter: tcp,127.0.0.1,{spec['backend_port']}\n"
        "  enable: on\n"
        "  options:\n"
        "    telnet-brk-on-sync: true\n"
        "    connector-retry-time: 5\n"
        f"  connector: serialdev,{spec['serial_device']},{spec['serial_options']},local\n").encode()


def unit_status(name: str) -> dict[str, str]:
    result = subprocess.run(["systemctl", "show", name, "--no-pager",
                             "--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,MainPID,NRestarts,DropInPaths"],
                            capture_output=True, timeout=20)
    value = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
    if result.returncode not in (0, 1, 4) or not {"LoadState", "ActiveState", "MainPID"} <= value.keys():
        raise NativeError("Cannot establish system unit identity")
    if value.get("DropInPaths"):
        raise NativeError("System unit drop-ins require separate review")
    return value


def system_units() -> dict[str, dict[str, str]]:
    return {name: unit_status(name) for name in (WEB, GATEWAY)}


def verify_units(files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = UNITS / name
        info = safe_path(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or path.read_bytes() != content):
            raise NativeError("Native unit differs from the reviewed installation")


def write_unit(name: str, content: bytes, operation: str) -> None:
    with checked_directory(UNITS, 0, private=False) as directory:
        from tool.deployment_inputs import write_file
        temporary = name + "." + operation
        write_file(directory, temporary, content, owner=0, group=0, mode=0o644)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)


def check_no_writers(spec: dict[str, Any]) -> None:
    if any(item["ActiveState"] not in ("inactive", "failed") or item["MainPID"] != "0"
           for item in system_units().values()):
        raise NativeError("Native writers are not stopped")
    check_host_writers(Path(spec["data_dir"]))
    check_serial(data_spec(spec))


def web_checks(spec: dict[str, Any]) -> None:
    host = "127.0.0.1" if spec["publish_host"] == "0.0.0.0" else spec["publish_host"]
    opener = build_opener(ProxyHandler({}))
    def get(path: str) -> dict[str, Any]:
        with opener.open(f"http://{host}:{spec['publish_port']}{path}", timeout=5) as response:
            value = json.load(response)
        if not is_mapping(value):
            raise NativeError("Native API returned an unexpected response")
        return value
    rooms, playback = get("/api/rooms"), get("/api/playback/health")
    if (rooms.get("config_required", True) or not rooms.get("backend_status", {}).get("connected")
            or playback.get("status") != "ok" or not playback.get("configured")):
        raise NativeError("Native controller or authenticated playback route is not healthy")


def runtime_checks(spec: dict[str, Any], selection: dict[str, Any], operation: str) -> dict[str, str]:
    verify_units(unit_files(spec, selection, operation))
    verify_release(selection)
    config = RUNTIME / operation / "ser2net.yaml"
    info = safe_path(config)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or config.read_bytes() != gateway_config(spec):
        raise NativeError("Native gateway configuration differs from the selected route/device")
    deadline = time.monotonic() + 60
    while True:
        units = system_units()
        if any(item["LoadState"] != "loaded" or item.get("NRestarts") != "0"
               or item.get("FragmentPath") != str(UNITS / name)
               for name, item in units.items()):
            raise NativeError("Native unit identity or restart count failed verification")
        try:
            if any(item["ActiveState"] != "active" or item["MainPID"] == "0"
                   or item.get("UnitFileState") != "enabled" for item in units.values()):
                raise NativeError("Native units are not active and enabled")
            web_checks(spec)
            return dict.fromkeys(CHECKS, "passed")
        except (NativeError, OSError, ValueError):
            if time.monotonic() >= deadline:
                raise NativeError("Native runtime acceptance failed; pending state and backup are retained") from None
            time.sleep(1)


class NativeDeployment:
    def __init__(self, spec: dict[str, Any], selection: dict[str, Any], storage: dict[str, Path], *,
                 operation: str, update: bool):
        self.spec = specification(spec)
        validate_candidate(selection)
        self.selection = copy.deepcopy(selection)
        operation_id(operation)
        self.operation, self.storage = operation, storage
        self.store = StateStore(storage["state"], owner_uid=os.getuid())
        state = self.store.status()
        if state["blocked"]:
            raise NativeError("Unresolved deployment state blocks native review")
        self.previous = state["deployments"].get(spec["deployment"], {}).get("committed")
        self.compose: ComposeDeployment | None = None
        self.previous_native: dict[str, Any] | None = None
        self.baseline: dict[str, Any] | None = None
        self.backup_estimate = 0
        if self.previous is not None:
            old = self.previous["selection"]
            if old["candidate"]["kind"] == NATIVE_CANDIDATE:
                if spec["from_compose"] is not None:
                    raise NativeError("Already-native deployments must not repeat Compose cutover")
                asset = storage["assets"] / old["operation"]
                previous_spec = json.loads(private_read(asset / "spec.json"))
                if {**previous_spec, "from_compose": None} != spec:
                    raise NativeError("Native settings/data migration requires separate review")
                self.previous_native = {"spec": previous_spec, "selection": old["candidate"],
                                        "operation": old["operation"]}
            else:
                if spec["from_compose"] is None:
                    raise NativeError("Docker-to-native migration requires explicit from_compose scope")
                self.compose = ComposeDeployment(spec["from_compose"], old["candidate"], storage)
        elif spec["from_compose"] is not None:
            raise NativeError("Cutover requires an already recorded, verified Compose deployment")
        if self.previous is not None and self.previous["selection"]["candidate"] != selection and not update:
            raise NativeError("Changing the committed native/source selection requires explicit --update")
        self.asset = storage["assets"] / operation
        self.asset.mkdir(mode=0o700)
        private_write(self.asset / "spec.json", self.spec)
        private_write(self.asset / "selection.json", self.selection)
        self.files = unit_files(self.spec, self.selection, operation)
        for name, content in self.files.items():
            path = self.asset / name
            path.write_bytes(content)
            path.chmod(0o400)
        self._attempted = False

    def preflight(self) -> None:
        spec = self.spec
        if (json.loads(private_read(self.asset / "spec.json")) != self.spec
                or json.loads(private_read(self.asset / "selection.json")) != self.selection
                or self.files != unit_files(self.spec, self.selection, self.operation)
                or any(private_read(self.asset / name) != content for name, content in self.files.items())):
            raise NativeError("Native inputs changed after preparation")
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise NativeError("Native activation requires Linux root")
        account = pwd.getpwuid(spec["uid"])
        if account.pw_gid != spec["gid"]:
            raise NativeError("Native account primary group differs from the explicit service identity")
        release = verify_release(self.selection)
        for path in (RELEASES, RUNTIME, UNITS, self.storage["restores"]):
            if overlap(Path(spec["data_dir"]), path):
                raise NativeError("Native data and control/runtime storage must not overlap")
        check_data(data_spec(spec))
        validate_runtime_files(data_spec(spec), environment(spec, native=False))
        from web.config_types import validate_russound_config
        configuration = json.loads((Path(spec["data_dir"]) / "config/russound_config.json").read_bytes())
        validate_russound_config(configuration)
        if not configuration.get("zones"):
            raise NativeError("Native acceptance requires explicit zone mappings")
        # Candidate imports/config validation execute only as the unprivileged service.
        result = subprocess.run(
            [str(release / "venv/bin/python"), "-c",
             "import json,sys; from pathlib import Path; from web.config_types import validate_russound_config; "
             "validate_russound_config(json.loads(Path(sys.argv[1]).read_bytes())); import web.russound_server",
             str(Path(spec["data_dir"]) / "config/russound_config.json")],
            cwd=release / "source", env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            user=spec["uid"], group=spec["gid"], extra_groups=[], capture_output=True, timeout=30)
        if result.returncode:
            raise NativeError("Candidate code/config or service access validation failed")
        distribution_gateway = unit_status("ser2net.service")
        if (distribution_gateway["ActiveState"] != "inactive"
                or distribution_gateway.get("UnitFileState", "") not in ("disabled", "", "static")):
            raise NativeError("Distribution ser2net must already be inactive and disabled; it is not managed here")
        units = system_units()
        if self.previous_native is None:
            if any(item["LoadState"] != "not-found" or (UNITS / name).exists()
                   or (UNITS / name).is_symlink() for name, item in units.items()):
                raise NativeError("Unrecorded native units require a separately reviewed migration")
        else:
            old = self.previous_native
            verify_units(unit_files(old["spec"], old["selection"], old["operation"]))
            runtime_checks(old["spec"], old["selection"], old["operation"])
        if self.compose is not None:
            self.compose.preflight()
            self.compose.checks(self.compose.unchanged_images() or {})
            serial = self.compose.config["services"]["russound-ser2net"].get("environment", {})
            if serial.get("RUSSOUND_SERIAL_OPTIONS") != spec["serial_options"]:
                raise NativeError("Docker-to-native cutover must preserve the prepared serial framing")
            policies = [item["host"]["RestartPolicy"]["Name"] for item in self.compose.baseline or []]
            if any(policy == "always" for policy in policies):
                raise NativeError("Docker cutover requires stop-persistent restart policies, not always")
        self.check_containers()
        if self.previous_native is None:
            check_port("127.0.0.1", spec["backend_port"])
        if self.previous is None:
            check_port(spec["publish_host"], spec["publish_port"])
            check_serial(data_spec(spec))
        baseline: dict[str, Any] = {"origin": "compose" if self.compose else "native" if self.previous_native else "prepared",
                    "units": units, "previous_native": self.previous_native, "committed": self.previous,
                    "compose_asset": str(self.compose.asset) if self.compose else None}
        if self.baseline is not None and self.baseline != baseline:
            raise NativeError("Native runtime identity changed after review")
        self.baseline = baseline
        size = sum(path.stat().st_size for path in Path(spec["data_dir"]).rglob("*") if path.is_file())
        if self.previous_native:
            size += sum(path.stat().st_size for path in
                        (RELEASES / self.previous_native["selection"]["release"]).rglob("*") if path.is_file())
        self.check_backup_space(size)
        if Path(spec["data_dir"]).parent.stat().st_dev != self.storage["restores"].stat().st_dev:
            raise NativeError("Native recovery needs a same-filesystem data restore")
        if RELEASES.stat().st_dev != self.storage["restores"].stat().st_dev:
            raise NativeError("Native releases and isolated restoration must share a filesystem")
        if RUNTIME.exists():
            with checked_directory(RUNTIME, 0, private=False):
                pass
            if stat.S_IMODE(RUNTIME.stat().st_mode) != 0o755:
                raise NativeError("Native runtime storage must already be root-owned mode 0755")
            if RUNTIME.stat().st_dev != self.storage["restores"].stat().st_dev:
                raise NativeError("Native runtime and isolated restoration must share a filesystem")
        command(["systemd-analyze", "verify", *[str(self.asset / name) for name in self.files]])

    def check_backup_space(self, size: int) -> None:
        margin = 64 * 1024**2
        required = max(3 * size, self.compose.backup_estimate - margin if self.compose else 0)
        if not self.backup_estimate:
            self.backup_estimate = required + margin
        elif required > self.backup_estimate:
            raise NativeError("Recovery contents outgrew the reviewed backup capacity")
        # Keep the reviewed capacity stable while live logs grow within its margin.
        if shutil.disk_usage(self.storage["backups"]).free < 3 * self.backup_estimate:
            raise NativeError("Native recovery needs adequate backup space")

    def check_containers(self) -> None:
        if shutil.which("docker") is None:
            return
        from tool.deployment_build import local_engine
        docker = ["docker", "--context", local_engine()]
        ids = command(docker + ["ps", "--all", "--quiet"]).decode().split()
        containers: list[dict[str, Any]] = json.loads(command(docker + ["inspect", *ids])) if ids else []
        approved: set[str] = {item["id"] for item in self.compose.baseline or []} if self.compose else set()
        for item in containers:
            devices: list[dict[str, Any]] = item["HostConfig"].get("Devices") or []
            related = (any(mount.get("Type") == "bind" and
                           overlap(Path(mount["Source"]), Path(self.spec["data_dir"]))
                           for mount in item.get("Mounts", []))
                       or any(device["PathOnHost"] == self.spec["serial_device"]
                              for device in devices))
            if related and item["Id"] not in approved and (
                    item["State"]["Running"] or item["HostConfig"]["RestartPolicy"]["Name"] == "always"):
                raise NativeError("Another container can write native data or own the serial adapter")

    def review(self) -> dict[str, Any]:
        if self.baseline is None:
            raise NativeError("Native review requires preflight")
        plan: dict[str, Any] = {"kind": "native-deployment-review", "operation": self.operation,
                "deployment": self.spec["deployment"], "selection": self.selection,
                "scope": {"origin": self.baseline["origin"], "services": [WEB, GATEWAY],
                          "service_identity": {"uid": self.spec["uid"], "gid": self.spec["gid"]},
                          "serial": {"device": self.spec["serial_device"], "gid": self.spec["serial_gid"],
                                     "options": self.spec["serial_options"]},
                          "listener": {"host": self.spec["publish_host"], "port": self.spec["publish_port"]},
                          "gateway_listener": {"host": "127.0.0.1", "port": self.spec["backend_port"]},
                          "playback_agents": [item["agent_id"] for item in self.spec["playback_agents"]],
                          "offhost_free_bytes_required": self.backup_estimate,
                          "audio_agent_proxy_and_os": "untouched"},
                "interrupt_web_and_gateway": True, "automatic_rollback": False}
        return {"plan": plan, "digest": "sha256:" + hashlib.sha256(encoded({
            "plan": plan, "spec": self.spec, "baseline": self.baseline,
        })).hexdigest()}

    def quiescent(self) -> None:
        check_no_writers(self.spec)
        if self.previous_native:
            old = self.previous_native
            verify_units(unit_files(old["spec"], old["selection"], old["operation"]))
        elif any((UNITS / name).exists() or (UNITS / name).is_symlink() for name in self.files):
            raise NativeError("Native unit files appeared after review")
        if self.compose:
            self.compose.quiescent()
        self.check_containers()

    def execute(self, approve: Callable[[dict[str, Any]], str | None],
                offhost: Callable[[FileBackup], None]) -> dict[str, Any]:
        if self._attempted:
            raise NativeError("Native execution is one-shot; inspect pending state before recovery")
        self.preflight()
        review = self.review()
        if approve(copy.deepcopy(review)) != review["digest"]:
            raise NativeError("Native deployment was declined; services were not changed")
        self.preflight()
        if self.review() != review:
            raise NativeError("Native scope changed after approval")
        if self.previous_native and self.previous_native["selection"] == self.selection:
            checks = runtime_checks(self.spec, self.selection, self.previous_native["operation"])
            return {"event": "unchanged", "operation": self.operation,
                    "deployment": self.spec["deployment"], "required": checks}
        self._attempted = True
        deployment, operation = self.spec["deployment"], self.operation
        private_write(self.asset / "baseline.json", self.baseline)
        private_write(self.storage["evidence"] / (operation + ".intent.json"), review)
        self.store.begin(deployment, operation, self.selection, review["digest"], list(CHECKS),
                         expected_committed=self.previous)
        self.store.advance(deployment, operation, "inputs-verified")
        sources = {"data": Path(self.spec["data_dir"]), "native": self.asset}
        if self.compose:
            private_write(self.compose.asset / "baseline.json", self.compose.baseline)
            self.compose.stop(recovery_operation=operation)
            sources["deployment"] = self.compose.asset
        elif self.previous_native:
            command(["systemctl", "stop", WEB, GATEWAY])
            sources["previous-release"] = RELEASES / self.previous_native["selection"]["release"]
            sources["previous-runtime"] = RUNTIME / self.previous_native["operation"]
        self.quiescent()
        backup = FileBackup.capture(self.storage["backups"], operation, sources,
                                    verify_quiescent=self.quiescent, source_owner_uid=self.spec["uid"])
        if offhost(backup) is not None:
            raise NativeError("Off-host verification must raise on failure, not return a status")
        backup.verify()
        self.quiescent()
        private_write(self.storage["evidence"] / (operation + ".backup.json"),
                      {"operation": operation, "manifest_sha256": backup.manifest_sha256})
        self.store.advance(deployment, operation, "backup-complete", backup=operation)
        verify_release(self.selection)
        check_port(self.spec["publish_host"], self.spec["publish_port"])
        check_port("127.0.0.1", self.spec["backend_port"])
        private_write(self.storage["evidence"] / (operation + ".apply-attempt.json"), review)
        if not RUNTIME.exists():
            RUNTIME.mkdir(mode=0o755)
            RUNTIME.chmod(0o755)
        with checked_directory(RUNTIME, 0, private=False):
            pass
        if stat.S_IMODE(RUNTIME.stat().st_mode) != 0o755:
            raise NativeError("Native runtime storage must already be root-owned mode 0755")
        runtime = RUNTIME / operation
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        config = runtime / "ser2net.yaml"
        config.write_bytes(gateway_config(self.spec))
        config.chmod(0o644)
        for name, content in self.files.items():
            write_unit(name, content, operation)
        command(["systemctl", "daemon-reload"])
        command(["systemctl", "enable", "--now", GATEWAY, WEB], timeout=90)
        self.store.advance(deployment, operation, "apply-complete")
        checks = runtime_checks(self.spec, self.selection, operation)
        self.store.advance(deployment, operation, "checks-complete")
        receipt: dict[str, Any] = {"operation": operation, "intent_digest": review["digest"],
                   "invocation": "verified-complete", "required": checks}
        self.store.finalize(deployment, operation, receipt)
        return {"event": "complete", "operation": operation, "deployment": deployment, "required": checks}


def recovery_assets(path: Path, operation: str, checksum: str) -> dict[str, Any]:
    manifest = portable_backup(path, operation, checksum)
    entries = {item["path"]: item for item in manifest["entries"] if item["type"] == "file"}
    def value(name: str) -> Any:
        entry = entries.get("native/" + name + ".json")
        if entry is None:
            raise NativeError("Native backup lacks required identity assets")
        return json.loads((path / entry["blob"]).read_bytes())
    spec = specification(value("spec"))
    selection = value("selection")
    validate_candidate(selection)
    baseline = value("baseline")
    if (not is_mapping(baseline) or set(baseline) != {
            "origin", "units", "previous_native", "committed", "compose_asset"}
            or baseline["origin"] not in ("prepared", "native", "compose")
            or manifest["sources"].get("data") != spec["data_dir"]):
        raise NativeError("Native recovery baseline or data identity is invalid")
    if baseline["origin"] == "compose":
        from tool.deployment_recovery import recovery_assets as compose_assets
        previous_spec, _ = compose_assets(path, operation, checksum)
        if spec["from_compose"] != previous_spec:
            raise NativeError("Native and Compose recovery scopes differ")
    if baseline["origin"] == "native":
        old = baseline["previous_native"]
        validate_candidate(old["selection"])
        specification(old["spec"])
        operation_id(old["operation"])
        expected = {"previous-release": str(RELEASES / old["selection"]["release"]),
                    "previous-runtime": str(RUNTIME / old["operation"])}
        if any(manifest["sources"].get(key) != value for key, value in expected.items()):
            raise NativeError("Previous native runtime roots differ from the captured selection")
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
        if digest != old["selection"]["sha256"]:
            raise NativeError("Off-host native source/environment bytes differ from the previous release")
    return {"spec": spec, "selection": selection, "baseline": baseline, "manifest": manifest}


def stop_native(spec: dict[str, Any], selection: dict[str, Any], operation: str,
                baseline: dict[str, Any]) -> None:
    allowed = [unit_files(spec, selection, operation)]
    if baseline["previous_native"]:
        old = baseline["previous_native"]
        allowed.append(unit_files(old["spec"], old["selection"], old["operation"]))
    loaded: list[str] = []
    for name, status in system_units().items():
        path = UNITS / name
        if status["LoadState"] == "not-found" and not path.exists() and not path.is_symlink():
            continue
        info = safe_path(path)
        if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode)
                or not any(path.read_bytes() == files[name] for files in allowed)):
            raise NativeError("Unexpected native units block recovery; nothing will be overwritten")
        if status["LoadState"] != "not-found":
            loaded.append(name)
        elif status["ActiveState"] != "inactive" or status["MainPID"] != "0":
            raise NativeError("Unloaded native unit reports a live process; inspect before recovery")
    if loaded:
        command(["systemctl", "stop", *loaded], timeout=90)


def preserve_replace(source: Path, destination: Path, recovery: str, owner: int) -> None:
    """Keep the post-backup tree beside its original path; never overlay or delete."""
    retained = destination.with_name(destination.name + ".failed-" + recovery)
    with checked_directory(destination.parent, owner, private=False) as parent:
        if retained.exists() or retained.is_symlink():
            raise NativeError("Recovery preservation destination already exists")
        if destination.exists() or destination.is_symlink():
            safe_path(destination)
            os.rename(destination.name, retained.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        os.rename(source, destination.name, dst_dir_fd=parent)
        os.fsync(parent)


def compose_original(storage: dict[str, Path], spec: dict[str, Any], baseline: dict[str, Any],
                     compose_file: Path) -> ComposeDeployment:
    previous = baseline["committed"]
    if previous is None or spec["from_compose"] is None:
        raise NativeError("No recorded Compose baseline exists")
    target_spec: dict[str, Any] = {**spec["from_compose"], "compose": str(compose_file), "adopt": True}
    target = ComposeDeployment(target_spec, previous["selection"]["candidate"], storage, restoring_native=True)
    own = target.own(target.inspect_all())
    if any(item["Config"]["Labels"].get("com.docker.compose.service") not in target.services for item in own):
        raise NativeError("Unexpected Compose services block native recovery")
    target.baseline = target.current_signature(own)
    return target


def recover(storage: dict[str, Path], deployment: str, operation: str, backup_path: Path, checksum: str,
            approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    """Restore a failed operation or explicitly roll back the latest native commit."""
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    state = store.status()
    record = state["deployments"].get(deployment)
    if record is None:
        raise NativeError("Native recovery has no matching deployment record")
    pending = record["pending"]
    committed = record["committed"]
    failed = pending is not None and pending["operation"] == operation
    latest = (not state["blocked"] and pending is None and committed is not None
              and committed["selection"]["operation"] == operation)
    if not (failed or latest):
        raise NativeError("Native recovery requires the failed or latest committed operation")
    if failed:
        current = pending
    elif committed is not None:
        current = committed["selection"]
    else:
        raise NativeError("Native recovery has no current selection")
    if not is_mapping(current):
        raise NativeError("Native recovery selection is invalid")
    assets = recovery_assets(backup_path, operation, checksum)
    spec, selection, baseline = assets["spec"], assets["selection"], assets["baseline"]
    if (spec["deployment"] != deployment or current["candidate"] != selection
            or selection["kind"] != NATIVE_CANDIDATE):
        raise NativeError("Native recovery identity differs from the selected operation")
    manifest = assets["manifest"]
    if (shutil.disk_usage(storage["restores"]).free < manifest["total_bytes"] + 64 * 1024**2
            or Path(spec["data_dir"]).parent.stat().st_dev != storage["restores"].stat().st_dev
            or RELEASES.stat().st_dev != storage["restores"].stat().st_dev):
        raise NativeError("Native recovery lacks isolated same-filesystem space")
    recovery = str(uuid4())
    plan: dict[str, Any] = {"kind": "native-recovery-review", "deployment": deployment, "operation": operation,
            "recovery": recovery, "previous_runtime": baseline["origin"],
            "restore_matching_source_environment_and_data": True, "preserve_post_backup_trees": True,
            "audio_agent_proxy_and_os": "untouched"}
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"plan": plan, "digest": digest}) != digest:
        raise NativeError("Native recovery was declined; services were not changed")
    if store.status() != state:
        raise NativeError("Deployment state changed after native recovery approval")
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": record,
                   "backup_manifest_sha256": checksum})
    owned_operation = operation
    if latest:
        owned_operation = recovery
        store.begin(deployment, owned_operation, selection, digest, list(RECOVERY_CHECKS),
                    expected_committed=committed)
    check_host_writers(Path(spec["data_dir"]), check_files=False)
    stop_native(spec, selection, operation, baseline)
    target = None
    if baseline["origin"] == "compose":
        entry = next(item for item in manifest["entries"] if item["path"] == "deployment/compose.json")
        compose_file = storage["evidence"] / (recovery + ".compose.json")
        private_write(compose_file, json.loads((backup_path / entry["blob"]).read_bytes()))
        target = compose_original(storage, spec, baseline, compose_file)
        target.command(target.compose() + ["stop", "--timeout", "30"], timeout=90)

    def quiet() -> None:
        check_no_writers(spec)
        if target:
            target.quiescent()
    quiet()
    restored = storage["restores"] / recovery
    FileBackup(backup_path.parent, operation, checksum).restore_new(restored, verify_quiescent=quiet)
    quiet()
    preserve_replace(restored / "data", Path(spec["data_dir"]), recovery, spec["uid"])
    if baseline["origin"] == "native":
        old = baseline["previous_native"]
        preserve_replace(restored / "previous-release", RELEASES / old["selection"]["release"], recovery, 0)
        preserve_replace(restored / "previous-runtime", RUNTIME / old["operation"], recovery, 0)
        verify_release(old["selection"])
        for name, content in unit_files(old["spec"], old["selection"], old["operation"]).items():
            write_unit(name, content, recovery)
        command(["systemctl", "daemon-reload"])
        command(["systemctl", "enable", "--now", GATEWAY, WEB], timeout=90)
        runtime_checks(old["spec"], old["selection"], old["operation"])
    else:
        installed_units = [name for name in (WEB, GATEWAY) if (UNITS / name).exists()]
        if installed_units:
            command(["systemctl", "disable", *installed_units])
            for name in installed_units:
                path = UNITS / name
                os.rename(path, path.with_name(name + ".failed-" + recovery))
            command(["systemctl", "daemon-reload"])
        if target:
            from tool.deployment_recovery import load_recovery_images, recovery_assets as compose_assets
            from tool.deployment_compose import apply, runtime_checks as compose_checks
            _, images = compose_assets(backup_path, operation, checksum)
            loaded = load_recovery_images(target, restored / "deployment", images)
            apply(target.asset / "compose.json", loaded)
            compose_checks(target, loaded)
        else:
            quiet()
    receipt: dict[str, Any] = {"operation": owned_operation, "review_digest": digest, "required": RECOVERY_CHECKS}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, owned_operation, receipt, restore_native=True,
                           native_previous=baseline["committed"])
    return {"event": "recovered", "operation": operation, "recovery": recovery, "required": RECOVERY_CHECKS}


def resume_original(storage: dict[str, Path], deployment: str, operation: str,
                    approve: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
    operation_id(operation)
    store = StateStore(storage["state"], owner_uid=os.getuid())
    state = store.status()
    record = state["deployments"].get(deployment)
    if (record is None or record["pending"] is None or record["pending"]["operation"] != operation
            or record["pending"]["candidate"]["kind"] != NATIVE_CANDIDATE
            or (storage["evidence"] / (operation + ".apply-attempt.json")).exists()):
        raise NativeError("Native original-runtime resolution requires a matching pre-apply operation")
    asset = storage["assets"] / operation
    spec = specification(json.loads(private_read(asset / "spec.json")))
    selection = json.loads(private_read(asset / "selection.json"))
    baseline = json.loads(private_read(asset / "baseline.json"))
    if spec["deployment"] != deployment or selection != record["pending"]["candidate"]:
        raise NativeError("Native original-runtime scope differs from the reserved operation")
    if baseline["origin"] == "native":
        old = baseline["previous_native"]
        verify_units(unit_files(old["spec"], old["selection"], old["operation"]))
        verify_release(old["selection"])
    elif any(status["LoadState"] != "not-found" or (UNITS / name).exists() or (UNITS / name).is_symlink()
             for name, status in system_units().items()):
        raise NativeError("Native units appeared before apply; use verified backup recovery")
    plan: dict[str, Any] = {"kind": "resume-native-original-runtime", "deployment": deployment, "operation": operation,
            "previous_runtime": baseline["origin"], "restore_data": False, "preserve_operator_edits": True}
    digest = "sha256:" + hashlib.sha256(encoded(plan)).hexdigest()
    if approve({"plan": plan, "digest": digest}) != digest:
        raise NativeError("Original native runtime restart was declined")
    if store.status() != state:
        raise NativeError("Pending native state changed after approval")
    private_write(storage["evidence"] / (operation + ".recovery-attempt.json"),
                  {"review": plan, "review_digest": digest, "original_record": record})
    stop_native(spec, selection, operation, baseline)
    if baseline["origin"] == "compose":
        asset = Path(baseline["compose_asset"])
        target = compose_original(storage, spec, baseline, asset / "compose.json")
        expected = json.loads(private_read(asset / "baseline.json"))
        if target.baseline != expected:
            raise NativeError("Original containers changed; use verified native backup recovery")
        target.command(target.compose() + ["stop", "--timeout", "30"], timeout=90)
        target.quiescent()
        from tool.deployment_compose import apply, runtime_checks as compose_checks
        images = {target.services[item["config"]["Labels"]["com.docker.compose.service"]]: item["image"]
                  for item in expected}
        apply(target.asset / "compose.json", images)
        compose_checks(target, images)
    elif baseline["origin"] == "native":
        old = baseline["previous_native"]
        verify_units(unit_files(old["spec"], old["selection"], old["operation"]))
        verify_release(old["selection"])
        check_no_writers(spec)
        command(["systemctl", "start", GATEWAY, WEB], timeout=90)
        runtime_checks(old["spec"], old["selection"], old["operation"])
    else:
        check_no_writers(spec)
    receipt: dict[str, Any] = {"operation": operation, "review_digest": digest, "required": RECOVERY_CHECKS}
    private_write(storage["evidence"] / (operation + ".recovery-complete.json"), receipt)
    store.resolve_recovery(deployment, operation, receipt, restore_native=True,
                           native_previous=baseline["committed"])
    return {"event": "recovered", "operation": operation, "required": RECOVERY_CHECKS}
