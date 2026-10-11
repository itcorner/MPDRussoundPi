import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TypeAlias, Union


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


def audio_ready(config_path: str | Path, *, enforce_profile: bool = False) -> None:
    config: JSONValue = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Invalid audio configuration")
    mappers = config.get("outputmappers")
    devices = mappers.get("audio_devices") if isinstance(mappers, dict) else None
    if not isinstance(devices, list) or not devices:
        raise ValueError("No audio devices configured")
    cards: JSONValue = json.loads(_pactl("--format=json", "list", "cards"))
    sinks: JSONValue = json.loads(_pactl("--format=json", "list", "sinks"))
    if not isinstance(cards, list) or not isinstance(sinks, list):
        raise RuntimeError("Invalid PulseAudio inventory")
    sink_names: set[str] = set()
    for item in sinks:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                sink_names.add(name)
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("Invalid audio device")
        name, profile = device.get("name"), device.get("card-profile")
        if not isinstance(name, str) or not isinstance(profile, str):
            raise ValueError("Audio device requires name and profile")
        card_name = f"alsa_card.{name}"
        card = next((item for item in cards if isinstance(item, dict) and item.get("name") == card_name), None)
        if not isinstance(card, dict):
            raise RuntimeError(f"Audio card {card_name} is missing")
        if card.get("active_profile") != f"output:{profile}":
            if enforce_profile:
                _pactl("set-card-profile", card_name, f"output:{profile}")
            raise RuntimeError(f"Audio card {card_name} is not ready with profile {profile}")
        master = f"alsa_output.{name}.{profile}"
        if master not in sink_names:
            raise RuntimeError(f"Hardware sink {master} is missing")
        expected_sinks = device.get("sinks")
        if not isinstance(expected_sinks, list):
            raise ValueError("Invalid configured sinks")
        for sink in expected_sinks:
            if not isinstance(sink, dict) or not isinstance(sink.get("name"), str):
                raise ValueError("Invalid configured sink")
            if f"sink.{sink['name']}" not in sink_names:
                raise RuntimeError(f"Virtual sink sink.{sink['name']} is missing")


def _pactl(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["pactl", *arguments], check=True, capture_output=True, text=True, timeout=2,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"PulseAudio readiness failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("PulseAudio readiness timed out") from error


def wait_for_audio(config_path: str | Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            audio_ready(config_path, enforce_profile=True)
            return
        except (OSError, RuntimeError) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Audio did not become ready: {error}") from error
            logging.warning("Waiting for audio: %s", error)
            time.sleep(1)
