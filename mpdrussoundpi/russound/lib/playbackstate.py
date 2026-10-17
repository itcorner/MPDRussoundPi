"""Agent-owned runtime state: favorites plus per-output station, gain and intention."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import cast

from lib.atomicjson import write_json
from lib.outputconfig import Configuration
from lib.sourceurl import validate_source_url
from lib.stations import MAX_STATIONS, Station, parse_station_list
from lib.streamgain import validate_gain_db


INTENTIONS = ("play", "pause", "stop")
OUTPUT_STATE_FIELDS = {"station", "resolved_url", "managed_song_id", "gain_db", "intention"}


class FavoritesFull(ValueError):
    pass


@dataclass(frozen=True)
class OutputState:
    station: Station | None
    gain_db: float
    intention: str
    resolved_url: str | None = None
    managed_song_id: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "station": self.station.to_json() if self.station else None,
            "resolved_url": self.resolved_url, "managed_song_id": self.managed_song_id,
            "gain_db": self.gain_db, "intention": self.intention,
        }


@dataclass(frozen=True)
class PlaybackState:
    favorites: tuple[Station, ...]
    outputs: dict[str, OutputState]

    def to_json(self) -> dict[str, object]:
        return {
            "favorites": [station.to_json() for station in self.favorites],
            "outputs": {key: value.to_json() for key, value in self.outputs.items()},
        }

    def with_output(self, identity: str, **changes: object) -> "PlaybackState":
        outputs = dict(self.outputs)
        outputs[identity] = replace(outputs[identity], **changes)  # type: ignore[arg-type]
        return PlaybackState(self.favorites, outputs)

    def add_favorite(self, station: Station) -> "PlaybackState":
        if any(item.key == station.key for item in self.favorites):
            return self
        if len(self.favorites) >= MAX_STATIONS:
            raise FavoritesFull(f"At most {MAX_STATIONS} favorites are allowed")
        return PlaybackState(self.favorites + (station,), self.outputs)

    def favorite(self, key: str) -> Station:
        for station in self.favorites:
            if station.key == key:
                return station
        raise KeyError(key)

    def remove_favorite(self, key: str) -> "PlaybackState":
        self.favorite(key)
        return PlaybackState(tuple(item for item in self.favorites if item.key != key), self.outputs)

    def move_favorite(self, key: str, index: int) -> "PlaybackState":
        station = self.favorite(key)
        if isinstance(index, bool) or not 0 <= index < len(self.favorites):
            raise ValueError("Favorite index is out of range")
        remaining = [item for item in self.favorites if item.key != key]
        remaining.insert(index, station)
        return PlaybackState(tuple(remaining), self.outputs)


def default_output_state(configuration: Configuration, identity: str) -> OutputState:
    output = configuration.output(identity)
    station = output.default_station
    return OutputState(
        station, output.default_gain_db, "play" if station else "stop",
        station.url if station and station.type == "url" else None,
    )


def _output_state(identity: str, value: object) -> OutputState:
    if not isinstance(value, dict) or set(cast(dict[str, object], value)) != OUTPUT_STATE_FIELDS:
        raise ValueError(f"{identity}: invalid output state")
    entry = cast(dict[str, object], value)
    station = None if entry["station"] is None else Station.parse(entry["station"])
    intention = entry["intention"]
    if intention not in INTENTIONS:
        raise ValueError(f"{identity}: invalid playback intention")
    if station is None and intention != "stop":
        raise ValueError(f"{identity}: an output without a station must be stopped")
    resolved = entry["resolved_url"]
    if resolved is not None:
        resolved = validate_source_url(resolved)
    song_id = entry["managed_song_id"]
    if song_id is not None and (
        not isinstance(song_id, str) or not song_id.isascii() or not song_id.isdigit() or len(song_id) > 10
    ):
        raise ValueError(f"{identity}: invalid managed song ID")
    return OutputState(station, validate_gain_db(entry["gain_db"]), cast(str, intention), resolved, song_id)


def parse_state(raw: bytes | None, configuration: Configuration) -> PlaybackState:
    """Missing outputs/favorites take config defaults; unknown output IDs are dropped."""
    value: object = {} if raw is None else json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Playback state must be an object")
    data = cast(dict[str, object], value)
    if set(data) - {"favorites", "outputs"}:
        raise ValueError("Playback state contains unsupported fields")
    favorites = (
        configuration.stations if "favorites" not in data
        else parse_station_list(data["favorites"], allow_url=True)
    )
    stored = data.get("outputs", {})
    if not isinstance(stored, dict):
        raise ValueError("Playback state outputs must be an object")
    entries = cast(dict[str, object], stored)
    outputs = {
        output.id: _output_state(output.id, entries[output.id]) if output.id in entries
        else default_output_state(configuration, output.id)
        for output in configuration.outputs
    }
    return PlaybackState(favorites, outputs)


def read_state(path: Path, configuration: Configuration) -> tuple[PlaybackState, bytes | None]:
    try:
        raw: bytes | None = path.read_bytes()
    except FileNotFoundError:
        raw = None
    return parse_state(raw, configuration), raw


def write_state(path: Path, state: PlaybackState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, state.to_json())
