"""Preparation-only inputs for the one-time Pi native-to-Compose handover.

This module does not reserve state, stop services or deploy anything. Its
output must not be used for activation without a reviewed transaction and
matching recovery path.
"""

import copy
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tool.deployment_compose import specification as compose_specification
from tool.deployment_local import LOCAL_CANDIDATE
from tool.deployment_native import environment, specification as native_specification
from tool.deployment_native_source import NativeError, validate_candidate as native_candidate
from tool.deployment_state import candidate, identity


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
