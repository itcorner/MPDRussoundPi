"""Small, deterministic admin configuration for playback tests."""

import json
from pathlib import Path


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
