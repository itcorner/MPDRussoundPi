# Dummy Russound Backend

This directory contains a small TCP server that emulates the Russound RNET protocol used by the app.

It is intended for local testing without RS-232 hardware. The server listens on TCP, accepts the same request packets the Russound client library sends, and returns protocol-shaped responses for zone reads.

## Run

```bash
python -m tool.dummy_backend.dummy_backend --host 127.0.0.1 --port 6666 --state tool/dummy_backend/example_state.json
```

## TUI

```bash
python -m tool.dummy_backend.dummy_backend --tui --state tool/dummy_backend/example_state.json
```

To edit the live backend state while the TCP server is running, start both modes together:

```bash
python -m tool.dummy_backend.dummy_backend --serve --tui --state tool/dummy_backend/example_state.json
```

In the TUI, `Tab` switches between the zone list and the field list. Use the arrow keys to navigate, `+`/`-` to change values, `Space` to toggle booleans, `S` to save, and `Q` to quit.

## Notes

- The server is stateful and updates zone power, source, volume, bass, treble, loudness, balance, turn-on volume, background color, do-not-disturb, and party mode when it receives matching protocol messages.
- The TUI edits the same JSON-backed state object and saves it back to the file path you pass with `--state`.
- It is designed to work with the Russound Python client used by this repo, which uses TCP sockets rather than RS-232 hardware directly.
- Zone numbers and controller numbers are handled using the Russound protocol's zero-based wire format.
