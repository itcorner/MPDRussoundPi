# Test Coverage Review

This review covers the Python production modules in `web/` and `tool/`.

## Baseline

- The documented `python -m unittest -q` command discovers zero tests in this checkout.
- The working command is `python3 -m unittest discover -s tests -p 'test_*.py' -q`.
- No `coverage` or `pytest` package is installed, so this is a test-to-module and behavior review, not a measured line-coverage report.
- The suite contains 103 named test methods across six test modules. This review maps the 8 non-package-marker modules in scope.
- In the project virtual environment, 101 tests pass and 2 existing controller tests fail because their `DummyClient` doubles do not implement `get_zone_extended_info` during state refresh.

## Covered Areas

- Web zone model, state/controller orchestration, backend adapter, connector protocol behavior, and Flask/SSE server behavior have dedicated tests.
- `tool/dummy_backend/dummy_backend.py` has TCP integration tests for zone reads/writes, broadcasts, persistence, checksums, and keypad displays.
- Connector tests cover checksum generation, selected frame parsing, zone information, user parameter normalization, displays, audit logging, and connection-reset handling.

## Blind Spots

- `web/config_types.py` has no dedicated tests for malformed configuration, boolean-vs-integer rejection, endpoint defaults, or controller-limit normalization.
- The `web/` and `tool/` package marker files are intentionally not documented as modules because they contain no application behavior.
- Frontend JavaScript and HTML files are not covered by the Python suite.
- Ansible playbooks and templates are not covered by automated tests.
- Several web tests exercise private helpers or mocks rather than full HTTP request flows for every route.
- Hardware-dependent behavior, systemd/PulseAudio/PipeWire commands, and real MPD connections are not tested in this environment.

## Test Links

- [Zone tests](../tests/test_zone.py)
- [Backend tests](../tests/test_russound_backend.py)
- [Connector tests](../tests/test_russound_connector.py)
- [Controller tests](../tests/test_russound_controller.py)
- [Server tests](../tests/test_russound_server.py)
- [Dummy backend integration tests](../tests/test_dummy_backend.py)

Each module document below links its closest test cases and names the remaining untested behavior.
