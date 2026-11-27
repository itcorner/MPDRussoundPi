"""Schema versions of the web application's persisted files.

docs/plans/general_open_persisted-data-schema-version.md. Each versioned JSON file
carries a top-level ``schema_version``; a file without it is version 1 (the format
before versions existed). This module is the single source for:

- the version each file is written in and the oldest version still read (and
  migrated forward, one step at a time);
- the image label that tells the updater, before anything is pulled or stopped,
  which versions an image reads (``label()``, ``parse_label()``, ``problems()``).

A file in a newer version than this code knows is never rewritten: the caller keeps
it read-only (``NewerSchemaError``).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping

FIELD = "schema_version"
LABEL = "net.itcorner.mpdrussoundpi.data-schema"


@dataclass(frozen=True)
class Schema:
    title: str      # in messages: "the settings"
    current: int    # written by this code
    oldest: int     # the oldest version this code still reads and migrates


SCHEMAS: dict[str, Schema] = {
    "config": Schema("the settings", current=1, oldest=1),
    "state": Schema("the room state", current=1, oldest=1),
    "lockout": Schema("the login lockout", current=1, oldest=1),
}

# MIGRATIONS[name][n] turns a version-n value into a version-(n + 1) value. None yet:
# add one together with the step of SCHEMAS[name].current (and a test with a
# recorded version-n file).
MIGRATIONS: dict[str, dict[int, Callable[[dict[str, Any]], dict[str, Any]]]] = {name: {} for name in SCHEMAS}

_LABEL_ITEM = re.compile(r"([a-z]+)=(\d{1,4})(?:-(\d{1,4}))?\Z")


class SchemaError(ValueError):
    """The version field is malformed, or the file is older than this code still reads."""


class NewerSchemaError(SchemaError):
    """Written by a newer application version: keep the file read-only."""

    def __init__(self, name: str, version: int) -> None:
        schema = SCHEMAS[name]
        super().__init__(f"{schema.title.capitalize()} were saved by a newer version of this application "
                         f"(format {version}; this version reads up to {schema.current}). They are shown "
                         "read-only and are not changed; update the application to edit them.")
        self.name, self.version = name, version


def version_of(value: object) -> int:
    """The version of a parsed file; a missing field means version 1."""
    if not isinstance(value, dict) or FIELD not in value:
        return 1
    version = value[FIELD]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SchemaError(f"{FIELD} must be a positive integer")
    return version


def load(name: str, value: object) -> tuple[Any, int]:
    """``(value in the current version, version found)``; raises ``NewerSchemaError`` for newer files.

    Migrations return new objects; the caller writes the migrated value only when it
    saves next, so reading alone never changes a file.
    """
    schema = SCHEMAS[name]
    version = version_of(value)
    if version > schema.current:
        raise NewerSchemaError(name, version)
    if version < schema.oldest:
        raise SchemaError(f"{schema.title.capitalize()} use format {version}, which this version no longer "
                          f"reads (oldest {schema.oldest})")
    for step in range(version, schema.current):
        assert isinstance(value, dict)
        value = MIGRATIONS[name][step](value)
    return value, version


def stamp(name: str, value: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``value`` with the current version, for writing."""
    return {**value, FIELD: SCHEMAS[name].current}


def label() -> str:
    """``config=1;lockout=1;state=1``, or ``config=1-2`` once a file reads more than one version."""
    return ";".join(f"{name}={schema.current}" if schema.oldest == schema.current
                    else f"{name}={schema.oldest}-{schema.current}" for name, schema in sorted(SCHEMAS.items()))


def parse_label(text: object) -> dict[str, tuple[int, int]] | None:
    """``{name: (oldest, current)}`` from an image label; None when absent or malformed."""
    if not isinstance(text, str) or not text or len(text) > 200:
        return None
    result: dict[str, tuple[int, int]] = {}
    for item in text.split(";"):
        match = _LABEL_ITEM.fullmatch(item)
        if match is None or match.group(1) in result:
            return None
        current = int(match.group(3) or match.group(2))
        oldest = int(match.group(2))
        if oldest < 1 or current < oldest:
            return None
        result[match.group(1)] = (oldest, current)
    return result


def problems(target: Mapping[str, tuple[int, int]] | None, data: Mapping[str, int]) -> list[str]:
    """Why an image with label ``target`` cannot run on ``data`` (versions found on the host).

    An image without the label predates schema versions: it reads version 1 only.
    """
    reads = dict(target) if target is not None else {name: (1, 1) for name in SCHEMAS}
    found = []
    for name, version in sorted(data.items()):
        oldest, current = reads.get(name, (1, 1))
        title = SCHEMAS[name].title if name in SCHEMAS else name
        if version > current:
            found.append(f"{title} (format {version}; that version reads up to {current})")
        elif version < oldest:
            found.append(f"{title} (format {version}; that version reads from {oldest})")
    return found
