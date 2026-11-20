# `web/russound_controller.py`

## Documentation

Application logic over the configuration and persisted state, which it reads and writes through [`config_store.py`](./web-config_store.md). Synchronizes hardware state, applies zone settings and shortcuts, manages shared/system power, and builds payloads for the web UI and configuration editor. `file_generations()` lets callers detect any config or state change since a read; `config_message()` explains a missing or invalid configuration. Each server creates its own controller instance.

## Requirements

- Python 3.11+.
- JSON config/state paths when persistence is used.
- A Russound backend for hardware-backed operations; local advanced-setting changes can run without one.

## Test Cases

[Controller tests](../../../tests/test_russound_controller.py) cover state loading, config revisions and load states, initial and legacy state, persistence, backend failures, view/config payloads, zone updates, shortcuts, mappings, and system power. [Backend tests](../../../tests/test_russound_backend.py) cover the adapter boundary.

## Blind Spots

Some helper branches, malformed JSON/config shapes, missing zones or shortcuts, concurrent persistence, backend synchronization failures, and every public handler combination are not directly tested.
