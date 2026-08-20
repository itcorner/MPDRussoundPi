# `tool/dummy_backend/dummy_backend.py`

## Documentation

Provides a TCP Russound simulator with configurable zone/keypad state, RNET request parsing, state persistence, unsolicited updates, display handling, and optional terminal UI support.

## Requirements

- Python 3.11+.
- Local TCP sockets; curses is needed only for the optional TUI.
- State JSON matching the simulator's zone and keypad schema.

## Test Cases

[Dummy backend integration tests](../../tests/test_dummy_backend.py) cover read/write round trips, all-zone power, persistence, unsolicited broadcasts, checksum generation, broadcast and targeted keypad displays, and ignored unsimulated keypads.

## Blind Spots

Many parser branches, malformed frames, all zone fields, TUI behavior, concurrent client failures, and command-line startup/shutdown paths are untested.
