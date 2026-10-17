"""Rename playback outputs to their audio channel identities.

Run only while the playback agent, supervisor and MPD units are stopped. The
stream name (which also names the MPD unit), output ID and label become the
configured sink: for example ``stereo_front`` / ``Stereo Front``. Regenerate the
MPD units with ``mpdsetup.py --install`` afterwards.
"""

import argparse
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from lib.atomicjson import write_json
from lib.mpdbackend import BackendError, JSONValue, MPDBackend
from lib.playbackidentity import identities, output_id, output_label
from lib.setuplock import setup_lock

Rename = tuple[str, str, str]


def channel_renames(config: Mapping[str, JSONValue]) -> dict[str, Rename]:
    """Map each legacy stream name to its (name, id, label) channel identity."""
    descriptions: dict[str, str] = {}
    mappers = config.get("outputmappers")
    devices = mappers.get("audio_devices") if isinstance(mappers, dict) else None
    for device in devices if isinstance(devices, list) else []:
        sinks = device.get("sinks") if isinstance(device, dict) else None
        for sink in sinks if isinstance(sinks, list) else []:
            if isinstance(sink, dict) and isinstance(sink.get("name"), str):
                description = sink.get("sink_description")
                descriptions[cast(str, sink["name"])] = (
                    description if isinstance(description, str) and description.strip()
                    else cast(str, sink["name"])
                )
    entries = config.get("streams")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Configuration requires a nonempty streams list")
    renames: dict[str, Rename] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("Invalid stream entry")
        audio = entry.get("audio_output")
        sink = audio.get("sink") if isinstance(audio, dict) else None
        if not isinstance(sink, str) or sink not in descriptions:
            raise ValueError(f"Stream {entry['name']!r} has no configured sink")
        label = output_label(descriptions[sink])
        renames[cast(str, entry["name"])] = (label, output_id(sink), label)
    targets = [rename[0].replace(" ", "_").lower() for rename in renames.values()]
    if len(set(targets)) != len(targets) or len({rename[1] for rename in renames.values()}) != len(renames):
        raise ValueError("Each stream must use a distinct sink to receive a channel identity")
    return renames


def assert_operations_idle(path: Path) -> None:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        busy = db.execute(
            "SELECT output_id, status FROM operations WHERE status IN ('queued', 'running')",
        ).fetchall()
    if busy:
        raise ValueError(f"Operations still pending: {busy}; let them finish before renaming")


def rename_outputs(config_path: Path, state_path: Path, *, dry_run: bool = False) -> dict[str, Rename]:
    if dry_run:
        return channel_renames(MPDBackend(config_path, state_path).configuration_snapshot()[0])
    with setup_lock("mpd_control.lock"):
        backend = MPDBackend(config_path, state_path)
        config, _, _ = backend.configuration_snapshot()
        renames = channel_renames(config)
        intentions: dict[str, JSONValue] = {}
        if state_path.exists():
            loaded: JSONValue = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Invalid persisted playback intentions")
            unknown = set(loaded) - set(renames)
            if unknown:
                raise ValueError(f"Intentions reference unknown streams: {sorted(unknown)}")
            intentions = {renames[name][0]: state for name, state in loaded.items()}
        entries = cast(list[dict[str, JSONValue]], config["streams"])
        for entry in entries:
            entry["name"], entry["id"], entry["label"] = renames[cast(str, entry["name"])]
        identities(entries, required=True)
        original = config_path.read_bytes()
        write_json(config_path, config)
        try:
            MPDBackend(config_path, state_path).list_streams()
            if state_path.exists():
                write_json(state_path, intentions)
        except BaseException:
            config_path.write_bytes(original)
            raise
        return renames


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    parser.add_argument("--intentions", type=Path, help="Playback intentions file (default: backend default)")
    parser.add_argument("--operations-db", type=Path, help="Refuse to run while this agent database has pending operations")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned renames without changing files")
    args = parser.parse_args(argv)
    try:
        state_path = args.intentions or MPDBackend(args.config).state_path
        if args.operations_db is not None:
            assert_operations_idle(args.operations_db)
        renames = rename_outputs(args.config, state_path, dry_run=args.dry_run)
    except (BackendError, OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Output rename failed: {error}\n")
    print(json.dumps({old: {"name": new[0], "id": new[1], "label": new[2]} for old, new in renames.items()}, indent=2))
    if not args.dry_run:
        print("Configuration and intentions renamed. Remove the old mpd_*.service/.conf files, "
              "then run mpdsetup.py --install.")


if __name__ == "__main__":
    main()
