import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TypeAlias, Union
from lib.audioidentity import (
    HardwareSelector, HardwareUnavailable, hardware_configuration, object_mapping,
    graph_properties, resolve_hardware_card, unique_card, validate_downstream, validate_hardware_devices,
)


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


def audio_ready(config_path: str | Path, *, enforce_profile: bool = False) -> None:
    config: JSONValue = json.loads(Path(config_path).read_text(encoding="utf-8"))
    audio_ready_config(config, enforce_profile=enforce_profile)


def audio_ready_config(config: JSONValue, *, enforce_profile: bool = False) -> None:
    """Inspect a captured configuration without re-reading a newer file."""
    if not isinstance(config, dict):
        raise ValueError("Invalid audio configuration")
    mappers = config.get("outputmappers")
    devices = mappers.get("audio_devices") if isinstance(mappers, dict) else None
    if not isinstance(devices, list) or not devices:
        raise ValueError("No audio devices configured")
    validate_hardware_devices(devices)
    cards: JSONValue = json.loads(_pactl("--format=json", "list", "cards"))
    sinks: JSONValue = json.loads(_pactl("--format=json", "list", "sinks"))
    if not isinstance(cards, list) or not isinstance(sinks, list):
        raise RuntimeError("Invalid PulseAudio inventory")
    graph = pipewire_graph() if any(
        isinstance(device, dict) and "hardware-selector" in device for device in devices
    ) else None
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
        selection = hardware_configuration(device)
        if selection is not None:
            selector, mappings = selection
            if graph is None:
                raise RuntimeError("PipeWire inventory is missing")
            hardware_card = resolve_hardware_card(graph, selector)
            pulse_cards = [object_mapping(item) for item in cards]
            card_properties = [
                dict(object_mapping(item.get("properties")), _pulse_card=item)
                for item in pulse_cards
            ]
            selected = unique_card(card_properties, selector)
            card = object_mapping(selected["_pulse_card"])
            if str(selected.get("object.id")) != str(hardware_card["id"]):
                raise HardwareUnavailable("PulseAudio/PipeWire card inventories disagree")
            index = card.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("Invalid PulseAudio card index")
            if card.get("active_profile") != f"output:{profile}":
                if enforce_profile:
                    # Recheck ambiguity and identity immediately before the mutation.
                    fresh = resolve_hardware_card(pipewire_graph(), selector)
                    if (fresh["id"] != hardware_card["id"]
                            or graph_properties(fresh).get("object.serial")
                            != graph_properties(hardware_card).get("object.serial")):
                        raise HardwareUnavailable("Selected USB card changed during recovery")
                    _pactl("set-card-profile", str(index), f"output:{profile}")
                raise HardwareUnavailable(f"Selected USB card is not ready with profile {profile}",
                                          code="hardware_profile_unavailable")
            validate_downstream(graph, selector, mappings)
            for sink_name in mappings:
                if f"sink.{sink_name}" not in sink_names:
                    raise HardwareUnavailable(f"Virtual sink sink.{sink_name} is missing")
            continue
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


def pipewire_graph() -> list[dict[str, object]]:
    try:
        raw = subprocess.run(
            ["pw-dump"], check=True, capture_output=True, text=True, timeout=2,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"PipeWire inventory failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("PipeWire inventory timed out") from error
    value: JSONValue = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("Invalid PipeWire inventory")
    return [object_mapping(item) for item in value]


def hardware_preflight(selectors: list[HardwareSelector]) -> None:
    """Refuse ambiguous/missing hardware before installing selector policy."""
    if selectors:
        graph = pipewire_graph()
        for selector in selectors:
            resolve_hardware_card(graph, selector)


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
