import re
from collections.abc import Mapping
from typing import cast


def output_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise ValueError("Output ID must match [a-z][a-z0-9_-]{0,63}")
    return value


def output_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or not all(c.isprintable() for c in value):
        raise ValueError("Output label must be nonempty and printable")
    return value


def identities(entries: object, *, required: bool = False) -> dict[str, tuple[str, str]]:
    if not isinstance(entries, list) or not entries:
        raise ValueError("Configuration requires a nonempty streams list")
    streams: list[Mapping[str, object]] = []
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            raise ValueError("Invalid stream entry")
        streams.append(cast(Mapping[str, object], entry))
    if not required and all("id" not in entry and "label" not in entry for entry in streams):
        return {}
    result: dict[str, tuple[str, str]] = {}
    used: set[str] = set()
    for entry in streams:
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise ValueError("Stream names must be nonempty and unique")
        identity, label = output_id(entry.get("id")), output_label(entry.get("label"))
        if identity in used:
            raise ValueError("Duplicate output ID")
        result[name] = (identity, label)
        used.add(identity)
    return result
