# `web/status_overview.py` and `web/audio_status.py`

## Documentation

`status_overview.py` builds the sections of `GET /api/status/overview` as pure
functions over cached data: the web process (uptime, runtime, build revision),
the Russound gateway and controllers, and per playback agent its connection,
features, controls and TLS certificate expiry, plus the audio system with one
group per output and one per sound card. Every section is
`{id, title, level, summary, rows, groups}`; levels are `ok`, `warning`,
`problem` or `unknown` and are always spelled out in the page text. Values come
from a fixed vocabulary or from fields that were validated and cleaned of
control characters; nothing is rendered as markup. Provenance is explicit: the
revision is "reported by the build", the runtime "detected", missing facts "not
reported".

`audio_status.py` validates the agent's `GET /v1/audio` response (see the
[read contract](../mpd-playback-read-api.md#audio-hardware)) with exact keys,
strict types and bounds before the client caches it. Only model-level facts pass:
card profile, USB vendor and product ID, sink ids, descriptions and channel
names.

## Requirements

- Python 3.11+, standard library only.

## Test Cases

[Status overview tests](../../../tests/test_status_overview.py) cover audio
validation, every section builder (levels, wording, hostile labels, serial mode
without device paths), the client's audio and certificate polling, and the
endpoint without any probing. The agent side is covered by
[audio info tests](../../../tests/test_audioinfo.py).
