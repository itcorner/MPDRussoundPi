"""Parse the admin-owned config.json: audio hardware, MPD outputs, seed stations and watchdog."""

from dataclasses import dataclass, fields
import json
import math
from typing import cast

from lib.audioidentity import validate_hardware_devices
from lib.playbackidentity import output_id, output_label
from lib.stations import Station, parse_station_list
from lib.streamgain import validate_gain_db


OLD_FORMAT_MESSAGE = "old config format; see docs/documentation/mpd-headless-deployment.md"
OUTPUT_FIELDS = {"id", "label", "port", "sink", "default_station", "default_gain_db"}


@dataclass(frozen=True)
class WatchdogConfig:
    """Stall watchdog thresholds; every config.json field is optional."""
    enabled: bool = True
    probe_interval_s: float = 10.0
    stall_threshold_s: float = 120.0
    unresponsive_probes: int = 3
    restart_limit: int = 3
    restart_window_s: float = 1800.0
    restart_grace_s: float = 90.0
    escalation_window_s: float = 3600.0


@dataclass(frozen=True)
class OutputConfig:
    id: str
    label: str
    port: int
    sink: str
    default_station: Station | None = None
    default_gain_db: float = 0.0


@dataclass(frozen=True)
class Configuration:
    raw: dict[str, object]
    outputs: tuple[OutputConfig, ...]
    stations: tuple[Station, ...]
    watchdog: WatchdogConfig = WatchdogConfig()

    def output(self, identity: str) -> OutputConfig:
        for output in self.outputs:
            if output.id == identity:
                return output
        raise KeyError(identity)


def _sinks(config: dict[str, object]) -> set[str]:
    mappers = config.get("outputmappers")
    if not isinstance(mappers, dict):
        raise ValueError("Configuration requires outputmappers")
    devices = cast(dict[str, object], mappers).get("audio_devices")
    if not isinstance(devices, list):
        raise ValueError("Configuration requires audio_devices")
    validate_hardware_devices(cast(list[object], devices))
    sinks: set[str] = set()
    for device in cast(list[object], devices):
        device_sinks = cast(dict[str, object], device).get("sinks") if isinstance(device, dict) else None
        if not isinstance(device_sinks, list):
            raise ValueError("Invalid audio device")
        for sink in cast(list[object], device_sinks):
            name = cast(dict[str, object], sink).get("name") if isinstance(sink, dict) else None
            if not isinstance(name, str):
                raise ValueError("Invalid sink name")
            sinks.add(name)
    return sinks


def _watchdog(config: dict[str, object]) -> WatchdogConfig:
    entry = config.get("watchdog")
    if entry is None:
        return WatchdogConfig()
    if not isinstance(entry, dict):
        raise ValueError("watchdog must be an object")
    item = cast(dict[str, object], entry)
    known = {field.name for field in fields(WatchdogConfig)}
    unknown = set(item) - known
    if unknown:
        raise ValueError(f"unsupported watchdog fields {sorted(unknown)}")
    values: dict[str, object] = {}
    for name, value in item.items():
        if name == "enabled":
            if not isinstance(value, bool):
                raise ValueError("watchdog enabled must be a boolean")
        elif name in ("unresponsive_probes", "restart_limit"):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                raise ValueError(f"watchdog {name} must be an integer between 1 and 100")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"watchdog {name} must be a finite number of seconds")
            minimum = 0.0 if name == "restart_grace_s" else 1.0
            if not minimum <= value <= 86400:
                raise ValueError(f"watchdog {name} must be between {minimum:g} and 86400 seconds")
            value = float(value)
        values[name] = value
    return WatchdogConfig(**values)  # type: ignore[arg-type]


def parse_configuration(raw: bytes) -> Configuration:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("Configuration is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    config = cast(dict[str, object], value)
    if "streams" in config:
        raise ValueError(OLD_FORMAT_MESSAGE)
    sinks = _sinks(config)
    stations = parse_station_list(config.get("stations", []), allow_url=True)
    by_key = {station.key: station for station in stations}
    entries = config.get("outputs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Configuration requires a nonempty outputs list")
    outputs: list[OutputConfig] = []
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            raise ValueError("Invalid output entry")
        item = cast(dict[str, object], entry)
        identity = output_id(item.get("id"))
        unknown = set(item) - OUTPUT_FIELDS
        if unknown:
            raise ValueError(f"{identity}: unsupported output fields {sorted(unknown)}")
        port, sink = item.get("port"), item.get("sink")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{identity}: invalid MPD port")
        if not isinstance(sink, str) or sink not in sinks:
            raise ValueError(f"{identity}: sink must exist in outputmappers")
        default_key = item.get("default_station")
        default_station = None
        if default_key is not None:
            if not isinstance(default_key, str) or default_key not in by_key:
                raise ValueError(f"{identity}: default_station must name an entry of stations")
            default_station = by_key[default_key]
        outputs.append(OutputConfig(
            identity, output_label(item.get("label")), port, sink, default_station,
            validate_gain_db(item.get("default_gain_db", 0.0)),
        ))
    for field in ("id", "port", "sink"):
        values = [getattr(output, field) for output in outputs]
        if len(set(values)) != len(values):
            raise ValueError(f"Output {field} values must be unique")
    return Configuration(config, tuple(outputs), stations, _watchdog(config))
