# `web/config_types.py`

## Documentation

Defines typed configuration shapes and normalizers for backend endpoints, controller zone limits, poll intervals, and protocol audit logging.

## Requirements

- Python 3.11+.
- Configuration values are JSON-compatible dictionaries and lists.
- Backend ports and controller IDs/zone counts must be integers, excluding booleans.

## Test Cases

No dedicated test currently covers this module. Related behavior is reached indirectly through [backend tests](../../tests/test_russound_backend.py) and [server tests](../../tests/test_russound_server.py).

## Blind Spots

Malformed nested configuration, invalid host/port combinations, poll interval types, audit-log whitespace, and controller limit clamping are untested directly.
