"""Small, deterministic admin configuration for playback tests."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch


def configuration():
    seed = Path(__file__).resolve().parents[1] / "mpdrussoundpi/russound/config.json"
    hardware = json.loads(seed.read_text())["outputmappers"]
    return {
        "outputmappers": hardware,
        "outputs": [
            {"id": "front", "label": "Front", "port": 6601, "sink": "stereo_front",
             "default_station": "radio", "default_gain_db": -7.1},
            {"id": "rear", "label": "Rear", "port": 6603, "sink": "stereo_rear"},
        ],
        "stations": [
            {"key": "radio", "name": "Private Radio", "type": "url",
             "url": "https://radio.example/private-stream?token=secret"},
            {"key": "tunein:s8007", "name": "TuneIn Radio", "type": "tunein", "station_id": "s8007"},
        ],
    }


@contextmanager
def installed_commands(directory, *names):
    """Pretend the package's console commands live next to the interpreter.

    Generated units resolve commands in ``Path(sys.executable).parent``; this
    creates that ``bin`` directory with the given command files.
    """
    bin_dir = Path(directory) / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (bin_dir / name).write_text("#!/bin/sh\n")
    with patch.object(sys, "executable", str(bin_dir / "python")):
        yield bin_dir
