from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Protocol, TypeAlias, Union, cast

import mpd  # pyright: ignore[reportMissingTypeStubs]

from lib.setuplock import setup_lock
from lib.atomicjson import write_json
from lib.audioready import wait_for_audio
from lib.streamgain import set_sink_gain, validate_gain_db
from lib.streamroute import StreamRoute, inspect_route, recover_route


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


class BackendError(RuntimeError):
    """An MPD, audio, locking or persistence operation failed."""


class MPDConnection(Protocol):
    timeout: float

    def connect(self, host: str, port: int) -> None: ...
    def disconnect(self) -> None: ...
    def status(self) -> dict[str, str]: ...
    def currentsong(self) -> dict[str, str | list[str]]: ...
    def playlistinfo(self) -> list[dict[str, str]]: ...
    def addid(self, uri: str) -> str: ...
    def playid(self, songid: str) -> None: ...
    def stop(self) -> None: ...
    def pause(self, value: int) -> None: ...
    def clearerror(self) -> None: ...
    def setvol(self, volume: int) -> None: ...


@dataclass(frozen=True)
class StreamSettings:
    name: str
    url: str
    port: int
    sink: str
    gain_db: float


@dataclass(frozen=True)
class PlaybackStatus:
    name: str
    state: str
    gain_db: float
    sink: str


class MPDBackend:
    """Control configured local MPD instances without running a server."""

    def __init__(self, config_path: str | Path, state_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.state_path = Path(state_path) if state_path is not None else (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "mpdrussoundpi/playback.json"
        )
        self.list_streams()

    def _load(self) -> tuple[dict[str, JSONValue], list[StreamSettings]]:
        try:
            raw = self.config_path.read_bytes()
        except (OSError, ValueError) as error:
            raise BackendError(f"Cannot read configuration: {error}") from error
        return self._parse_configuration(raw)

    def configuration_snapshot(self) -> tuple[dict[str, JSONValue], tuple[StreamSettings, ...], str]:
        """Validate settings and revision from the same configuration bytes."""
        try:
            raw = self.config_path.read_bytes()
        except OSError as error:
            raise BackendError("Cannot read observation configuration") from error
        config, streams = self._parse_configuration(raw)
        return config, tuple(streams), hashlib.sha256(raw).hexdigest()

    def _parse_configuration(self, raw: bytes) -> tuple[dict[str, JSONValue], list[StreamSettings]]:
        try:
            config: JSONValue = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise BackendError("Cannot read configuration: invalid JSON") from error
        if not isinstance(config, dict):
            raise ValueError("Configuration must be an object")
        entries = config.get("streams")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Configuration requires a nonempty streams list")
        mappers = config.get("outputmappers")
        if not isinstance(mappers, dict):
            raise ValueError("Configuration requires outputmappers")
        devices = mappers.get("audio_devices")
        if not isinstance(devices, list):
            raise ValueError("Configuration requires audio_devices")
        sinks: set[str] = set()
        for device in devices:
            if not isinstance(device, dict) or not isinstance(device.get("sinks"), list):
                raise ValueError("Invalid audio device")
            device_sinks = device["sinks"]
            if isinstance(device_sinks, list):
                for sink in device_sinks:
                    if not isinstance(sink, dict) or not isinstance(sink.get("name"), str):
                        raise ValueError("Invalid sink name")
                    name = sink["name"]
                    if isinstance(name, str):
                        sinks.add(name)
        settings: list[StreamSettings] = []
        names: set[str] = set()
        ports: set[int] = set()
        used_sinks: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid stream entry")
            name, url, port = entry.get("name"), entry.get("stream"), entry.get("port")
            output = entry.get("audio_output")
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("Stream names must be nonempty and unique")
            if not isinstance(url, str) or not url:
                raise ValueError(f"{name}: missing stream URL")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or port in ports:
                raise ValueError(f"{name}: invalid or duplicate MPD port")
            if not isinstance(output, dict) or output.get("type") != "pulse":
                raise ValueError(f"{name}: only configured PulseAudio outputs are supported")
            sink_name = output.get("sink")
            if not isinstance(sink_name, str) or sink_name not in sinks or sink_name in used_sinks:
                raise ValueError(f"{name}: sink must exist and be exclusive to this stream")
            gain = validate_gain_db(entry.get("gain_db", 0))
            settings.append(StreamSettings(name, url, port, sink_name, gain))
            names.add(name)
            ports.add(port)
            used_sinks.add(sink_name)
        return config, settings

    def list_streams(self) -> tuple[StreamSettings, ...]:
        return tuple(self._load()[1])

    def get_gain(self, name: str) -> float:
        """Return persisted gain, not the current mixer reading."""
        return self._select(name)[0].gain_db

    def _select(self, name: str | None) -> list[StreamSettings]:
        settings = self._load()[1]
        if name is None:
            return settings
        selected = [item for item in settings if item.name == name]
        if not selected:
            raise ValueError(f"Unknown stream name: {name!r}")
        return selected

    @contextmanager
    def _mutation(self) -> Generator[None, None, None]:
        try:
            with setup_lock("mpd_control.lock"):
                yield
        except SystemExit as error:
            raise BackendError("MPD control lock is busy") from error
        except (OSError, RuntimeError) as error:
            raise BackendError(str(error)) from error

    @contextmanager
    def _connection(
        self, stream: StreamSettings, *, timeout: float = 5, redact_errors: bool = False,
    ) -> Generator[MPDConnection, None, None]:
        # python-mpd2 exposes protocol commands dynamically through __getattr__.
        client = cast(MPDConnection, mpd.MPDClient())
        client.timeout = timeout
        try:
            client.connect("localhost", stream.port)
            yield client
        except (mpd.MPDError, OSError) as error:
            if redact_errors:
                raise BackendError("MPD read failed") from error
            raise BackendError(f"{stream.name}: MPD operation failed: {error}") from error
        finally:
            failing = sys.exc_info()[0] is not None
            try:
                client.disconnect()
            except (mpd.MPDError, OSError) as error:
                if failing:
                    if redact_errors:
                        logging.error("MPD observation disconnect failed during error cleanup")
                    else:
                        logging.error("%s: disconnect failed during error cleanup: %s", stream.name, error)
                else:
                    if redact_errors:
                        raise BackendError("MPD observation disconnect failed") from error
                    raise BackendError(f"{stream.name}: disconnect failed: {error}") from error

    @contextmanager
    def observation_connection(self, stream: StreamSettings) -> Generator[MPDConnection, None, None]:
        """An independent bounded connection with sanitized operational errors."""
        with self._connection(stream, timeout=3, redact_errors=True) as client:
            yield client

    def status(self, name: str | None = None) -> tuple[PlaybackStatus, ...]:
        statuses: list[PlaybackStatus] = []
        for stream in self._select(name):
            with self._connection(stream) as client:
                state = client.status().get("state")
            if state not in ("play", "pause", "stop"):
                raise BackendError(f"{stream.name}: invalid MPD playback state: {state!r}")
            statuses.append(PlaybackStatus(stream.name, state, stream.gain_db, f"sink.{stream.sink}"))
        return tuple(statuses)

    def start(self, name: str | None = None) -> None:
        with self._mutation():
            streams = self._select(name)
            self._record_intention(streams, "play")
            wait_for_audio(self.config_path)
            for stream in streams:
                self._start_stream(stream)

    def _start_stream(self, stream: StreamSettings) -> None:
        with self._connection(stream) as client:
            playlist = client.playlistinfo()
            songid = next((song.get("id") for song in playlist if song.get("file") == stream.url), None)
            if songid is None:
                songid = client.addid(stream.url)
            if not songid:
                raise BackendError(f"{stream.name}: MPD returned no song ID")
            state = client.status()
            restarting = state.get("state") != "play" or state.get("songid") != songid or "error" in state
            try:
                if restarting:
                    # Software mixer gates new playback before the Pulse sink is active.
                    client.setvol(0)
                    client.clearerror()
                    client.playid(songid)
                recover_route(stream.sink)
                set_sink_gain(stream.sink, stream.gain_db)
                client.setvol(100)
                final = client.status()
                if final.get("state") != "play" or "error" in final:
                    raise BackendError(f"{stream.name}: playback did not start: {final}")
            except (mpd.MPDError, OSError, RuntimeError) as error:
                try:
                    client.stop()
                except (mpd.MPDError, OSError) as stop_error:
                    raise BackendError(
                        f"{stream.name}: route/gain failed ({error}); stop also failed ({stop_error})"
                    ) from stop_error
                raise

    def stop(self, name: str | None = None) -> None:
        self._playback_action(name, pause=False)

    def pause(self, name: str | None = None) -> None:
        self._playback_action(name, pause=True)

    def _playback_action(self, name: str | None, *, pause: bool) -> None:
        with self._mutation():
            streams = self._select(name)
            self._record_intention(streams, "pause" if pause else "stop")
            for stream in streams:
                with self._connection(stream) as client:
                    if pause:
                        client.pause(1)
                    else:
                        client.stop()
                    expected = "pause" if pause else "stop"
                    if client.status().get("state") != expected:
                        raise BackendError(f"{stream.name}: playback did not enter {expected}")

    def check_route(self, name: str) -> StreamRoute:
        stream = self._select(name)[0]
        try:
            return inspect_route(stream.sink)
        except (OSError, RuntimeError) as error:
            raise BackendError(f"{name}: route inspection failed: {error}") from error

    def recover_route(self, name: str | None = None) -> tuple[StreamRoute, ...]:
        with self._mutation():
            return tuple(recover_route(stream.sink) for stream in self._select(name))

    def set_gain(self, name: str, gain_db: float) -> None:
        gain = validate_gain_db(gain_db)
        with self._mutation():
            config, settings = self._load()
            stream = next((item for item in settings if item.name == name), None)
            if stream is None:
                raise ValueError(f"Unknown stream name: {name!r}")
            with self._connection(stream) as client:
                state = client.status().get("state")
                if state not in ("play", "pause", "stop"):
                    raise BackendError(f"{stream.name}: invalid MPD state {state}")
            if state not in ("play", "pause", "stop"):
                raise BackendError(f"{name}: invalid playback state")
            active = state == "play"
            if active:
                recover_route(stream.sink)
                set_sink_gain(stream.sink, gain)
            entries = config["streams"]
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name") == name:
                        entry["gain_db"] = gain
            try:
                self._save(config)
            except (OSError, ValueError) as error:
                if active:
                    try:
                        set_sink_gain(stream.sink, stream.gain_db)
                    except (OSError, RuntimeError) as rollback_error:
                        raise BackendError(
                            f"{name}: saving gain failed ({error}); gain rollback also failed ({rollback_error})"
                        ) from rollback_error
                raise BackendError(f"{name}: cannot persist gain: {error}") from error

    def _save(self, config: dict[str, JSONValue]) -> None:
        write_json(self.config_path, config)

    def _intentions(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        value: JSONValue = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid persisted playback intentions")
        intentions: dict[str, str] = {}
        for name, state in value.items():
            if state not in ("play", "pause", "stop"):
                raise ValueError(f"Invalid playback intention for {name}")
            intentions[name] = state
        return intentions

    def _record_intention(self, streams: list[StreamSettings], state: str) -> None:
        intentions = self._intentions()
        intentions.update({stream.name: state for stream in streams})
        write_json(self.state_path, intentions)

    def initialize_intentions(self) -> None:
        """Default only previously unknown streams to playing; retain user choices."""
        with self._mutation():
            intentions = self._intentions()
            for stream in self.list_streams():
                intentions.setdefault(stream.name, "play")
            write_json(self.state_path, intentions)

    def reconcile(self, name: str) -> None:
        """Restore a saved intention without overwriting it with observed state."""
        with self._mutation():
            stream = self._select(name)[0]
            intended = self._intentions().get(name)
            if intended is None:
                raise BackendError(f"No saved intention for {name}; initialize intentions first")
            if intended == "play":
                wait_for_audio(self.config_path, timeout=5)
                self._start_stream(stream)
            else:
                with self._connection(stream) as client:
                    state = client.status().get("state")
                    if intended == "stop" and state != "stop":
                        client.stop()
                    elif intended == "pause" and state == "play":
                        client.pause(1)
                    # A stopped/empty instance stays silent for a paused intention.
                    final = client.status().get("state")
                    if final not in (("stop",) if intended == "stop" else ("stop", "pause")):
                        raise BackendError(f"{stream.name}: failed to restore {intended} intention")

    def apply_gain(self, name: str | None = None) -> None:
        with self._mutation():
            for stream in self._select(name):
                recover_route(stream.sink)
                set_sink_gain(stream.sink, stream.gain_db)
