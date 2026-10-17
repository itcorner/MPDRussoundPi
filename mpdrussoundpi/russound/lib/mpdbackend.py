from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import logging
import os
from pathlib import Path
import sys
from typing import Protocol, TypeAlias, TypeGuard, Union, cast

import mpd  # pyright: ignore[reportMissingTypeStubs]

from lib.setuplock import setup_lock
from lib.audioready import wait_for_audio
from lib.mpdinstance import MPD_HOST
from lib.outputconfig import Configuration, parse_configuration
from lib.playbackstate import (
    FavoritesFull, OutputState, PlaybackState, read_state, write_state,
)
from lib.stations import Station
from lib.tunein import TuneInCatalog, TuneInError
from lib.streamgain import set_sink_gain, validate_gain_db
from lib.streamroute import StreamRoute, inspect_route, recover_route


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]

# MPD opens its Pulse client only after a newly started internet stream has buffered.
STARTUP_ROUTE_TIMEOUT = 12.0
OUTPUT_ACTIONS = ("play", "pause", "stop", "set_gain", "set_tunein_station", "select_favorite")
FAVORITE_ACTIONS = ("add_favorite", "remove_favorite", "move_favorite")


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


class NoStationError(BackendError):
    """Playback was requested for an output without a selected station."""


class FavoriteNotFound(BackendError):
    """The named favorite is not in the favorites list."""


class FavoritesFullError(BackendError):
    """The favorites list already holds the maximum number of stations."""


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
class OutputSettings:
    """One configured output merged with its runtime station, gain and intention."""
    id: str
    label: str
    port: int
    sink: str
    gain_db: float
    station: Station | None = None
    url: str = ""
    managed_song_id: str | None = None
    intention: str = "stop"


@dataclass(frozen=True)
class PlaybackStatus:
    id: str
    state: str
    gain_db: float
    sink: str


class MPDBackend:
    """Control configured local MPD instances without running a server.

    config.json is admin-owned and never written; all runtime changes go to playback.json.
    """

    def __init__(self, config_path: str | Path, state_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path).resolve()
        self.state_path = Path(state_path) if state_path is not None else (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "mpdrussoundpi/playback.json"
        )
        self._tunein = TuneInCatalog()
        self.list_outputs()

    def _configuration(self) -> tuple[Configuration, str]:
        try:
            raw = self.config_path.read_bytes()
        except OSError as error:
            raise BackendError(f"Cannot read configuration: {error}") from error
        return parse_configuration(raw), hashlib.sha256(raw).hexdigest()

    def _read(self) -> tuple[Configuration, PlaybackState]:
        configuration, _ = self._configuration()
        return configuration, read_state(self.state_path, configuration)[0]

    @staticmethod
    def _settings(configuration: Configuration, state: PlaybackState) -> tuple[OutputSettings, ...]:
        result: list[OutputSettings] = []
        for output in configuration.outputs:
            current = state.outputs[output.id]
            result.append(OutputSettings(
                output.id, output.label, output.port, output.sink, current.gain_db,
                current.station, current.resolved_url or "", current.managed_song_id, current.intention,
            ))
        return tuple(result)

    def configuration_snapshot(self) -> tuple[Configuration, tuple[OutputSettings, ...], str]:
        """Outputs merged with runtime state, plus the revision of config.json alone."""
        configuration, revision = self._configuration()
        state = read_state(self.state_path, configuration)[0]
        return configuration, self._settings(configuration, state), revision

    def list_outputs(self) -> tuple[OutputSettings, ...]:
        return self.configuration_snapshot()[1]

    def get_gain(self, identity: str) -> float:
        """Return persisted gain, not the current mixer reading."""
        return self._select(identity)[0].gain_db

    def _select(self, identity: str | None) -> list[OutputSettings]:
        outputs = list(self.list_outputs())
        if identity is None:
            return outputs
        selected = [item for item in outputs if item.id == identity]
        if not selected:
            raise ValueError(f"Unknown output ID: {identity!r}")
        return selected

    def _write_outputs(
        self, changes: dict[str, dict[str, object]],
        check: Callable[[OutputState], bool] | None = None,
    ) -> None:
        state = self._read()[1]
        for identity, values in changes.items():
            if check is not None and not check(state.outputs[identity]):
                raise RevisionConflict("Playback source changed while saving its state")
            state = state.with_output(identity, **values)
        write_state(self.state_path, state)

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
        self, stream: OutputSettings, *, timeout: float = 5, redact_errors: bool = False,
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
            raise MPDOperationError(f"{stream.id}: MPD operation failed: {error}") from error
        finally:
            failing = sys.exc_info()[0] is not None
            try:
                client.disconnect()
            except (mpd.MPDError, OSError) as error:
                if failing:
                    if redact_errors:
                        logging.error("MPD observation disconnect failed during error cleanup")
                    else:
                        logging.error("%s: disconnect failed during error cleanup: %s", stream.id, error)
                else:
                    if redact_errors:
                        raise BackendError("MPD observation disconnect failed") from error
                    raise BackendError(f"{stream.id}: disconnect failed: {error}") from error

    @contextmanager
    def observation_connection(self, stream: OutputSettings) -> Generator[MPDConnection, None, None]:
        """An independent bounded connection with sanitized operational errors."""
        with self._connection(stream, timeout=3, redact_errors=True) as client:
            yield client

    def status(self, identity: str | None = None) -> tuple[PlaybackStatus, ...]:
        statuses: list[PlaybackStatus] = []
        for stream in self._select(identity):
            with self._connection(stream) as client:
                state = client.status().get("state")
            if state not in ("play", "pause", "stop"):
                raise BackendError(f"{stream.id}: invalid MPD playback state: {state!r}")
            statuses.append(PlaybackStatus(stream.id, state, stream.gain_db, f"sink.{stream.sink}"))
        return tuple(statuses)

    def start(self, identity: str | None = None) -> None:
        """Start one output, or every output that has a station."""
        with self._mutation():
            streams = self._select(identity)
            if identity is None:
                streams = [stream for stream in streams if stream.station is not None]
            elif streams[0].station is None:
                raise NoStationError(f"{identity}: no station selected")
            self._record_intention(streams, "play")
            self._start_selected(streams)

    def _start_selected(self, streams: list[OutputSettings], *, resolved: bool = False) -> None:
        try:
            wait_for_audio(self.config_path)
        except (OSError, RuntimeError) as error:
            raise AudioOperationError(str(error)) from error
        for stream in streams:
            self._start_stream(stream, resolve=not resolved)

    @staticmethod
    def _prepare_route_and_gain(stream: OutputSettings, gain: float, *, starting: bool = False) -> None:
        try:
            if starting:
                recover_route(stream.sink, timeout=STARTUP_ROUTE_TIMEOUT)
            else:
                recover_route(stream.sink)
            set_sink_gain(stream.sink, gain)
        except (OSError, RuntimeError) as error:
            raise AudioOperationError(str(error)) from error

    def _playing_source(self, stream: OutputSettings) -> bool:
        with self._connection(stream, redact_errors=True) as client:
            state = client.status()
            if state.get("state") != "play" or "error" in state:
                return False
            song_id = state.get("songid")
            return any(
                song.get("id") == song_id and song.get("file") == stream.url
                for song in client.playlistinfo()
            )

    def _start_stream(self, stream: OutputSettings, *, resolve: bool = True) -> None:
        station = stream.station
        if station is None:
            raise NoStationError(f"{stream.id}: no station selected")
        if station.type == "url" and station.url is not None:
            if stream.url != station.url:
                stream = self._persist_resolution(stream, station.url)
        # Re-resolve only when the saved URL is not already playing; resolution is a network call under the lock.
        elif resolve and station.station_id is not None and (not stream.url or not self._playing_source(stream)):
            try:
                resolved_url = self._tunein.resolve(station.station_id)
            except TuneInError:
                raise MPDOperationError("TuneIn station could not be resolved") from None
            if resolved_url != stream.url:
                stream = self._persist_resolution(stream, resolved_url)
        with self._connection(stream, redact_errors=True) as client:
            self._start_managed_stream(stream, client, client.playlistinfo())

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

    def _persist_resolution(self, stream: OutputSettings, url: str) -> OutputSettings:
        station = stream.station
        self._write_outputs(
            {stream.id: {"resolved_url": url}},
            lambda current: current.station == station,
        )
        return replace(stream, url=url)

    def _start_managed_stream(
        self, stream: OutputSettings, client: MPDConnection, playlist: object,
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

    def _persist_managed_song_id(self, stream: OutputSettings, song_id: str) -> None:
        if stream.managed_song_id == song_id:
            return
        station, url = stream.station, stream.url
        self._write_outputs(
            {stream.id: {"managed_song_id": song_id}},
            lambda current: current.station == station and current.resolved_url == url,
        )

    def _set_station(
        self, stream: OutputSettings, station: Station, progress: Callable[[str], None],
    ) -> None:
        if station.type == "tunein" and station.station_id is not None:
            try:
                source_url = self._tunein.resolve(station.station_id)
            except TuneInError as exc:
                raise StationUnavailableError(str(exc)) from None
        elif station.url is not None:
            source_url = station.url
        else:
            raise StationUnavailableError("Station has no playable source")
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
        progress("source_saving")
        try:
            # Station, cache and play intention are one atomic state write.
            self._write_outputs({stream.id: {
                "station": station, "resolved_url": source_url,
                "managed_song_id": managed_id, "intention": "play",
            }})
        except (OSError, ValueError) as error:
            raise BackendError("Cannot persist selected source") from error
        progress("source_persisted")
        progress("intention_persisted")
        selected = replace(
            stream, station=station, url=source_url, managed_song_id=managed_id, intention="play",
        )
        self._start_selected([selected], resolved=True)

    def stop(self, identity: str | None = None) -> None:
        self._playback_action(identity, pause=False)

    def pause(self, identity: str | None = None) -> None:
        self._playback_action(identity, pause=True)

    def _playback_action(self, identity: str | None, *, pause: bool) -> None:
        with self._mutation():
            streams = self._select(identity)
            if pause:
                # An output without a station has nothing to pause and stays stopped.
                streams = [stream for stream in streams if stream.station is not None]
            self._record_intention(streams, "pause" if pause else "stop")
            self._stop_selected(streams, pause=pause)

    def _stop_selected(self, streams: list[OutputSettings], *, pause: bool) -> None:
        for stream in streams:
            with self._connection(stream) as client:
                if pause:
                    client.pause(1)
                else:
                    client.stop()
                expected = "pause" if pause else "stop"
                if client.status().get("state") != expected:
                    raise MPDOperationError(f"{stream.id}: playback did not enter {expected}")

    def check_route(self, identity: str) -> StreamRoute:
        stream = self._select(identity)[0]
        try:
            return inspect_route(stream.sink)
        except (OSError, RuntimeError) as error:
            raise BackendError(f"{identity}: route inspection failed: {error}") from error

    def recover_route(self, identity: str | None = None) -> tuple[StreamRoute, ...]:
        with self._mutation():
            return tuple(recover_route(stream.sink) for stream in self._select(identity))

    def set_gain(self, identity: str, gain_db: float) -> None:
        gain = validate_gain_db(gain_db)
        with self._mutation():
            self._set_gain(self._select(identity)[0], gain)

    def _set_gain(self, stream: OutputSettings, gain: float) -> bool:
        with self._connection(stream) as client:
            state = client.status().get("state")
            if state not in ("play", "pause", "stop"):
                raise BackendError(f"{stream.id}: invalid MPD state {state}")
        active = state == "play"
        if active:
            self._prepare_route_and_gain(stream, gain)
        try:
            self._write_outputs({stream.id: {"gain_db": gain}})
        except (OSError, ValueError) as error:
            if active:
                try:
                    set_sink_gain(stream.sink, stream.gain_db)
                except (OSError, RuntimeError) as rollback_error:
                    raise GainPersistenceError(
                        f"{stream.id}: saving gain failed ({error}); gain rollback also failed ({rollback_error})",
                        rollback_failed=True,
                    ) from rollback_error
            raise GainPersistenceError(f"{stream.id}: cannot persist gain: {error}") from error
        return active

    def control_revision(self) -> str:
        """Include file identity/time so repeated identical state writes invalidate edits."""
        try:
            with self.state_path.open("rb") as source:
                raw = source.read()
                stat = os.fstat(source.fileno())
        except FileNotFoundError:
            return hashlib.sha256(b"no-intentions").hexdigest()
        version = f"{stat.st_ino}:{stat.st_mtime_ns}:".encode()
        return hashlib.sha256(version + raw).hexdigest()

    def operation_inventory(self) -> tuple[str, tuple[tuple[str, str, str, bool], ...]]:
        """Per output: ID, control revision, desired state and whether a station is selected."""
        _, outputs, revision = self.configuration_snapshot()
        control = self.control_revision()
        return revision, tuple(
            (output.id, control, output.intention, output.station is not None) for output in outputs
        )

    def operation_observation(self, identity: str) -> dict[str, object]:
        """Fresh recovery evidence, not proof that an interrupted command completed."""
        with self._mutation():
            _, outputs, revision = self.configuration_snapshot()
            stream = next((item for item in outputs if item.id == identity), None)
            if stream is None:
                raise KeyError(identity)
            with self.observation_connection(stream) as client:
                state = client.status().get("state")
                if state not in ("play", "pause", "stop"):
                    raise MPDOperationError("Invalid MPD state during recovery")
            return {
                "config_revision": revision, "control_revision": self.control_revision(),
                "desired_state": stream.intention, "observed_state": state,
                "configured_gain_db": stream.gain_db,
            }

    def validate_operation(self, identity: str, config_revision: str, control_revision: str) -> None:
        # Submission is read-only; execution revalidates under the mutation lock.
        self._operation_target(identity, config_revision, control_revision)

    def _check_revisions(self, config_revision: str, control_revision: str) -> None:
        self._check_revision(config_revision, self._configuration()[1])
        self._check_revision(control_revision, self.control_revision())

    def _operation_target(
        self, identity: str, config_revision: str, control_revision: str,
    ) -> OutputSettings:
        self._check_revisions(config_revision, control_revision)
        stream = next((item for item in self.list_outputs() if item.id == identity), None)
        if stream is None:
            raise KeyError(identity)
        return stream

    def manual_operation(
        self, identity: str, action: str, gain_db: float | None, *,
        expected_config_revision: str, expected_control_revision: str,
        progress: Callable[[str], None], station_id: str | None = None,
        station_name: str | None = None, favorite_key: str | None = None,
    ) -> None:
        if action not in OUTPUT_ACTIONS:
            raise ValueError("Unsupported manual action")
        gain = validate_gain_db(gain_db) if action == "set_gain" else None
        station = Station.tunein(station_id, station_name) if action == "set_tunein_station" else None
        if action == "select_favorite" and not isinstance(favorite_key, str):
            raise ValueError("A favorite key is required")
        with self._mutation():
            stream = self._operation_target(
                identity, expected_config_revision, expected_control_revision,
            )
            if action == "select_favorite" and favorite_key is not None:
                try:
                    station = self._read()[1].favorite(favorite_key)
                except KeyError:
                    raise FavoriteNotFound("Favorite is not in the list") from None
            if gain is not None:
                progress("gain_applying")
                active = self._set_gain(stream, gain)
                progress("gain_saved")
                progress("gain_live" if active else "gain_deferred")
            elif station is not None:
                self._set_station(stream, station, progress)
            else:
                if action == "play" and stream.station is None:
                    raise NoStationError(f"{identity}: no station selected")
                if action == "pause" and stream.station is None:
                    action = "stop"
                progress("intention_saving")
                self._record_intention([stream], action)
                progress("intention_persisted")
                if action == "play":
                    self._start_selected([stream])
                else:
                    self._stop_selected([stream], pause=action == "pause")

    def favorites(self) -> tuple[tuple[Station, ...], str, str]:
        """Favorites with the revisions a favorites operation must name."""
        configuration, revision = self._configuration()
        state = read_state(self.state_path, configuration)[0]
        return state.favorites, revision, self.control_revision()

    def validate_favorites_operation(self, config_revision: str, control_revision: str) -> None:
        self._check_revisions(config_revision, control_revision)

    def favorites_operation(
        self, action: str, *, expected_config_revision: str, expected_control_revision: str,
        key: str | None = None, index: int | None = None,
        station_id: str | None = None, station_name: str | None = None,
    ) -> None:
        if action not in FAVORITE_ACTIONS:
            raise ValueError("Unsupported favorites action")
        with self._mutation():
            self._check_revisions(expected_config_revision, expected_control_revision)
            _, state = self._read()
            try:
                if action == "add_favorite":
                    updated = state.add_favorite(Station.tunein(station_id, station_name))
                elif action == "remove_favorite" and key is not None:
                    updated = state.remove_favorite(key)
                elif action == "move_favorite" and key is not None and index is not None:
                    updated = state.move_favorite(key, index)
                else:
                    raise ValueError("Favorite operation fields are missing")
            except FavoritesFull as error:
                raise FavoritesFullError(str(error)) from None
            except KeyError:
                raise FavoriteNotFound("Favorite is not in the list") from None
            if updated is not state:
                write_state(self.state_path, updated)

    @staticmethod
    def _check_revision(expected: str | None, actual: str) -> None:
        if expected is not None and expected != actual:
            raise RevisionConflict("Configuration revision conflict; read the current configuration and retry")

    def _record_intention(self, streams: list[OutputSettings], state: str) -> None:
        self._write_outputs({stream.id: {"intention": state} for stream in streams})

    def initialize_intentions(self) -> None:
        """Persist defaults for outputs without runtime state; retain user choices."""
        with self._mutation():
            write_state(self.state_path, self._read()[1])

    def reconcile(self, identity: str) -> None:
        """Restore a saved intention without overwriting it with observed state."""
        with self._mutation():
            stream = self._select(identity)[0]
            intended = stream.intention
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
                        raise BackendError(f"{stream.id}: failed to restore {intended} intention")

    def apply_gain(self, identity: str | None = None) -> None:
        with self._mutation():
            for stream in self._select(identity):
                recover_route(stream.sink)
                set_sink_gain(stream.sink, stream.gain_db)
