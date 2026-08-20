# `web/russound_controller.py`

## Documentation

Coordinates configuration and persisted state, synchronizes hardware state, applies zone settings and shortcuts, manages shared/system power, and builds payloads for the web UI and configuration editor.

## Requirements

- Python 3.11+.
- JSON config/state paths when persistence is used.
- A Russound backend for hardware-backed operations; local advanced-setting changes can run without one.

## Test Cases

[Controller tests](../../tests/test_russound_controller.py) cover singleton loading, initial and legacy state, persistence, backend failures, view/config payloads, zone updates, shortcuts, mappings, and system power. [Backend tests](../../tests/test_russound_backend.py) cover the adapter boundary.

## Blind Spots

Some helper branches, malformed JSON/config shapes, missing zones or shortcuts, concurrent persistence, backend synchronization failures, and every public handler combination are not directly tested.
