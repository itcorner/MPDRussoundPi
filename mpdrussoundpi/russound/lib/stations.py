"""Station values shared by the admin configuration and the runtime favorites."""

from dataclasses import dataclass
import re
from typing import Literal, cast

from lib.sourceurl import validate_source_url
from lib.tunein import TuneInCatalog


MAX_STATIONS = 20
MAX_STATION_NAME = 120
STATION_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
StationType = Literal["url", "tunein"]


def station_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_STATION_NAME
        or not value.isprintable()
    ):
        raise ValueError("Station name must be printable and at most 120 characters")
    return value


@dataclass(frozen=True)
class Station:
    key: str
    name: str
    type: StationType
    station_id: str | None = None
    url: str | None = None

    @classmethod
    def tunein(cls, station_id: object, name: object) -> "Station":
        identity = TuneInCatalog.validate_station_id(station_id)
        return cls(f"tunein:{identity}", station_name(name), "tunein", identity)

    @classmethod
    def parse(cls, value: object, *, allow_url: bool = True) -> "Station":
        if not isinstance(value, dict):
            raise ValueError("Station must be an object")
        entry = cast(dict[str, object], value)
        kind = entry.get("type")
        if kind == "tunein":
            if set(entry) != {"key", "name", "type", "station_id"}:
                raise ValueError("TuneIn station requires exactly key, name, type and station_id")
            station = cls.tunein(entry.get("station_id"), entry.get("name"))
            if entry.get("key") != station.key:
                raise ValueError(f"TuneIn station key must be {station.key!r}")
            return station
        if kind == "url" and allow_url:
            if set(entry) != {"key", "name", "type", "url"}:
                raise ValueError("URL station requires exactly key, name, type and url")
            key = entry.get("key")
            if not isinstance(key, str) or STATION_KEY_PATTERN.fullmatch(key) is None:
                raise ValueError("URL station key must match [a-z0-9][a-z0-9_-]{0,63}")
            try:
                url = validate_source_url(entry.get("url"))
            except ValueError:
                raise ValueError(f"{key}: url must be a direct HTTP(S) URL") from None
            return cls(key, station_name(entry.get("name")), "url", url=url)
        raise ValueError("Station type must be 'url' or 'tunein'")

    def to_json(self) -> dict[str, str]:
        value = {"key": self.key, "name": self.name, "type": self.type}
        if self.station_id is not None:
            value["station_id"] = self.station_id
        if self.url is not None:
            value["url"] = self.url
        return value

    def public(self) -> dict[str, str | None]:
        """Client view: never includes a stream URL."""
        return {"key": self.key, "name": self.name, "type": self.type, "station_id": self.station_id}


def parse_station_list(value: object, *, allow_url: bool) -> tuple[Station, ...]:
    if not isinstance(value, list):
        raise ValueError("Stations must be a list")
    items = cast(list[object], value)
    if len(items) > MAX_STATIONS:
        raise ValueError(f"At most {MAX_STATIONS} stations are allowed")
    stations = tuple(Station.parse(item, allow_url=allow_url) for item in items)
    if len({station.key for station in stations}) != len(stations):
        raise ValueError("Station keys must be unique")
    return stations
