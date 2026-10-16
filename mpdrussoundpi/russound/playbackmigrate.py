"""Prepare a private offline bundle; never install files or contact audio services."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import cast

from lib.mpdbackend import BackendError, JSONValue, MPDBackend
from lib.playbackidentity import identities


def control_preview(config: dict[str, JSONValue], intentions: object) -> dict[str, JSONValue]:
    known = identities(config.get("streams"), required=True)
    if not isinstance(intentions, dict):
        raise ValueError("Legacy intentions must be an object")
    saved = cast(dict[str, object], intentions)
    if set(saved) != set(known):
        raise ValueError("Intentions must exactly cover configured legacy stream names")
    outputs: dict[str, JSONValue] = {}
    for name, (identity, _) in known.items():
        state = saved[name]
        if not isinstance(state, str) or state not in ("play", "pause", "stop"):
            raise ValueError(f"Invalid playback intention for {name}")
        outputs[identity] = {"mode": "manual", "intention": state}
    return {"schema_version": 1, "outputs": outputs}


def validate_control_preview(value: JSONValue, config: dict[str, JSONValue]) -> None:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "outputs"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise ValueError("Unsupported control preview schema")
    known = identities(config.get("streams"), required=True)
    outputs = value["outputs"]
    if not isinstance(outputs, dict) or set(outputs) != {item[0] for item in known.values()}:
        raise ValueError("Control preview output IDs do not match configuration")
    for entry in outputs.values():
        if (not isinstance(entry, dict) or set(entry) != {"mode", "intention"}
                or entry["mode"] != "manual" or entry["intention"] not in ("play", "pause", "stop")):
            raise ValueError("Invalid manual control preview")


def prepare_bundle(config_path: Path, state_path: Path, mapping_path: Path, output: Path) -> str:
    backend = MPDBackend(config_path, state_path)
    config, _, revision = backend.configuration_snapshot()
    original = config_path.read_bytes()
    if hashlib.sha256(original).hexdigest() != revision:
        raise ValueError("Configuration changed while preparing migration")
    state = state_path.read_bytes()
    mapping: JSONValue = json.loads(mapping_path.read_text(encoding="utf-8"))
    entries = config["streams"]
    if not isinstance(entries, list) or not isinstance(mapping, dict):
        raise ValueError("Identity mapping must be an object keyed by legacy stream name")
    existing = identities(entries)
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("Invalid stream entry")
        name = cast(str, entry["name"])
        names.add(name)
        identity = mapping.get(name)
        if not isinstance(identity, dict) or set(identity) != {"id", "label"}:
            raise ValueError(f"Missing or invalid identity mapping for {name}")
        if name in existing and identity["id"] != existing[name][0]:
            raise ValueError(f"Cannot change persisted output ID for {name}")
        entry.update(identity)
    if set(mapping) != names:
        raise ValueError("Identity mapping must exactly cover configured stream names")
    identities(entries, required=True)
    preview = control_preview(config, json.loads(state))
    validate_control_preview(preview, config)

    def encoded(value: object) -> bytes:
        return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")

    files = {
        "config.original.json": original,
        "playback.original.json": state,
        "config.prepared.json": encoded(config),
        "playback.legacy.json": state,
        "control-v1.preview.json": encoded(preview),
    }
    prepared_revision = hashlib.sha256(files["config.prepared.json"]).hexdigest()
    files["manifest.json"] = encoded({
        "bundle_version": 1, "activation": "offline-only",
        "original_config_revision": revision, "prepared_config_revision": prepared_revision,
        "original_modes": {
            "config": config_path.stat().st_mode & 0o777,
            "intentions": state_path.stat().st_mode & 0o777,
        },
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    })
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    created: list[Path] = []
    try:
        for name, data in files.items():
            path = output / name
            with path.open("xb") as stream:
                created.append(path)
                path.chmod(0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        for path in reversed(created):
            path.unlink()
        output.rmdir()
        raise
    return prepared_revision


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--intentions", required=True, type=Path)
    parser.add_argument("--identities", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New offline bundle directory")
    args = parser.parse_args(argv)
    try:
        revision = prepare_bundle(args.config, args.intentions, args.identities, args.output)
    except (BackendError, OSError, ValueError) as error:
        parser.exit(1, f"Offline migration preparation failed: {error}\n")
    print(f"Offline bundle prepared; config revision {revision}. No operational files changed.")


if __name__ == "__main__":
    main()
