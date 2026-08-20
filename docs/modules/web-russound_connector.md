# `web/russound_connector.py`

## Documentation

Implements the TCP RNET connector: frame construction, checksum handling, response matching, zone reads/writes, display messages, protocol auditing, and unsolicited update listening.

## Requirements

- Python 3.11+.
- A reachable Russound TCP gateway, or a socket-compatible test double.
- RNET frames use `F0`/`F7` boundaries, inverted `F1` bytes, and the repository checksum convention.

## Test Cases

[Connector tests](../../tests/test_russound_connector.py) cover update parsing, reset handling, checksums, all-zone targeting, protocol audit logs, extended zone information, parameter normalization, and display frames. [Dummy backend tests](../../tests/test_dummy_backend.py) provide wire-level round trips.

## Blind Spots

Partial/multiple frame buffering, unmatched frame retention, most setter methods, mute behavior, listener callbacks under concurrency, malformed frames, and socket send/receive failures need more direct tests.
