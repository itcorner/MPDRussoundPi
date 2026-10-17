from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Protocol, TypeAlias, TypeGuard, Union, cast

import mpd  # pyright: ignore[reportMissingTypeStubs]

from lib.setuplock import setup_lock
from lib.atomicjson import write_json
from lib.audioready import wait_for_audio
from lib.audioidentity import validate_hardware_devices
from lib.mpdinstance import MPD_HOST
from lib.playbackidentity import identities, output_label
from lib.sourceurl import validate_source_url
from lib.tunein import TuneInCatalog, TuneInError
from lib.streamgain import set_sink_gain, validate_gain_db
from lib.streamroute import StreamRoute, inspect_route, recover_route


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]

# MPD opens its Pulse client only after a newly started internet stream has buffered.
STARTUP_ROUTE_TIMEOUT = 12.0


class BackendError(RuntimeError):
    """An MPD, audio, locking or persistence operation failed."""


class RevisionConflict(BackendError):
    """The configuration changed since the caller read it."""


class BackendBusy(BackendError):
    """Another backend mutation owns the shared lock."""


class MPDOperationError(BackendError):
    """An MPD protocol/readback operation failed."""


class StationUnavailableError(BackendError):
    """The selected station could not be resolved to a supported stream."""


class AudioOperationError(BackendError):
    """Audio readiness, routing or gain application failed."""


class GainPersistenceError(BackendError):
    """Saving gain failed, possibly also failing to restore the live gain."""

    def __init__(self, message: str, *, rollback_failed: bool = False) -> None:
        super().__init__(message)
        self.rollback_failed = rollback_failed


class MPDConnection(Protocol):
    timeout: float

    def connect(self, host: str, port: int) -> None: ...
    def disconnect(self) -> None: ...
    def status(self) -> dict[str, str]: ...
    def currentsong(self) -> dict[str, str | list[str]]: ...
    def playlistinfo(self) -> list[dict[str, str]]: ...
    def addid(self, uri: str) -> str: ...
    def deleteid(self, songid: str) -> None: ...
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
    queue_managed: bool = False
    managed_song_id: str | None = None
    source_provider: str | None = None
    source_station_id: str | None = None
    source_station_name: str | None = None


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
        self._tunein = TuneInCatalog()
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
        identities(entries)
        mappers = config.get("outputmappers")
        if not isinstance(mappers, dict):
            raise ValueError("Configuration requires outputmappers")
        devices = mappers.get("audio_devices")
        if not isinstance(devices, list):
            raise ValueError("Configuration requires audio_devices")
        validate_hardware_devices(devices)
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
            if url is None and entry.get("source_provider") == "tunein":
                # TuneIn entries may omit the URL; it is resolved and saved on the next start.
                url = ""
            else:
                try:
                    url = validate_source_url(url)
                except ValueError:
                    raise ValueError(f"{name}: stream must be a direct HTTP(S) URL") from None
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or port in ports:
                raise ValueError(f"{name}: invalid or duplicate MPD port")
            if not isinstance(output, dict) or output.get("type") != "pulse":
                raise ValueError(f"{name}: only configured PulseAudio outputs are supported")
            sink_name = output.get("sink")
            if not isinstance(sink_name, str) or sink_name not in sinks or sink_name in used_sinks:
                raise ValueError(f"{name}: sink must exist and be exclusive to this stream")
            gain = validate_gain_db(entry.get("gain_db", 0))
            queue_managed = entry.get("queue_managed", False)
            managed_song_id = entry.get("managed_song_id")
            if not isinstance(queue_managed, bool):
                raise ValueError(f"{name}: invalid managed queue flag")
            if managed_song_id is not None and (
                not isinstance(managed_song_id, str)
                or not managed_song_id.isascii()
                or not managed_song_id.isdigit()
                or len(managed_song_id) > 10
            ):
                raise ValueError(f"{name}: invalid managed song ID")
            if not queue_managed and managed_song_id is not None:
                raise ValueError(f"{name}: managed song ID requires a managed queue")
            source_provider_value = entry.get("source_provider")
            source_station_id_value = entry.get("source_station_id")
            source_station_name_value = entry.get("source_station_name")
            source_provider: str | None = None
            source_station_id: str | None = None
            source_station_name: str | None = None
            if source_provider_value is None:
                if source_station_id_value is not None or source_station_name_value is not None:
                    raise ValueError(f"{name}: TuneIn station metadata requires a source provider")
            elif source_provider_value == "tunein":
                try:
                    source_station_id = TuneInCatalog.validate_station_id(source_station_id_value)
                except ValueError:
                    raise ValueError(f"{name}: invalid TuneIn station identity") from None
                if (
                    not isinstance(source_station_name_value, str)
                    or not source_station_name_value
                    or len(source_station_name_value) > 120
                    or not source_station_name_value.isprintable()
                ):
                    raise ValueError(f"{name}: invalid TuneIn station name")
                source_provider = "tunein"
                source_station_name = source_station_name_value
            else:
                raise ValueError(f"{name}: unsupported source provider")
            settings.append(StreamSettings(
                name, url, port, sink_name, gain, queue_managed, managed_song_id,
                source_provider, source_station_id, source_station_name,
            ))
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
            raise BackendBusy("MPD control lock is busy") from error
        except BackendError:
            raise
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
            client.connect(MPD_HOST, stream.port)
            yield client
        except (mpd.MPDError, OSError) as error:
            if redact_errors:
                raise MPDOperationError("MPD read failed") from error
            raise MPDOperationError(f"{stream.name}: MPD operation failed: {error}") from error
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
            self._start_selected(streams)

    def _start_selected(self, streams: list[StreamSettings], *, resolved: bool = False) -> None:
        try:
            wait_for_audio(self.config_path)
        except (OSError, RuntimeError) as error:
            raise AudioOperationError(str(error)) from error
        for stream in streams:
            self._start_stream(stream, resolve=not resolved)

    @staticmethod
    def _prepare_route_and_gain(stream: StreamSettings, gain: float, *, starting: bool = False) -> None:
        try:
            if starting:
                recover_route(stream.sink, timeout=STARTUP_ROUTE_TIMEOUT)
            else:
                recover_route(stream.sink)
            set_sink_gain(stream.sink, gain)
        except (OSError, RuntimeError) as error:
            raise AudioOperationError(str(error)) from error

    def _playing_source(self, stream: StreamSettings) -> bool:
        with self._connection(stream, redact_errors=True) as client:
            state = client.status()
            if state.get("state") != "play" or "error" in state:
                return False
            song_id = state.get("songid")
            return any(
                song.get("id") == song_id and song.get("file") == stream.url
                for song in client.playlistinfo()
            )

    def _start_stream(self, stream: StreamSettings, *, resolve: bool = True) -> None:
        # Re-resolve only when the saved URL is not already playing; resolution is a network call under the lock.
        if stream.source_provider == "tunein" and resolve and not self._playing_source(stream):
            if stream.source_station_id is None:
                raise BackendError("Selected TuneIn station has no saved identity")
            try:
                resolved_url = self._tunein.resolve(stream.source_station_id)
            except TuneInError:
                raise MPDOperationError("TuneIn station could not be resolved") from None
            if resolved_url != stream.url:
                stream = self._persist_tunein_resolution(stream, resolved_url)
        with self._connection(stream, redact_errors=stream.queue_managed) as client:
            playlist = client.playlistinfo()
            if stream.queue_managed:
                self._start_managed_stream(stream, client, playlist)
                return
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
                self._prepare_route_and_gain(stream, stream.gain_db, starting=restarting)
                client.setvol(100)
                final = client.status()
                if final.get("state") != "play" or "error" in final:
                    raise MPDOperationError(f"{stream.name}: playback did not start: {final}")
            except (mpd.MPDError, OSError, RuntimeError) as error:
                try:
                    client.stop()
                except (mpd.MPDError, OSError) as stop_error:
                    raise BackendError(
                        f"{stream.name}: route/gain failed ({error}); stop also failed ({stop_error})"
                    ) from stop_error
                raise

    @staticmethod
    def _valid_song_id(value: object) -> TypeGuard[str]:
        return (
            isinstance(value, str)
            and value.isascii()
            and value.isdigit()
            and len(value) <= 10
        )

    @staticmethod
    def _queue_entries(playlist: object) -> list[tuple[str, str]]:
        if not isinstance(playlist, list):
            raise MPDOperationError("Managed source queue is invalid")
        raw_entries = cast(list[object], playlist)
        if len(raw_entries) > 64:
            raise MPDOperationError("Managed source queue is invalid or exceeds its 64-entry safety bound")
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for song in raw_entries:
            if not isinstance(song, dict):
                raise MPDOperationError("Managed source queue contains an invalid entry")
            song_entry = cast(dict[str, object], song)
            song_id, uri = song_entry.get("id"), song_entry.get("file")
            if (
                not MPDBackend._valid_song_id(song_id)
                or song_id in seen
                or not isinstance(uri, str)
            ):
                raise MPDOperationError("Managed source queue contains an invalid entry")
            entries.append((song_id, uri))
            seen.add(song_id)
        return entries

    def _persist_tunein_resolution(self, stream: StreamSettings, url: str) -> StreamSettings:
        config, settings = self._load()
        current = next((item for item in settings if item.name == stream.name), None)
        if (
            current is None
            or current.source_provider != "tunein"
            or current.source_station_id != stream.source_station_id
            or current.url != stream.url
        ):
            raise RevisionConflict("TuneIn station changed while refreshing its stream")
        entries = config["streams"]
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == stream.name:
                    entry["stream"] = url
        self._save(config)
        return replace(stream, url=url)

    def _start_managed_stream(
        self, stream: StreamSettings, client: MPDConnection, playlist: object,
    ) -> None:
        entries = self._queue_entries(playlist)
        managed_id = stream.managed_song_id
        target_id: str | None = None
        if managed_id is not None and any(song_id == managed_id for song_id, _ in entries):
            if len(entries) != 1:
                raise MPDOperationError("Managed source queue contains unmanaged entries; refusing cleanup")
            if entries[0][1] == stream.url:
                target_id = managed_id
        elif entries:
            if len(entries) == 1 and entries[0][1] == stream.url:
                target_id = entries[0][0]
            else:
                raise MPDOperationError("Managed source queue contains unmanaged entries; refusing cleanup")

        state = client.status()
        if state.get("state") not in ("play", "pause", "stop"):
            raise MPDOperationError("MPD returned an invalid playback state")
        try:
            if target_id is None:
                client.setvol(0)
                client.clearerror()
                if state.get("state") != "stop":
                    client.stop()
                if managed_id is not None and any(song_id == managed_id for song_id, _ in entries):
                    client.deleteid(managed_id)
                    remaining = self._queue_entries(client.playlistinfo())
                    if remaining:
                        raise MPDOperationError("Managed source queue cleanup left unexpected entries")
                target_id = client.addid(stream.url)
                if not self._valid_song_id(target_id):
                    raise MPDOperationError("MPD returned an invalid song ID")
                self._persist_managed_song_id(stream, target_id)
            else:
                self._persist_managed_song_id(stream, target_id)

            restarting = (
                state.get("state") != "play"
                or state.get("songid") != target_id
                or "error" in state
            )
            if restarting:
                client.setvol(0)
                client.clearerror()
                client.playid(target_id)
            self._prepare_route_and_gain(stream, stream.gain_db, starting=restarting)
            client.setvol(100)
            final = client.status()
            if final.get("state") != "play" or "error" in final:
                raise MPDOperationError("Playback did not start")
        except (mpd.MPDError, OSError, RuntimeError):
            try:
                client.stop()
            except (mpd.MPDError, OSError):
                logging.error("Managed playback cleanup stop failed")
            raise

    def _persist_managed_song_id(self, stream: StreamSettings, song_id: str) -> None:
        if stream.managed_song_id == song_id:
            return
        config, settings = self._load()
        current = next((item for item in settings if item.name == stream.name), None)
        if current is None or current.url != stream.url or not current.queue_managed:
            raise RevisionConflict("Playback source changed while saving managed queue state")
        entries = config["streams"]
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == stream.name:
                    entry["managed_song_id"] = song_id
        self._save(config)

    def _set_tunein_station(
        self, config: dict[str, JSONValue], stream: StreamSettings, station_id: str,
        station_name: str, progress: Callable[[str], None],
    ) -> None:
        try:
            source_url = self._tunein.resolve(station_id)
        except TuneInError as exc:
            raise StationUnavailableError(str(exc)) from None
        with self._connection(stream, redact_errors=True) as client:
            queue = self._queue_entries(client.playlistinfo())
        managed_id = stream.managed_song_id
        if managed_id is not None and any(song_id == managed_id for song_id, _ in queue):
            if len(queue) != 1:
                raise MPDOperationError("Managed source queue contains unmanaged entries; refusing cleanup")
        elif queue:
            if len(queue) == 1 and queue[0][1] == stream.url:
                managed_id = queue[0][0]
            else:
                raise MPDOperationError("MPD queue contains unmanaged entries; source was not changed")
        entry_list = config["streams"]
        if isinstance(entry_list, list):
            for entry in entry_list:
                if isinstance(entry, dict) and entry.get("name") == stream.name:
                    entry["stream"] = source_url
                    entry["queue_managed"] = True
                    entry["source_provider"] = "tunein"
                    entry["source_station_id"] = station_id
                    entry["source_station_name"] = station_name
                    if managed_id is None:
                        entry.pop("managed_song_id", None)
                    else:
                        entry["managed_song_id"] = managed_id
        progress("source_saving")
        try:
            self._save(config)
        except (OSError, ValueError) as error:
            raise BackendError("Cannot persist selected source") from error
        progress("source_persisted")
        progress("intention_saving")
        self._record_intention([stream], "play")
        progress("intention_persisted")
        selected = StreamSettings(
            stream.name, source_url, stream.port, stream.sink, stream.gain_db, True, managed_id,
            "tunein", station_id, station_name,
        )
        self._start_selected([selected], resolved=True)

    def stop(self, name: str | None = None) -> None:
        self._playback_action(name, pause=False)

    def pause(self, name: str | None = None) -> None:
        self._playback_action(name, pause=True)

    def _playback_action(self, name: str | None, *, pause: bool) -> None:
        with self._mutation():
            streams = self._select(name)
            self._record_intention(streams, "pause" if pause else "stop")
            self._stop_selected(streams, pause=pause)

    def _stop_selected(self, streams: list[StreamSettings], *, pause: bool) -> None:
        for stream in streams:
            with self._connection(stream) as client:
                if pause:
                    client.pause(1)
                else:
                    client.stop()
                expected = "pause" if pause else "stop"
                if client.status().get("state") != expected:
                    raise MPDOperationError(f"{stream.name}: playback did not enter {expected}")

    def check_route(self, name: str) -> StreamRoute:
        stream = self._select(name)[0]
        try:
            return inspect_route(stream.sink)
        except (OSError, RuntimeError) as error:
            raise BackendError(f"{name}: route inspection failed: {error}") from error

    def recover_route(self, name: str | None = None) -> tuple[StreamRoute, ...]:
        with self._mutation():
            return tuple(recover_route(stream.sink) for stream in self._select(name))

    def set_gain(self, name: str, gain_db: float, *, expected_revision: str | None = None) -> None:
        gain = validate_gain_db(gain_db)
        with self._mutation():
            config, settings, revision = self.configuration_snapshot()
            self._check_revision(expected_revision, revision)
            stream = next((item for item in settings if item.name == name), None)
            if stream is None:
                raise ValueError(f"Unknown stream name: {name!r}")
            self._set_gain(config, stream, gain)

    def _set_gain(self, config: dict[str, JSONValue], stream: StreamSettings, gain: float) -> bool:
        with self._connection(stream) as client:
            state = client.status().get("state")
            if state not in ("play", "pause", "stop"):
                raise BackendError(f"{stream.name}: invalid MPD state {state}")
        active = state == "play"
        if active:
            self._prepare_route_and_gain(stream, gain)
        entries = config["streams"]
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == stream.name:
                    entry["gain_db"] = gain
        try:
            self._save(config)
        except (OSError, ValueError) as error:
            if active:
                try:
                    set_sink_gain(stream.sink, stream.gain_db)
                except (OSError, RuntimeError) as rollback_error:
                    raise GainPersistenceError(
                        f"{stream.name}: saving gain failed ({error}); gain rollback also failed ({rollback_error})",
                        rollback_failed=True,
                    ) from rollback_error
            raise GainPersistenceError(f"{stream.name}: cannot persist gain: {error}") from error
        return active

    def control_revision(self) -> str:
        """Include file identity/time so repeated legacy intention writes invalidate edits."""
        try:
            with self.state_path.open("rb") as source:
                raw = source.read()
                stat = os.fstat(source.fileno())
        except FileNotFoundError:
            return hashlib.sha256(b"no-intentions").hexdigest()
        version = f"{stat.st_ino}:{stat.st_mtime_ns}:".encode()
        return hashlib.sha256(version + raw).hexdigest()

    def operation_inventory(self) -> tuple[str, tuple[tuple[str, str, str | None], ...]]:
        config, _, revision = self.configuration_snapshot()
        known = identities(config["streams"], required=True)
        intentions = self._intentions()
        control = self.control_revision()
        return revision, tuple((identity, control, intentions.get(name)) for name, (identity, _) in known.items())

    def operation_observation(self, identity: str) -> dict[str, object]:
        """Fresh recovery evidence, not proof that an interrupted command completed."""
        with self._mutation():
            config, settings, revision = self.configuration_snapshot()
            known = identities(config["streams"], required=True)
            name = next((name for name, (key, _) in known.items() if key == identity), None)
            if name is None:
                raise KeyError(identity)
            stream = next(item for item in settings if item.name == name)
            with self.observation_connection(stream) as client:
                state = client.status().get("state")
                if state not in ("play", "pause", "stop"):
                    raise MPDOperationError("Invalid MPD state during recovery")
            return {
                "config_revision": revision, "control_revision": self.control_revision(),
                "desired_state": self._intentions().get(name), "observed_state": state,
                "configured_gain_db": stream.gain_db,
            }

    def validate_operation(self, identity: str, config_revision: str, control_revision: str) -> None:
        # Submission is read-only; execution revalidates under the mutation lock.
        self._operation_target(identity, config_revision, control_revision)

    def _operation_target(
        self, identity: str, config_revision: str, control_revision: str,
    ) -> tuple[dict[str, JSONValue], StreamSettings]:
        config, settings, actual = self.configuration_snapshot()
        self._check_revision(config_revision, actual)
        self._check_revision(control_revision, self.control_revision())
        known = identities(config["streams"], required=True)
        name = next((name for name, (key, _) in known.items() if key == identity), None)
        if name is None:
            raise KeyError(identity)
        return config, next(stream for stream in settings if stream.name == name)

    def manual_operation(
        self, identity: str, action: str, gain_db: float | None, *,
        expected_config_revision: str, expected_control_revision: str,
        progress: Callable[[str], None], station_id: str | None = None,
        station_name: str | None = None,
    ) -> None:
        if action not in ("play", "pause", "stop", "set_gain", "set_tunein_station"):
            raise ValueError("Unsupported manual action")
        gain = validate_gain_db(gain_db) if action == "set_gain" else None
        selected_station_id = TuneInCatalog.validate_station_id(station_id) if action == "set_tunein_station" else None
        selected_station_name = station_name if action == "set_tunein_station" else None
        if selected_station_id is not None and (
            not isinstance(selected_station_name, str)
            or not selected_station_name
            or len(selected_station_name) > 120
            or not selected_station_name.isprintable()
        ):
            raise ValueError("Invalid TuneIn station name")
        with self._mutation():
            config, stream = self._operation_target(
                identity, expected_config_revision, expected_control_revision,
            )
            if gain is not None:
                progress("gain_applying")
                active = self._set_gain(config, stream, gain)
                progress("gain_saved")
                progress("gain_live" if active else "gain_deferred")
            elif selected_station_id is not None and selected_station_name is not None:
                self._set_tunein_station(
                    config, stream, selected_station_id, selected_station_name, progress,
                )
            else:
                progress("intention_saving")
                self._record_intention([stream], action)
                progress("intention_persisted")
                if action == "play":
                    self._start_selected([stream])
                else:
                    self._stop_selected([stream], pause=action == "pause")

    def _save(self, config: dict[str, JSONValue]) -> None:
        write_json(self.config_path, config)

    @staticmethod
    def _check_revision(expected: str | None, actual: str) -> None:
        if expected is not None and expected != actual:
            raise RevisionConflict("Configuration revision conflict; read the current configuration and retry")

    def set_label(self, name: str, label: str, *, expected_revision: str) -> None:
        label = output_label(label)
        with self._mutation():
            config, _, revision = self.configuration_snapshot()
            self._check_revision(expected_revision, revision)
            known = identities(config["streams"], required=True)
            if name not in known:
                raise ValueError(f"Unknown stream name: {name!r}")
            entries = config["streams"]
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name") == name:
                        entry["label"] = label
            self._save(config)

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
