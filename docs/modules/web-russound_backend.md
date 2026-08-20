# `web/russound_backend.py`

## Documentation

Provides the application-facing adapter around the Russound connector. It resolves endpoints and zone scope, reads zone state, writes power/source/volume and sound parameters, and handles keypad display and global power operations.

## Requirements

- Python 3.11+.
- `web.russound_connector.Russound` or a compatible test double.
- Optional backend configuration with host, port, controller limits, and audit-log path.

## Test Cases

[Backend tests](../../tests/test_russound_backend.py) cover connection reuse/failure, endpoint configuration, parameter reads/writes, source mapping, scope validation, global power, and keypad display validation. [Dummy backend tests](../../tests/test_dummy_backend.py) exercise selected connector behavior end to end.

## Blind Spots

Not every adapter method has a direct test, including listener startup, disconnect variants, some read failures, all advanced parameter bounds, and backend exception paths.
