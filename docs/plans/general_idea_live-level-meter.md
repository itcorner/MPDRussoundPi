# Idea: live stream level meter (volume visualization)

Status: **idea, not scheduled** (noted 2026-10-23). Not part of S01–S11; needs
a decision before implementation.

## Goal

Show a live level meter (VU/peak bar) per output on the dashboard / playback
page, so you can see that a stream is actually producing audio and how loud it
is relative to its calibration gain.

## External proposal and validation

An external AI suggested a Node.js `ws` server (`server.js`, port 8080) that
starts one `pw-record` per requested PipeWire source, pools processes per
source across browser tabs, computes RMS over 1024-byte chunks and pushes
`{"volume": 0..100}` to every tab. Only the **pooling idea** (one capture per
output, fan-out to all viewers, stop when the last viewer leaves) is worth
keeping. The rest does not fit our architecture or has bugs:

| # | Issue in the proposal | Why it matters here |
|---|-----------------------|---------------------|
| 1 | New Node.js runtime and `ws` dependency | The Pi and web stack are Python (Flask/waitress agent and web). Adds a runtime, a unit and a deployment path for one feature. |
| 2 | Browser connects directly to `ws://<host>:8080` | Violates the [selected architecture](../documentation/mpd-web-integration-architecture.md#5-recommended-deployment-and-ownership): the browser talks only same-origin to the web app; the web talks to the agent over Caddy TLS (`8443`), CIDR filter and bearer token. A plain, unauthenticated port breaks the split Docker deployment, causes mixed content on HTTPS and bypasses all of that. |
| 3 | `source` query parameter is passed straight to `--target` | Lets any client record **any** PipeWire node. `auto` records the default *source*, which on the Pi is likely the USB card's microphone/line input, not the playback stream. Must be an allowlist: output ID → `sink.<id>` monitor. |
| 4 | Recording a sink needs its monitor | `--target sink.x` alone is not guaranteed to capture the sink monitor; likely needs `-P '{ stream.capture.sink = true }'` (verify on the Pi with WirePlumber 0.5.8). The capture stream must also not disturb the routes checked by the supervisor/`audioready` (verify it does not appear as a misrouted sink input). |
| 5 | 1024-byte chunks → ~190 messages/s per client at 48 kHz stereo | Far too chatty. Aggregate to ~10–20 Hz frames with peak and RMS per channel. |
| 6 | Linear 0–100 % with `16384` as full scale | 16384 is −6 dBFS, so the bar clips early and the scale is linear. The project uses dB everywhere (gain −60…0 dB, dBFS measurements): report dBFS. |
| 7 | Interleaved channels averaged together | Our outputs are channel pairs of a 5.1 card; report per channel (L/R). |
| 8 | Lifecycle race: last tab closes → `kill()`, but the entry is deleted again in the async `close` handler | A tab that connects between kill and exit gets a new process whose map entry is then deleted by the old process's `close`: orphaned `pw-record`. Key entries by process instance. |
| 9 | Process death not propagated, no `error` handler | When `pw-record` exits (USB loss, PipeWire restart) clients stay connected but silent; a missing binary emits an unhandled `error` and crashes the server. Need explicit "meter unavailable" state and bounded restart/backoff. |
| 10 | No backpressure | `ws.send` ignores `bufferedAmount`; slow clients grow memory (same class as [backlog #8](web_open_reliability-backlog.md#8-unbounded-per-client-sse-queues-m)). Drop frames, keep only the latest. |

## Architecture-conformant sketch

Owner mapping follows the existing table: actual audio observation belongs to
the **playback agent** (audio user on the Pi); the web is a typed, allowlisted
relay; the browser keeps same-origin access.

```text
Browser (status/playback page, only while visible)
  | same-origin  GET /api/playback/outputs/{id}/levels  (separate SSE, ~10 Hz)
  v
Russound web app  -- one upstream subscription per output, ref-counted
  | Caddy TLS 8443 + bearer token
  v
Playback agent  GET /v1/outputs/{id}/levels  (stream, ~10 Hz)
  |-- LevelMeter per output (lazy, ref-counted, idle timeout)
  |     pw-record --target sink.<id> -P '{ stream.capture.sink = true }'
  |               --rate 16000 --channels <n> --format s16 -
  v
PipeWire sink.<id> monitor
```

Design points:

- **Demand-driven and pooled at both layers:** the agent runs at most one
  capture per output while someone is subscribed, the web keeps at most one
  upstream per output regardless of tab count. Stop after a short grace period
  (e.g. 5 s) when the last viewer leaves; never run when nobody watches.
- **Allowlist only:** the output ID must exist in the agent inventory; the
  target node name is derived server-side (`sink.<id>`), never taken from the
  request.
- **Frame format:** `{"output_id", "seq", "at", "channels": [{"peak_dbfs",
  "rms_dbfs"}], "available": true|false, "error": null|code}`; silence as
  `-inf`/`null`, not 0 %. Include `available: false` when the stream is
  stopped/paused or capture failed, consistent with the "never look healthy
  when unknown" rule of the read API.
- **Keep it out of the existing state SSE:** `/api/events` carries full view
  payloads and is limited by waitress worker threads. Use a separate, bounded
  meter stream (own connection limit, finite lifetime, drop-old-frames
  queue), or reuse the existing SSE limits explicitly. No meter work under the
  Russound `state_lock`.
- **CPU on the Pi:** a low capture rate (8–16 kHz) is enough for a meter.
  Python 3.13 removed `audioop`; use `array('h')`/`memoryview` math or check
  whether a dependency is acceptable. Measure CPU with three outputs active.
- **Hardening:** the agent unit already sets `XDG_RUNTIME_DIR` and
  `NoNewPrivileges`; confirm `pw-record` can connect from the service context
  and that the capture is not affected by the playback egress filter (it is
  local only).
- **Frontend:** small per-channel bar with peak hold on the stream card;
  open the stream with `EventSource` only while the card/page is visible and
  close on `visibilitychange`/unload.

## Open questions before implementation

1. Where to show it: dashboard stream cards, playback page, or both?
2. Meter only, or also a coarse spectrum later? (Spectrum needs FFT; keep out
   of scope for a first version.)
3. Acceptable extra load on the Pi and LAN at ~10 Hz × 3 outputs.
4. Separate meter stream vs. extending the existing SSE with its own limits.

## Verification ideas

- Unit tests: RMS/peak to dBFS conversion (silence, full scale, clipping),
  frame aggregation rate, allowlist rejection, ref-counting with the
  kill/reconnect race from issue 8, capture process exit → `available: false`.
- On the Pi: capture `sink.stereo_front` monitor while playing, compare levels
  with [`streamlevels.py`](../../mpdrussoundpi/russound/streamlevels.py)
  measurements; confirm the supervisor/route check ignores the capture stream;
  confirm no `pw-record` processes remain after all tabs close.
