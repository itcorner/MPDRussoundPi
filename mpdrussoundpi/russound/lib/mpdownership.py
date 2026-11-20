"""Exact configured MPD mutation targets and fail-closed unit ownership checks."""

import json
import os
from pathlib import Path
import shlex
import stat
import subprocess

from .outputconfig import parse_configuration


MARKER = "# Managed by MPDRussoundPi; component=mpd; schema=1\n"
EGRESS_MARKER = "# Managed by MPDRussoundPi; component=egress; schema=1\n"
CONTROL_UNITS = ("mpd_meta_control.service", "mpd_playback_supervisor.service")


def owned_directory(path: Path, *, missing: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise ValueError(f"Required managed directory is absent: {path}") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError(f"Unsafe MPD directory: {path}")


def configured_units(config: Path) -> list[str]:
    configuration = parse_configuration(config.read_bytes())
    return [f"mpd_{entry.id}.service" for entry in configuration.outputs]


def owned_file(path: Path, marker: str = MARKER, *, missing: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise ValueError(f"Required managed file is absent: {path}") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError(f"Unowned, unsafe or legacy file; inspect and explicitly migrate before continuing: {path}")
    with path.open("rb") as managed_file:
        if managed_file.read(len(marker.encode())) != marker.encode():
            raise ValueError(f"Unowned, unsafe or legacy file; inspect and explicitly migrate before continuing: {path}")


def preflight(config: Path, unit_root: Path, config_root: Path, *, install: bool = False,
              controls: bool = True) -> list[str]:
    names = configured_units(config)
    if not names:
        raise ValueError("No configured MPD outputs")
    for root in (unit_root, config_root):
        owned_directory(root, missing=install)
    selected = names + (list(CONTROL_UNITS) if controls else [])
    for name in selected:
        path = unit_root / name
        owned_file(path, missing=install)
        dropin = unit_root / (name + ".d")
        owned_directory(dropin, missing=True)
        allowed = dropin / "50-egress.conf"
        if dropin.exists():
            for entry in dropin.iterdir():
                if entry != allowed or name not in names:
                    raise ValueError(f"Unreviewed unit drop-in: {entry}")
                owned_file(entry, EGRESS_MARKER)
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=LoadState", "--property=FragmentPath",
             "--property=DropInPaths", name], capture_output=True, text=True, check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        fragment = values.get("FragmentPath")
        load = values.get("LoadState")
        if (load not in ("loaded", "not-found")
                or result.returncode not in ((0, 1) if load == "not-found" and install else (0,))):
            raise ValueError(f"Cannot verify effective systemd unit: {name}")
        if load == "not-found":
            if not install or fragment or values.get("DropInPaths"):
                raise ValueError(f"Configured unit is not loaded: {name}")
        elif fragment != str(path) or not path.exists():
            raise ValueError(f"Effective unit is overridden outside the owned path: {name}")
        if any(Path(item) != allowed for item in shlex.split(values.get("DropInPaths", ""))):
            raise ValueError(f"Effective unit has unreviewed drop-ins: {name}")
        if name in names:
            owned_file(config_root / (Path(name).stem + ".conf"), missing=install)
    # Stale and unrelated matching names are intentionally reported, never mutated.
    if unit_root.is_dir():
        stale = sorted(entry.name for entry in unit_root.glob("mpd_*.service") if entry.name not in selected)
        if stale:
            import logging
            logging.warning("Leaving unselected MPD units unchanged: %s", ", ".join(stale))
    return names


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    home = Path.home()
    try:
        names = preflight(args.config, home / ".config/systemd/user", home / ".config/mpd", controls=False)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"MPD ownership preflight failed: {error}\n")
    print(json.dumps([{"path": str(home / ".config/systemd/user" / name)} for name in names]))


if __name__ == "__main__":
    main()
