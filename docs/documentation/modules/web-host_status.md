# `web/host_status.py`

## Documentation

Validates, assesses and formats the host facts that the playback agent reports
(`GET /v1/host`, see the [read contract](../mpd-playback-read-api.md#host-information)).
The Status page has no login, so nothing the agent sends is forwarded as it
arrived: each of the eight sections (`system`, `cpu`, `memory`, `temperature`,
`power`, `disk`, `network`, `clock`) is rebuilt from an allowlist with exact
keys, strict types and bounds (booleans are not numbers, text is printable ASCII
and short, addresses must be real non-local IP addresses). An invalid section
degrades alone to an error probe; unknown fields and sections are dropped, and
agent error text is replaced by a fixed code.

`assess` turns the values into `{level, label}` using thresholds kept at the top
of the module (Wi-Fi good from -60 dBm and fair to -70, memory low below 10 %
available, temperature high from 70 °C and critical from 80 °C, power and
throttling flags, clock synchronization). `view` builds the one-line summary and
the detail rows as plain text. `PlaybackStatusClient` calls it and serves the
result through `GET /api/status/host`.

## Requirements

- Python 3.11+, standard library only.

## Test Cases

[Host status tests](../../../tests/test_host_status.py) cover the reference
document, unknown fields, invalid and hostile values, error reduction, every
threshold boundary, the display rows, the client's polling rules (capability,
interval, separate revision, failure isolation, effective age) and the endpoint.
Fixture: [host-info-v1.json](../../../tests/fixtures/host-info-v1.json).

## Blind Spots

Several agents on one physical host produce one card each; merging them needs
the host identity comparison described in the deployment overview plan. The web
host's own facts are not shown yet.
