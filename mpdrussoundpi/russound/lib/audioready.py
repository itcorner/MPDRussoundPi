from collections.abc import Generator, Mapping
from contextlib import contextmanager
import fcntl
import json
import logging
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import TextIO, TypeAlias, Union, cast
from .audioidentity import (
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
    if enforce_profile and any(
        isinstance(device, dict) and "hardware-selector" in device for device in devices
    ):
        with _profile_lock() as state:
            _inspect_audio(devices, enforce_profile=True, recovery_state=state)
    else:
        _inspect_audio(devices, enforce_profile=enforce_profile)


@contextmanager
def _profile_lock() -> Generator[TextIO, None, None]:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isabs(runtime):
        raise RuntimeError("Audio recovery requires an absolute XDG_RUNTIME_DIR")
    directory = os.stat(runtime)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700):
        raise RuntimeError("Audio recovery requires a private, user-owned runtime directory")
    descriptor = os.open(
        Path(runtime) / "mpdrussoundpi-audio-profile.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
    )
    with os.fdopen(descriptor, "r+", encoding="utf-8") as state:
        details = os.fstat(state.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1):
            raise RuntimeError("Unsafe audio recovery state file")
        try:
            fcntl.flock(state.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another audio profile recovery is in progress") from error
        try:
            yield state
        finally:
            fcntl.flock(state.fileno(), fcntl.LOCK_UN)


def _inspect_audio(
    devices: list[JSONValue], *, enforce_profile: bool, recovery_state: TextIO | None = None,
) -> None:
    cards: JSONValue = json.loads(_pactl("--format=json", "list", "cards"))
    if not isinstance(cards, list):
        raise RuntimeError("Invalid PulseAudio inventory")
    graph = pipewire_graph() if any(
        isinstance(device, dict) and "hardware-selector" in device for device in devices
    ) else None
    if graph is not None and recovery_state is not None:
        for device in devices:
            selection = hardware_configuration(object_mapping(device))
            if selection is None:
                continue
            selector, _ = selection
            hardware_card, card, index = _selected_card(cards, graph, selector)
            if _missing_profile_inventory(hardware_card, card):
                _recover_session_inventory(selector, hardware_card, index, recovery_state)
            else:
                _clear_session_observation(selector, recovery_state)
    sinks: JSONValue = json.loads(_pactl("--format=json", "list", "sinks"))
    if not isinstance(sinks, list):
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
        selection = hardware_configuration(device)
        if selection is not None:
            selector, mappings = selection
            if graph is None:
                raise RuntimeError("PipeWire inventory is missing")
            hardware_card, card, index = _selected_card(cards, graph, selector)
            desired = f"output:{profile}"
            if card.get("active_profile") != desired or _native_profile(hardware_card) != desired:
                if enforce_profile:
                    if recovery_state is None:
                        raise RuntimeError("Audio profile recovery is not coordinated")
                    _select_profile(selector, mappings, hardware_card, index, profile, recovery_state)
                raise HardwareUnavailable(f"Selected USB card is not ready with profile {profile}",
                                          code="hardware_profile_unavailable")
            validate_downstream(graph, selector, mappings)
            for sink_name in mappings:
                if f"sink.{sink_name}" not in sink_names:
                    raise HardwareUnavailable(f"Virtual sink sink.{sink_name} is missing")
            if recovery_state is not None:
                _clear_profile_observation(selector, recovery_state)
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


def _read_recovery_state(state: TextIO) -> dict[str, object]:
    state.seek(0)
    raw = state.read()
    return object_mapping(json.loads(raw)) if raw else {}


def _write_recovery_state(state: TextIO, records: dict[str, object]) -> None:
    state.seek(0)
    json.dump(records, state)
    state.truncate()
    state.flush()
    os.fsync(state.fileno())


def _selector_key(selector: HardwareSelector) -> str:
    return f"{selector.vendor:04x}:{selector.product:04x}"


def _native_profile(card: Mapping[str, object]) -> str:
    parameters = object_mapping(object_mapping(card.get("info")).get("params", {}))
    profiles = parameters.get("Profile")
    if not isinstance(profiles, list):
        raise HardwareUnavailable("Native USB card profile is unavailable",
                                  code="hardware_profile_unavailable")
    entries = cast(list[object], profiles)
    if len(entries) != 1:
        raise HardwareUnavailable("Native USB card profile is ambiguous",
                                  code="hardware_profile_unavailable")
    name = object_mapping(entries[0]).get("name")
    if not isinstance(name, str):
        raise ValueError("Invalid native USB card profile")
    return name


def _selected_card(
    cards: list[JSONValue], graph: list[dict[str, object]], selector: HardwareSelector,
) -> tuple[Mapping[str, object], dict[str, object], int]:
    hardware = resolve_hardware_card(graph, selector)
    properties = [
        dict(object_mapping(object_mapping(item).get("properties")), _pulse_card=item)
        for item in cards
    ]
    selected = unique_card(properties, selector)
    pulse = object_mapping(selected["_pulse_card"])
    if str(selected.get("object.id")) != str(hardware["id"]):
        raise HardwareUnavailable("PulseAudio/PipeWire card inventories disagree")
    index = pulse.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("Invalid PulseAudio card index")
    return hardware, pulse, index


def _missing_profile_inventory(card: Mapping[str, object], pulse: Mapping[str, object]) -> bool:
    parameters = object_mapping(object_mapping(card.get("info")).get("params", {}))
    profiles = parameters.get("Profile")
    if isinstance(profiles, list) and len(cast(list[object], profiles)) > 1:
        raise HardwareUnavailable("Native USB card profile is ambiguous",
                                  code="hardware_profile_unavailable")
    return (parameters.get("Profile") == [] or parameters.get("EnumProfile") == []
            or pulse.get("profiles") == {} or pulse.get("active_profile") is None)


def _require_idle_audio(graph: list[dict[str, object]]) -> None:
    for item in graph:
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        properties = graph_properties(item)
        details = object_mapping(item.get("info"))
        if (properties.get("media.class") in ("Audio/Sink", "Stream/Output/Audio")
                and details.get("state") not in ("idle", "suspended")):
            raise HardwareUnavailable("Refusing audio recovery while audio may be running")


def _boot_id() -> str:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if len(boot) != 36 or any(character not in "0123456789abcdef-" for character in boot):
        raise RuntimeError("Audio recovery boot identity is invalid")
    return boot


def _clear_session_observation(selector: HardwareSelector, state: TextIO) -> None:
    records = _read_recovery_state(state)
    if "session" in records:
        record = object_mapping(records["session"])
        if record.get("selector") == _selector_key(selector) and record.get("since") is not None:
            record["since"] = None
            _write_recovery_state(state, records | {"session": record})


def _recover_session_inventory(
    selector: HardwareSelector, expected: Mapping[str, object], index: int, state: TextIO,
) -> None:
    graph, pulse = _same_card(selector, expected, index)
    card = resolve_hardware_card(graph, selector)
    if not _missing_profile_inventory(card, pulse):
        return
    _require_idle_audio(graph)
    cookie = _core_cookie(graph)
    serial = graph_properties(card).get("object.serial")
    if serial is None:
        raise HardwareUnavailable("Audio recovery generation is unavailable")
    boot = _boot_id()
    generation: list[object] = [cookie, card["id"], serial, index]
    records = _read_recovery_state(state)
    record: dict[str, object] = object_mapping(records["session"]) if "session" in records else {}
    if record.get("boot") != boot:
        record = {"boot": boot, "attempted": False}
    if record.get("attempted"):
        raise HardwareUnavailable("WirePlumber recovery budget exhausted for this boot")
    now = time.monotonic()
    since = record.get("since")
    if record.get("generation") != generation or since is None:
        since = now
        record.update(generation=generation, since=since, selector=_selector_key(selector))
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        raise ValueError("Invalid session recovery observation time")
    if now - since < 5:
        _write_recovery_state(state, records | {"session": record})
        raise HardwareUnavailable("Waiting for missing audio profile inventory",
                                  code="hardware_profile_unavailable")
    # The boot-scoped budget survives the server/device replacement this requests.
    record["attempted"] = True
    _write_recovery_state(state, records | {"session": record})
    logging.warning("Restarting WirePlumber once for persistently missing USB profile inventory")
    try:
        subprocess.run(
            ["systemctl", "--user", "--no-block", "restart", "wireplumber.service"],
            check=True, capture_output=True, text=True, timeout=2,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"WirePlumber recovery failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("WirePlumber recovery request timed out") from error
    raise HardwareUnavailable("WirePlumber recovery requested; awaiting fresh audio readiness",
                              code="hardware_profile_unavailable")


def _clear_profile_observation(selector: HardwareSelector, state: TextIO) -> None:
    records = _read_recovery_state(state)
    key = _selector_key(selector)
    if key in records:
        record = object_mapping(records[key])
        if record.get("since") is not None:
            record["since"] = None
            records[key] = record
            _write_recovery_state(state, records)


def _same_card(
    selector: HardwareSelector, expected: Mapping[str, object], index: int,
    *, cookie: int | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    graph = pipewire_graph()
    if cookie is not None and _core_cookie(graph) != cookie:
        raise HardwareUnavailable("PipeWire restarted during audio recovery")
    fresh = resolve_hardware_card(graph, selector)
    if (fresh["id"] != expected["id"]
            or graph_properties(fresh).get("object.serial")
            != graph_properties(expected).get("object.serial")):
        raise HardwareUnavailable("Selected USB card changed during recovery")
    cards: JSONValue = json.loads(_pactl("--format=json", "list", "cards"))
    if not isinstance(cards, list):
        raise ValueError("Invalid PulseAudio inventory")
    _, pulse, pulse_index = _selected_card(cards, graph, selector)
    if pulse_index != index:
        raise HardwareUnavailable("PulseAudio card changed during recovery")
    return graph, pulse


def _core_cookie(graph: list[dict[str, object]]) -> int:
    cores = [item for item in graph if item.get("type") == "PipeWire:Interface:Core"]
    if len(cores) != 1:
        raise HardwareUnavailable("Audio recovery core generation is unavailable")
    cookie = object_mapping(cores[0].get("info")).get("cookie")
    if not isinstance(cookie, int) or isinstance(cookie, bool):
        raise HardwareUnavailable("Audio recovery core generation is unavailable")
    return cookie


def _select_profile(
    selector: HardwareSelector, mappings: dict[str, tuple[str, str]],
    expected: Mapping[str, object], index: int, profile: str, state: TextIO,
) -> None:
    graph, pulse = _same_card(selector, expected, index)
    card = resolve_hardware_card(graph, selector)
    native = _native_profile(card)
    if pulse.get("active_profile") == native == f"output:{profile}":
        _clear_profile_observation(selector, state)
        return
    stale = (pulse.get("active_profile") == native == "off"
             and any(
                 item.get("type") == "PipeWire:Interface:Node"
                 and graph_properties(item).get("device.id") == card["id"]
                 and graph_properties(item).get("device.profile.name") == profile
                 and graph_properties(item).get("media.class") == "Audio/Sink"
                 for item in graph
             ))
    if not stale:
        _clear_profile_observation(selector, state)
        _pactl("set-card-profile", str(index), f"output:{profile}")
        return
    validate_downstream(graph, selector, mappings)
    _require_idle_audio(graph)
    serial = graph_properties(card).get("object.serial")
    if serial is None:
        raise HardwareUnavailable("Audio recovery generation is unavailable")
    cookie = _core_cookie(graph)
    generation: list[object] = [cookie, card["id"], serial, index, profile]
    key = _selector_key(selector)
    records = _read_recovery_state(state)
    record: dict[str, object] = object_mapping(records[key]) if key in records else {}
    now = time.monotonic()
    if record.get("generation") != generation:
        record = {"generation": generation, "since": now, "attempted": False}
    if record.get("attempted"):
        raise HardwareUnavailable("Audio profile reset budget exhausted for this device generation")
    since = record.get("since")
    if since is None:
        since = now
        record["since"] = since
    if not isinstance(since, (float, int)) or isinstance(since, bool):
        raise ValueError("Invalid audio recovery observation time")
    records[key] = record
    if now - since < 5:
        _write_recovery_state(state, records)
        _pactl("set-card-profile", str(index), f"output:{profile}")
        return
    # Persist the budget before either command, including across guard crashes.
    record["attempted"] = True
    _write_recovery_state(state, records)
    logging.warning("Resetting persistently inconsistent USB audio profile for %s", key)
    _pactl("set-card-profile", str(index), "off")
    _same_card(selector, card, index, cookie=cookie)
    _pactl("set-card-profile", str(index), f"output:{profile}")
    logging.warning("USB audio profile reset requested; awaiting fresh readiness verification")


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
