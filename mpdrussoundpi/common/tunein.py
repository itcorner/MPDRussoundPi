"""Bounded TuneIn station search and playable-stream resolution."""

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
import json
import re
import threading
import time
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from .publicnetwork import public_opener, validate_public_url
from .sourceurl import validate_source_url


STATION_ID_PATTERN = re.compile(r"s[0-9]{1,12}\Z")
STATION_ID_QUERY_PATTERN = re.compile(r"[sS][0-9]{1,12}\Z")
SUPPORTED_MEDIA_TYPES = frozenset({"mp3", "aac", "ogg"})
TUNEIN_API = "https://opml.radiotime.com"
MAX_RESPONSE_BYTES = 512_000
MAX_PLAYLIST_BYTES = 16_384
PLAYLIST_TYPES = frozenset({"m3u", "pls"})
PLAYLIST_SUFFIXES = (".m3u", ".m3u8", ".pls", ".asx", ".xspf")
MAX_RESULTS = 20
SEARCH_CACHE_SECONDS = 60
SEARCH_CACHE_SIZE = 128


class TuneInError(RuntimeError):
    """Safe, user-facing TuneIn failure with no upstream response details."""


class TuneInUnavailable(TuneInError):
    """TuneIn could not be reached or returned an unreadable response."""


@dataclass(frozen=True)
class TuneInStation:
    station_id: str
    name: str
    description: str | None = None
    bitrate_kbps: int | None = None
    formats: str | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "id": self.station_id,
            "name": self.name,
            "description": self.description,
            "bitrate_kbps": self.bitrate_kbps,
            "formats": self.formats,
        }


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(character for character in value if character.isprintable()).strip()
    return text[:limit] or None


def _valid_public_stream_url(value: object) -> str:
    url = validate_source_url(value)
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or not parts.hostname:
        raise TuneInError("TuneIn returned an unsupported station stream.")
    try:
        validate_public_url(url)
    except ValueError:
        raise TuneInError("TuneIn returned an unsupported station stream.")
    return url


class TuneInCatalog:
    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        cache_seconds: int = SEARCH_CACHE_SECONDS,
        cache_size: int = SEARCH_CACHE_SIZE,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, tuple[TuneInStation, ...]]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def validate_query(value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value.strip()) < 2
            or len(value) > 80
            or not value.isprintable()
            or any(character in value for character in "\r\n\t")
        ):
            raise ValueError("Search query must contain 2 to 80 printable characters.")
        return value.strip()

    @staticmethod
    def validate_station_id(value: object) -> str:
        if not isinstance(value, str) or STATION_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Invalid TuneIn station identity.")
        return value

    def search(self, value: object) -> list[dict[str, str | int | None]]:
        query = self.validate_query(value)
        key = query.casefold()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(key)
                return [station.to_dict() for station in cached[1]]
            self._cache.pop(key, None)

        if STATION_ID_QUERY_PATTERN.fullmatch(query) is not None:
            stations = self._lookup_station(query.lower())
        else:
            params = urlencode({"query": query, "formats": "mp3,aac,ogg", "render": "json"})
            payload = self._fetch_json(f"/Search.ashx?{params}")
            stations = self._parse_search(payload)
        with self._lock:
            self._cache[key] = (now + self.cache_seconds, tuple(stations))
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return [station.to_dict() for station in stations]

    def resolve(self, station_id: object) -> str:
        return self._direct_stream(self.validate_station_id(station_id))[0]

    def _direct_stream(self, identity: str) -> tuple[str, dict[str, object]]:
        """Return a direct stream URL, preferring direct entries over simple M3U/PLS playlists."""
        params = urlencode({"id": identity, "formats": "mp3,aac,ogg", "render": "json"})
        payload = self._fetch_json(f"/Tune.ashx?{params}")
        candidates: list[dict[str, object]] = []
        for entry in self._body(payload):
            if not isinstance(entry, dict):
                continue
            item = cast(dict[str, object], entry)
            media_type = item.get("media_type")
            if (
                item.get("element") != "audio"
                or not isinstance(media_type, str)
                or media_type.lower() not in SUPPORTED_MEDIA_TYPES
            ):
                continue
            candidates.append(item)
        for item in candidates:
            if item.get("is_direct") not in (True, "true"):
                continue
            try:
                return _valid_public_stream_url(item.get("url")), item
            except (TypeError, ValueError, TuneInError):
                continue
        for item in candidates:
            playlist_type = item.get("playlist_type")
            if item.get("is_direct") in (True, "true") or not isinstance(playlist_type, str) \
                    or playlist_type.lower() not in PLAYLIST_TYPES:
                continue
            try:
                playlist_url = _valid_public_stream_url(item.get("url"))
                text = self._fetch_bytes(playlist_url, MAX_PLAYLIST_BYTES).decode("utf-8", "replace")
            except (TypeError, ValueError, TuneInError):
                continue
            for url in self._playlist_entries(text):
                try:
                    return _valid_public_stream_url(url), item
                except (TypeError, ValueError, TuneInError):
                    continue
        raise TuneInError("TuneIn did not return a supported stream for this station.")

    @staticmethod
    def _playlist_entries(text: str) -> list[str]:
        entries: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("\ufeff")
            if not line or line.startswith(("#", "[", ";")):
                continue
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"(?i)file[0-9]{1,3}", key.strip()):
                line = value.strip()
            elif separator and "://" not in key:
                continue
            if urlsplit(line).path.lower().endswith(PLAYLIST_SUFFIXES):
                continue
            entries.append(line)
            if len(entries) == 10:
                break
        return entries

    def _lookup_station(self, identity: str) -> list[TuneInStation]:
        """Return only the station with this ID, if it exists and has a supported direct stream."""
        params = urlencode({"id": identity, "render": "json"})
        payload = self._fetch_json(f"/Describe.ashx?{params}")
        head = payload.get("head")
        if isinstance(head, dict) and str(cast(dict[str, object], head).get("status")) != "200":
            return []
        station: dict[str, object] | None = None
        for entry in self._body(payload):
            if isinstance(entry, dict) and entry.get("element") == "station" and entry.get("guide_id") == identity:
                station = cast(dict[str, object], entry)
                break
        name = _clean_text(station.get("name"), 120) if station is not None else None
        if station is None or name is None:
            return []
        try:
            _, stream = self._direct_stream(identity)
        except TuneInUnavailable:
            raise
        except TuneInError:
            return []
        details = [
            _clean_text(station.get(field), 120) for field in ("slogan", "location")
        ]
        return [TuneInStation(
            identity,
            name,
            " · ".join(detail for detail in details if detail) or None,
            self._bitrate(stream.get("bitrate")),
            _clean_text(cast(str, stream.get("media_type")).lower(), 32),
        )]

    @staticmethod
    def _bitrate(value: object) -> int | None:
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if 0 < parsed <= 1000:
                return parsed
        return None

    def _fetch_bytes(self, url: str, limit: int, accept: str = "*/*") -> bytes:
        request = Request(url, headers={"Accept": accept, "User-Agent": "MPDRussoundPi/1.0"})
        opener = public_opener()
        try:
            validate_public_url(url)
            with closing(opener.open(request, timeout=self.timeout_seconds)) as response:
                raw = response.read(limit + 1)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise TuneInUnavailable("TuneIn is unavailable; try again later.") from None
        if len(raw) > limit:
            raise TuneInError("TuneIn response exceeded the supported size.")
        return raw

    def _fetch_json(self, path: str) -> dict[str, object]:
        raw = self._fetch_bytes(f"{TUNEIN_API}{path}", MAX_RESPONSE_BYTES, "application/json")
        try:
            payload: object = json.loads(raw)
        except (ValueError, UnicodeError):
            raise TuneInUnavailable("TuneIn is unavailable; try again later.") from None
        if not isinstance(payload, dict):
            raise TuneInError("TuneIn returned an invalid response.")
        return cast(dict[str, object], payload)

    @staticmethod
    def _body(payload: dict[str, object]) -> list[object]:
        head_value, body = payload.get("head"), payload.get("body")
        if not isinstance(head_value, dict) or not isinstance(body, list):
            raise TuneInError("TuneIn returned an invalid response.")
        head = cast(dict[str, object], head_value)
        if str(head.get("status")) != "200":
            raise TuneInError("TuneIn returned an invalid response.")
        return cast(list[object], body)

    @classmethod
    def _parse_search(cls, payload: dict[str, object]) -> list[TuneInStation]:
        stations: list[TuneInStation] = []
        seen: set[str] = set()
        for entry in cls._body(payload):
            if not isinstance(entry, dict):
                continue
            item = cast(dict[str, object], entry)
            if item.get("type") != "audio" or item.get("item") != "station":
                continue
            station_id = item.get("guide_id")
            name = _clean_text(item.get("text"), 120)
            if (
                not isinstance(station_id, str)
                or STATION_ID_PATTERN.fullmatch(station_id) is None
                or name is None
                or station_id in seen
            ):
                continue
            bitrate = cls._bitrate(item.get("bitrate"))
            formats = _clean_text(item.get("formats"), 32)
            stations.append(TuneInStation(
                station_id,
                name,
                _clean_text(item.get("subtext"), 240),
                bitrate,
                formats,
            ))
            seen.add(station_id)
            if len(stations) == MAX_RESULTS:
                break
        return stations
