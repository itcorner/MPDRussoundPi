#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || ( "$2" != amd64 && "$2" != arm64 ) ]]; then
    echo "Usage: bash tool/ci/smoke-web-image.sh IMAGE amd64|arm64" >&2
    exit 2
fi

image=$1
arch=$2
container=

cleanup() {
    result=$?
    trap - EXIT
    if [[ -n "$container" ]]; then
        if [[ "$result" -ne 0 ]]; then
            docker logs --tail 100 "$container" >&2 || echo "Unable to read smoke-test logs" >&2
            docker inspect --format '{{json .State}}' "$container" >&2 || echo "Unable to inspect smoke-test state" >&2
        fi
        if ! docker rm --force --volumes "$container" >/dev/null; then
            echo "Failed to remove smoke-test container and anonymous volumes: $container" >&2
            if [[ "$result" -eq 0 ]]; then result=1; fi
        fi
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# No host mounts, published ports or external network; Docker creates empty volumes.
container=$(docker create --platform "linux/$arch" --network none "$image")
docker start "$container" >/dev/null

health=
for ((attempt = 0; attempt < 60; attempt++)); do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]]; then
        echo "Smoke-test container exited before becoming healthy" >&2
        exit 1
    fi
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container")
    case "$health" in
        healthy) break ;;
        starting) sleep 2 ;;
        *) echo "Unexpected container health: $health" >&2; exit 1 ;;
    esac
done
if [[ "$health" != healthy ]]; then
    echo "Container did not become healthy within the startup deadline" >&2
    exit 1
fi

# The updater decides from this label whether the image can read the data on a host
# (docs/plans/general_open_persisted-data-schema-version.md): it must match the code.
labelled=$(docker image inspect --format '{{index .Config.Labels "net.itcorner.mpdrussoundpi.data-schema"}}' "$image")
expected=$(docker exec "$container" python -c 'from mpdrussoundpi.common.dataschema import label; print(label())')
if [[ -z "$labelled" || "$labelled" != "$expected" ]]; then
    echo "Data schema label '$labelled' does not match the image's code '$expected'" >&2
    exit 1
fi

docker exec -i "$container" python - "$arch" <<'PY'
import json
import os
from pathlib import Path
import platform
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def request(path, payload=None, revision=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if revision is not None:
        headers["X-Config-Revision"] = revision
    req = Request("http://127.0.0.1:8000" + path, data=data, headers=headers)
    with urlopen(req, timeout=10) as response:
        require(response.status == 200, f"{path}: unexpected HTTP status")
        return response.read().decode()


expected_machine = {"amd64": "x86_64", "arm64": "aarch64"}[sys.argv[1]]
require(platform.machine() == expected_machine, f"Wrong runtime architecture: {platform.machine()}")
require(sys.version_info[:2] == (3, 12), "Image must run Python 3.12")
require(os.getuid() == 10001, "Image must run as the non-root application user")
for directory in ("/config", "/data", "/logs"):
    require(os.access(directory, os.W_OK), f"{directory} is not writable")
require(not Path("/config/russound_config.json").exists(), "Config volume was not empty")
require(not Path("/data/russound_state.json").exists(), "State volume was not empty")

for path, marker in (
    ("/", 'id="zones"'),
    ("/config", 'id="saveConfigButton"'),
    ("/playback", "Russound playback"),
    ("/status", "License"),
    ("/static/config.js", "config_required"),
):
    require(marker in request(path), f"{path}: expected UI content missing")

editor = json.loads(request("/api/config"))
require(editor["config_required"] is True, "Empty config must enter setup")
require(editor["config_writable"] is True, "Setup must allow saving configuration")
require(json.loads(request("/api/rooms"))["config_required"] is True, "Rooms snapshot must report setup")
require(editor["configuration"]["load_state"] == "missing", "Empty config must report a missing load state")
revision = editor["configuration"]["revision"]

setup = {
    "backend": {"host": "127.0.0.1", "port": 9, "poll_interval_seconds": 2},
    "controllers": [{"id": 1, "zone_count": 6}],
    "zone_slots": [],
    "source_slots": [],
}
try:
    request("/api/config", setup)
except HTTPError as error:
    require(error.code == 428, f"Save without revision returned HTTP {error.code}, expected 428")
else:
    raise RuntimeError("Save without a config revision was accepted")
require(not Path("/config/russound_config.json").exists(), "Save without revision wrote configuration")

try:
    request("/api/config", {"backend": {}, "controllers": []}, revision)
except HTTPError as error:
    require(error.code == 400, f"Invalid setup returned HTTP {error.code}, expected 400")
    require("error" in json.loads(error.read()), "Invalid setup must return a JSON error")
else:
    raise RuntimeError("Invalid setup was accepted")
require(not Path("/config/russound_config.json").exists(), "Invalid setup wrote configuration")

saved = json.loads(request("/api/config", setup, revision))
require(saved["config_required"] is False, "Valid offline setup was not accepted")
persisted = json.loads(Path("/config/russound_config.json").read_text())
require(persisted["backend"]["host"] == "127.0.0.1", "Saved backend was not persisted")
require(persisted["backend"]["port"] == 9, "Saved backend port was not persisted")
require(json.loads(request("/api/rooms"))["config_required"] is False, "Rooms snapshot did not leave setup")
try:
    request("/api/config", setup, revision)
except HTTPError as error:
    require(error.code == 409, f"Stale revision returned HTTP {error.code}, expected 409")
else:
    raise RuntimeError("Save with a stale config revision was accepted")
print(f"PASS: {sys.argv[1]} health, UI, setup validation, revision checks, offline save and persistence")
PY
