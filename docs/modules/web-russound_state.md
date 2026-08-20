# `web/russound_state.py`

## Documentation

Stores the collection of zones and inputs, system power, shared source state, persistence payloads, and update callbacks used by the web controller and dummy backend.

## Requirements

- Python 3.11+.
- Zone addresses must be unique within a state instance.
- JSON-compatible state values for file persistence and API payloads.

## Test Cases

State behavior is exercised through [controller tests](../../tests/test_russound_controller.py), [zone tests](../../tests/test_zone.py), and [dummy backend tests](../../tests/test_dummy_backend.py), especially persistence, updates, and zone mutation.

## Blind Spots

There is no focused test module for duplicate addresses, callback failures, empty/malformed state input, shared-source edge cases, or concurrent state mutation.
