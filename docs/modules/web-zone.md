# `web/zone.py`

## Documentation

Models one logical Russound zone, including its physical controller/zone address, playback state, advanced sound settings, serialization, and backend write operations.

## Requirements

- Python 3.11+.
- A backend object implementing the zone setter methods used by `apply_to_backend`.
- Source IDs and zone addresses follow the repository's 1-based configuration convention.

## Test Cases

See [zone tests](../../tests/test_zone.py) for construction, serialization, normalization, addressing, direct power updates, and applying all settings to a backend. [Controller tests](../../tests/test_russound_controller.py) add persistence and targeted-update coverage.

## Blind Spots

Backend failure behavior for each individual zone setter, invalid ranges, and unusual missing dictionary fields are only partially covered.
