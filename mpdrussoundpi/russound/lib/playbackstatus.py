from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import math
import re
import subprocess
import threading
import time
from typing import Generic, Literal, Protocol, TypeVar, cast

from lib.audioready import audio_ready_config
from lib.audioidentity import HardwareUnavailable
from lib.mpdbackend import BackendError, JSONValue, MPDBackend, MPDConnection, OutputSettings
from lib.streamgain import read_sink_gain
from lib.streamroute import inspect_route
from lib.stations import Station
from lib.sourceurl import source_display


class ReadValue(Protocol):
    def to_dict(self) -> dict[str, object]: ...


T = TypeVar("T", bound=ReadValue)
ProbeStatus = Literal["ok", "error", "pending", "not_applicable"]


@dataclass(frozen=True)
class ObservationError:
    code: str
    message: str
    retryable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class InvalidObservation(ValueError):
    pass


class InconsistentSnapshot(RuntimeError):
    pass


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        items: list[str] = []
        for item in cast(list[object], value):
            if not isinstance(item, str):
                raise InvalidObservation("Invalid metadata tags")
            items.append(item)
        value = "; ".join(items)
    if not isinstance(value, str):
        raise InvalidObservation("Invalid metadata text")
    # Tags and errors can contain endpoint credentials, not just MPD's file field.
    value = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+", "[redacted]", value)
    value = re.sub(r"\b[a-zA-Z0-9_.+-]+:[^\s@]+@[^\s]+", "[redacted]", value)
    value = "".join(character for character in value if character.isprintable()).strip()
    return value[:512] or None


def _number(value: object, *, integer: bool = False, required: bool = False) -> float | int | None:
    if value is None:
        if required:
            raise InvalidObservation("Required number is absent")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InvalidObservation("Invalid numeric field")
    if integer and isinstance(value, (str, int)):
        if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
            raise InvalidObservation("Invalid integer field")
        parsed_integer = int(value)
        if parsed_integer < 0:
            raise InvalidObservation("Invalid integer field")
        return parsed_integer
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as error:
        raise InvalidObservation("Invalid numeric field") from error
    if not math.isfinite(parsed) or parsed < 0 or (integer and not parsed.is_integer()):
        raise InvalidObservation("Invalid numeric field")
    return int(parsed) if integer else parsed


@dataclass(frozen=True)
class NowPlaying:
    title: str | None
    artist: str | None
    album: str | None
    station: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title, "artist": self.artist, "album": self.album,
            "station": self.station,
            "metadata_available": any((self.title, self.artist, self.album, self.station)),
        }


@dataclass(frozen=True)
class Playback:
    state: str
    song_id: str | None
    queue_entries: int
    elapsed_seconds: float | int | None
    duration_seconds: float | int | None
    bitrate_kbps: float | int | None
    audio_format: str | None
    mpd_error: str | None
    now_playing: NowPlaying
    current_source: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "reachable": True, "state": self.state, "song_id": self.song_id,
            "queue_entries": self.queue_entries, "elapsed_seconds": self.elapsed_seconds,
            "duration_seconds": self.duration_seconds, "bitrate_kbps": self.bitrate_kbps,
            "audio_format": self.audio_format, "mpd_error": self.mpd_error,
            "now_playing": self.now_playing.to_dict(), "current_source": self.current_source,
        }


def _response(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidObservation("Invalid MPD response")
    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise InvalidObservation("Invalid MPD field")
        result[key] = item
    return result


def read_playback(client: MPDConnection) -> Playback:
    for _ in range(2):
        before = _response(client.status())
        song = _response(client.currentsong())
        after = _response(client.status())
        song_id = after.get("songid")
        if song_id is not None and (not isinstance(song_id, str) or not song_id.isascii() or not song_id.isdigit()):
            raise InvalidObservation("Invalid song ID")
        if before.get("songid") != song_id or song.get("id") != song_id:
            continue
        if before.get("playlist") != after.get("playlist") or before.get("state") != after.get("state"):
            continue
        state = after.get("state")
        if state not in ("play", "pause", "stop"):
            raise InvalidObservation("Invalid playback state")
        entries = _number(after.get("playlistlength"), integer=True, required=True)
        if not isinstance(entries, int):
            raise InvalidObservation("Invalid queue length")
        return Playback(
            state, song_id, entries, _number(after.get("elapsed")),
            _number(after.get("duration")), _number(after.get("bitrate")),
            _text(after.get("audio")), "MPD reported a playback error." if _text(after.get("error")) else None,
            NowPlaying(_text(song.get("title")), _text(song.get("artist")),
                       _text(song.get("album")), _text(song.get("name"))),
                       source_display(song.get("file")),
        )
    raise InconsistentSnapshot("MPD snapshot did not settle")


@dataclass(frozen=True)
class Readiness:
    ready: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready}


@dataclass(frozen=True)
class Route:
    expected_sink: str
    actual_sink: str
    correct: bool

    def to_dict(self) -> dict[str, object]:
        return {"expected_sink": self.expected_sink, "actual_sink": self.actual_sink, "correct": self.correct}


@dataclass(frozen=True)
class Gain:
    measured_units: tuple[int, int]

    def to_dict(self) -> dict[str, object]:
        return {"measured_units": list(self.measured_units)}


@dataclass(frozen=True)
class TimedValue(Generic[T]):
    observed_at: str
    monotonic: float
    value: T

    def to_dict(self, now: float) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "age_ms": max(0, int((now - self.monotonic) * 1000)),
            "value": self.value.to_dict(),
        }


@dataclass(frozen=True)
class Probe(Generic[T]):
    status: ProbeStatus = "pending"
    attempted_at: str | None = None
    current: TimedValue[T] | None = None
    last_known: TimedValue[T] | None = None
    error: ObservationError | None = None

    def to_dict(self, now: float, stale_after: float) -> dict[str, object]:
        age = max(0, int((now - self.current.monotonic) * 1000)) if self.current else None
        return {
            "status": self.status, "attempted_at": self.attempted_at,
            "observed_at": self.current.observed_at if self.current else None,
            "age_ms": age,
            "stale": self.status in ("error", "pending") or (age is not None and age >= stale_after * 1000),
            "error": self.error.to_dict() if self.error else None,
            "value": self.current.value.to_dict() if self.current else None,
            "last_known": self.last_known.to_dict(now) if self.last_known else None,
        }


@dataclass(frozen=True)
class OutputObservation:
    id: str
    label: str
    configured_db: float
    station: Station | None = None
    selected_source: str | None = None
    playback: Probe[Playback] = Probe()
    readiness: Probe[Readiness] = Probe()
    route: Probe[Route] = Probe()
    gain: Probe[Gain] = Probe()

    def to_dict(self, now: float) -> dict[str, object]:
        route = self.route
        if route.status == "not_applicable" and (
            self.playback.current is None or now - self.playback.current.monotonic >= 10
            or self.playback.current.value.state == "play"
        ):
            route = Probe[Route]()
        station = self.station
        return {
            "id": self.id, "label": self.label, "selection": {
                "key": station.key if station else None,
                "label": station.name if station else None,
                "source": self.selected_source,
                "provider": station.type if station else None,
                "station_id": station.station_id if station else None,
                "station_name": station.name if station else None,
            },
            "gain": {"configured_db": self.configured_db},
            "playback": self.playback.to_dict(now, 10),
            "audio": {
                "readiness": self.readiness.to_dict(now, 30),
                "route": route.to_dict(now, 30), "gain": self.gain.to_dict(now, 30),
            },
        }


class PlaybackCollector:
    """Read a captured inventory; independently publish bounded output probes."""

    def __init__(self, backend: MPDBackend) -> None:
        configuration, streams, self.config_revision = backend.configuration_snapshot()
        self._streams: dict[str, OutputSettings] = {}
        self._outputs: dict[str, OutputObservation] = {}
        for stream in streams:
            label = _text(stream.label)
            if label is None:
                raise ValueError("Observation inventory requires printable labels")
            self._streams[stream.id] = stream
            self._outputs[stream.id] = OutputObservation(
                stream.id, label, stream.gain_db, stream.station, source_display(stream.url),
            )
        config = cast(JSONValue, configuration.raw)
        self._config = config
        self._backend = backend
        self._lock = threading.Lock()
        self._playback_pool = ThreadPoolExecutor(max_workers=len(streams), thread_name_prefix="playback-read")
        self._audio_pool = ThreadPoolExecutor(max_workers=len(streams), thread_name_prefix="audio-read")
        self._jobs: dict[tuple[str, str], Future[None]] = {}
        self._closed = False

    def apply_state(self, streams: tuple[OutputSettings, ...]) -> None:
        """Adopt runtime station/gain changes without discarding observations."""
        with self._lock:
            for stream in streams:
                if stream.id not in self._streams:
                    continue
                self._streams[stream.id] = stream
                self._outputs[stream.id] = replace(
                    self._outputs[stream.id], configured_db=stream.gain_db,
                    station=stream.station, selected_source=source_display(stream.url),
                )

    def snapshot(self) -> tuple[OutputObservation, ...]:
        with self._lock:
            return tuple(self._outputs[key] for key in sorted(self._outputs))

    def refresh(
        self, *, audio: bool = False, playback: bool = True,
        output_ids: tuple[str, ...] | None = None,
    ) -> tuple[Future[None], ...]:
        """Submit at most one playback/audio job per output; never wait for I/O."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Observation collector is closed")
            identities = tuple(self._streams) if output_ids is None else output_ids
            if any(identity not in self._streams for identity in identities):
                raise ValueError("Unknown observation output ID")
            for identity in identities:
                for kind, pool, operation in (
                    ("playback", self._playback_pool, self._collect_playback),
                    ("audio", self._audio_pool, self._collect_audio),
                ):
                    if kind == "audio" and not audio:
                        continue
                    if kind == "playback" and not playback:
                        continue
                    key = identity, kind
                    previous = self._jobs.get(key)
                    if previous is None or previous.done():
                        if previous is not None:
                            previous.result()
                        self._jobs[key] = pool.submit(operation, identity)
            return tuple(self._jobs.values())

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._playback_pool.shutdown(wait=True)
        self._audio_pool.shutdown(wait=True)
        for job in self._jobs.values():
            job.result()

    def _observe(
        self, identity: str, kind: str, previous: Probe[T], operation: Callable[[], T],
        error_code: str, message: str,
    ) -> Probe[T]:
        attempted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            value = operation()
        except (BackendError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            code = (error.code if isinstance(error, HardwareUnavailable) else
                    "snapshot_inconsistent" if isinstance(error, InconsistentSnapshot) else
                    "observation_invalid" if isinstance(error, (InvalidObservation, ValueError)) else error_code)
            logging.warning("Output %s %s observation failed: %s", identity, kind, code)
            return Probe(
                "error", attempted_at, last_known=previous.current or previous.last_known,
                error=ObservationError(code, message),
            )
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return Probe("ok", attempted_at, TimedValue(observed_at, time.monotonic(), value))

    def _collect_playback(self, identity: str) -> None:
        stream = self._streams[identity]
        def read() -> Playback:
            with self._backend.observation_connection(stream) as client:
                return read_playback(client)
        with self._lock:
            previous = self._outputs[identity].playback
        probe = self._observe(identity, "playback", previous, read, "mpd_unavailable", "MPD observation failed.")
        with self._lock:
            self._outputs[identity] = replace(self._outputs[identity], playback=probe)

    def _collect_audio(self, identity: str) -> None:
        stream = self._streams[identity]
        with self._lock:
            previous = self._outputs[identity]
        def readiness() -> Readiness:
            audio_ready_config(self._config, enforce_profile=False)
            return Readiness()
        def route() -> Route:
            result = inspect_route(stream.sink)
            return Route(result.expected_sink, result.actual_sink, result.correct)
        readiness_probe = self._observe(
            identity, "readiness", previous.readiness, readiness, "audio_unavailable", "Audio readiness inspection failed.",
        )
        playback = previous.playback.current
        fresh = playback is not None and time.monotonic() - playback.monotonic < 10
        if fresh and playback is not None and playback.value.state == "play":
            route_probe = self._observe(
                identity, "route", previous.route, route, "route_unavailable", "Playback route inspection failed.",
            )
        elif fresh:
            route_probe = Probe[Route]("not_applicable")
        else:
            route_probe = Probe[Route]()
        gain_probe = self._observe(
            identity, "gain", previous.gain, lambda: Gain(read_sink_gain(stream.sink)),
            "gain_unavailable", "Mixer gain inspection failed.",
        )
        with self._lock:
            self._outputs[identity] = replace(
                self._outputs[identity], readiness=readiness_probe, route=route_probe, gain=gain_probe,
            )
