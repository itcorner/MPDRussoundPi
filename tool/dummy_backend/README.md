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

## Capture and Decode Serial Traffic

The capture tool reads RNET frames from a serial device, prints each raw packet,
validates its checksum, and applies the same request labels and zone-update
parsers used by the live connector and dummy backend:

```bash
python -m tool.russound_capture --device /dev/cu.PL2303G-USBtoUART1120 --baud 19200
```

To capture over IP instead of a local serial port, point it at a ser2net (or
any RNET-over-TCP) host with `--host`/`--port` (default port `6666`). This
takes precedence over `--device`/`--baud`:

```bash
python -m tool.russound_capture --host 192.168.1.50 --port 6666
```

Captured hex frames can be replayed without hardware. Use one whitespace-separated
frame per line, or pipe the input through stdin:

```bash
printf '%s\n' 'F0 00 00 7F 00 00 70 05 02 02 00 00 F1 23 00 01 00 02 00 01 F7' \
	| python -m tool.russound_capture --input - --format json
```

The tool also provides an interactive serial monitor with a zone/action menu.
Use Left/Right to choose a zone, Up/Down to choose an action, and Enter to
send it. The menu includes zone info, power on/off, volume up/down, volume 42,
and mute. Scroll the trace with `j`/`k` (line), `u`/`d` or PageUp/PageDown
(page), and `g`/`G` or Home/End (top/bottom); the view stays pinned to the
newest lines until you scroll up, and jumps back to following new traffic
once you scroll back to the bottom (or press `G`). `q` quits. Every packet is
written to the log file with timestamped `DEBUG` raw and `INFO` semantic lines.
The default log is `russound_capture.log`; override it with `--log-file`:

```bash
python -m tool.russound_capture --tui \
	--device /dev/cu.PL2303G-USBtoUART1120 \
	--baud 19200 \
	--log-file russound_capture.log
```

Or against a ser2net host over TCP:

```bash
python -m tool.russound_capture --tui --host 192.168.1.50 --port 6666
```
