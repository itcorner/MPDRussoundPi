# Python MPD Backend API

[MPDBackend](../mpdrussoundpi/russound/lib/mpdbackend.py) is a reusable Python
API for configured local MPD instances. It runs no HTTP server and manages
playback, not daemon services. The CLI now delegates to this backend. Both
interfaces use the same persisted intentions, route/gain helpers and lock.

For planned remote web access, see the
[MPD web integration architecture](./mpd-web-integration-architecture.md).
It proposes a Pi-local agent around this backend. Web stream volume directly
uses the existing `set_gain` calibration operation; metadata, runtime source
selection and hardware-driven policy require extensions and are not part of
the current API below.

The [S01 read API contract](./mpd-playback-read-api.md) specifies the remote
observation interface and fixtures. S02 implements the local collector below;
HTTP endpoints, agent service and web integration are not implemented yet.
Existing `status()` behavior remains unchanged.

## Read-only detailed observations (S02)

[playbackstatus.py](../mpdrussoundpi/russound/lib/playbackstatus.py) provides
`PlaybackCollector`, immutable `OutputObservation`/`Probe` results and MPD
metadata normalization. It uses the backend's new `configuration_snapshot()`
and independent `observation_connection()` helpers; it does not write config,
intentions, queues, gain, routes, services or audio profiles.

**Prerequisite:** each config stream must have persisted `id` and printable
`label`, as defined by the read contract. Legacy configs remain accepted by
`MPDBackend`/CLI/supervisor but are rejected by this collector. S04 assigns IDs
in an approved migration; do not edit the running Pi just to try this example.
Use a separate migrated fixture configuration for local testing.

```python
import time
from lib.mpdbackend import MPDBackend
from lib.playbackstatus import PlaybackCollector

backend = MPDBackend("migrated-test-config.json")
collector = PlaybackCollector(backend)
try:
    for job in collector.refresh():
        job.result(timeout=30)   # Worker wait for a local tool, never an HTTP GET.
    for job in collector.refresh(audio=True):
        job.result(timeout=30)
    outputs = [item.to_dict(time.monotonic()) for item in collector.snapshot()]
finally:
    collector.close()
```

- `refresh()` submits playback reads without waiting; `audio=True` also submits
  independent readiness/route/mixer reads. At most one in-flight job of each
  kind exists per output; repeated refreshes do not grow an offline backlog.
- Separate per-output workers publish completed probes immediately: slow/dead
  MPDs and slow audio commands do not block healthy playback snapshots.
  `snapshot()` returns an ID-sorted tuple without doing I/O.
- Results include state, song ID, queue size, elapsed/duration, bitrate/format,
  optional station/title/artist/album tags and sanitized MPD error presence.
  Two consistency attempts compare song ID, playlist revision and playback
  state before/after metadata reads. Unsettled/malformed snapshots fail explicitly.
- Missing tags stay null; configured station labels do not become invented
  now-playing tags. Repeated tags normalize to a bounded display string.
  URLs/URI credentials in displayed text are redacted; MPD's `file` field,
  local paths, ports and transient audio client IDs are never serialized.
  Raw MPD error text is replaced by a generic playback-error message.
- Playback connections use three-second per-operation timeouts; `pactl`
  commands use two-second timeouts. A consistency retry can span multiple
  timeouts: three seconds is not a whole-output latency guarantee.
  Disconnect is attempted even on failure; disconnect failures cannot report
  a successful observation. Read-path failure logs omit raw endpoint errors.
- Readiness uses the captured config without profile enforcement. Route is
  inspected only for freshly observed playing outputs; known paused/stopped
  outputs are not applicable, unknown playback stays pending. Wrong routes
  are observations, never repaired. Stereo mixer values are measured separately
  from persisted calibration and may differ on inactive outputs.
- Probe ages use monotonic time; playback becomes stale at 10 seconds and audio
  probes at 30 seconds. Failures expose null current value plus timestamped
  last-known data when available; recovery clears the old fallback.
- The collector captures one validated inventory/config hash for its lifetime.
  It does not watch changes or schedule recurring polls. S03 must check config
  revisions in its background scheduler and replace collectors on changes;
  neither cached GET reads nor this constructor performs a migration.
- `close()` stops accepting work, joins the bounded reads and propagates
  unexpected worker failures. Operational errors are explicit probes; programming
  errors are not converted into successful/default snapshots.

Validation: [collector tests](../tests/test_playbackstatus.py) consume the
[S01 fixtures](../tests/fixtures/playback-read-v1.json) and cover serialization,
race retries, null tags, errors/cleanup, monotonic freshness, identity validation,
read-only calls and blocked-output/audio isolation. Local mocked acceptance
only; no agent, Pi deployment or live latency acceptance is claimed.

## Usage

Run as the audio-owning user with the same Python environment as the controller
(`python-mpd2` required). From `mpdrussoundpi/russound/`, or with that directory
on your application's Python import path:

```python
from lib.mpdbackend import BackendError, MPDBackend

from pathlib import Path

backend = MPDBackend(Path.home() / ".config/mpdrussoundpi/config.json")
streams = backend.list_streams()
states = backend.status()                  # All configured instances
backend.start("Radio 88.6")
backend.pause("Radio 88.6")
backend.start("Radio 88.6")                 # Resume configured stream
backend.set_gain("Radio 88.6", -6.0)        # Apply and persist attenuation
gain = backend.get_gain("Radio 88.6")       # Persisted gain, not mixer reading
route = backend.check_route("Radio 88.6")
if not route.correct:
    backend.recover_route("Radio 88.6")
backend.stop("Radio 88.6")
```

## Methods and results

| Method | Behavior |
|---|---|
| `list_streams()` | Tuple of immutable `StreamSettings`: name, URL, port, sink name, configured gain in dB. |
| `status(name=None)` | Tuple of immutable `PlaybackStatus`: name, MPD state (`play`/`pause`/`stop`), persisted `gain_db`, expected sink. Does not claim actual route or measured mixer gain. |
| `start(name=None)` | Persist playing intention, wait for configured audio readiness, play/resume configured URL, recover/verify routing, apply gain and verify playback. New/resumed playback starts at software mixer volume zero, then returns to 100 after gain verification. |
| `pause(name=None)` | Explicit pause, not a toggle; verify paused state. A stopped instance cannot be reported as successfully paused. |
| `stop(name=None)` | Stop playback and verify state without clearing the queue. |
| `get_gain(name)` | Read persisted configured gain. |
| `set_gain(name, gain_db)` | Persist per-stream attenuation. When playing, recover/verify route and apply/read-verify gain before saving. When stopped or paused, save without starting audio; next `start` applies it. |
| `check_route(name)` | Return `StreamRoute` including actual/expected sink, current client ID and `correct`. Missing/ambiguous clients raise an error. |
| `recover_route(name=None)` | Return tuple of verified `StreamRoute` objects; move only incorrectly routed matching MPD clients. |
| `initialize_intentions()` | Initialize only unknown streams as playing; preserve previously saved choices. |
| `reconcile(name)` | Restore saved intention after daemon/audio loss; never infer intention from observed MPD state. |

Names are exact and case-sensitive. `None` selects all where supported;
gain changes require one named stream. Only existing exclusive PulseAudio
virtual sinks and finite nonpositive gains are supported. Configuration is
reloaded on each operation, so subsequent starts and CLI calls see persisted
changes. It is validated before contacting MPD.

## Reliability and persistence

- All mutations share the CLI's per-user nonblocking kernel lock; set
  `XDG_RUNTIME_DIR` to the current user's private mode-0700 runtime directory.
  Concurrent CLI/API mutations fail rather than overlap. Read-only methods
  are snapshots and can observe concurrent changes.
- Starts reuse a queued entry matching the configured URL. An already-playing
  matching entry is not restarted. Missing URLs are appended using `addid`;
  unrelated entries and pre-existing duplicates are not deleted. Stop/pause
  never change queues. CLI and supervisor share this idempotence.
- MPD operations have a five-second timeout and guaranteed disconnect attempts.
  Route/gain helpers retain bounded checks and absolute, non-accumulating gain.
  Route/gain failure during start requests stop on the affected instance.
  Cleanup/stop failure is reported explicitly.
- Gain updates preserve unrelated JSON fields and file permissions. A
  temporary file in the same directory is flushed/fsynced and atomically
  replaces the configuration. The directory must be writable. Saving failure
  attempts restoration of the prior configured live gain; failed rollback is
  reported with the persistence failure. Saving does not regenerate audio or
  systemd configuration. This does not synchronize noncooperating manual file
  edits or guarantee durability against every power-loss scenario.
- Ansible seeds `~/.config/mpdrussoundpi/config.json` once and does not
  overwrite it on redeployment. Use that same path for API, CLI and measurement
  tools. Repository configuration is a deployment seed, not ongoing runtime
  configuration. Structural changes must be deliberately merged into the
  operational file before regenerating units/audio; gain-only changes need no
  service restart. This separation preserves calibration through git updates.
- Playback intention lives in
  `${XDG_STATE_HOME:-~/.local/state}/mpdrussoundpi/playback.json`, independently
  of the runtime directory and MPD queues. An optional second constructor
  argument selects a different state file. Start/pause/stop save the requested
  intention before contacting MPD. On failure the caller gets an error, but
  the supervisor can fulfill that intention later. Corrupt state fails
  explicitly; it is never silently replaced with playing defaults.
- `ValueError` denotes invalid input/configuration. `BackendError` denotes
  operational failure, including lock contention; methods never deliberately
  terminate the caller with `SystemExit`. Successful mutations return `None`
  unless listed otherwise. No errors are converted into successful empty results.
- All-stream operations execute sequentially and fail on the first error.
  Earlier successful instances are not rolled back. Paused/stopped gain
  persistence does not promise the inactive sink already reflects that gain.
- This is synchronous; callers should use a worker in an event loop.
  The separate supervisor reconciles every two seconds, with per-stream
  exponential failure backoff capped at 30 seconds. It starts missing daemon
  services, restores the configured URL, clears output errors, recovers
  routing and reapplies absolute gains. There is no continuous loudness
  normalization. Pause/stop choices survive reboot; a paused stream with a
  lost/empty queue remains silently stopped until explicitly started.
- Generated outputs use `mixer_type "software"`. This makes startup gating
  independent of the inactive Pulse sink mixer. Existing matching playback
  is not interrupted. External raw `mpc` play/stop/volume commands do not
  update intentions and may be undone by reconciliation; use the API/CLI.

## Validation

[Backend tests](../tests/test_mpdbackend.py) cover repeated starts, URL reuse,
status, pause/stop, route-before-gain, configuration validation, lock contention,
protocol/cleanup failure, atomic persistence, rollback failures and deferred
gain application. MPD and mixer effects are mocked; shared helpers have their
own route/gain tests and earlier target validation.

See [headless deployment](./mpd-headless-deployment.md) for lifecycle setup,
validation commands and target verification limits.
