# Python MPD Backend API

[MPDBackend](../mpdrussoundpi/russound/lib/mpdbackend.py) is a reusable Python
API for configured local MPD instances. It runs no HTTP server and manages
playback, not daemon services. The CLI now delegates to this backend. Both
interfaces use the same persisted intentions, route/gain helpers and lock.

For deployed remote web access, see the
[MPD web integration architecture](./mpd-web-integration-architecture.md).
It uses a Pi-local agent around this backend. Web stream volume directly
uses the existing `set_gain` calibration operation. S08 adds manual station
selection through the durable agent operation contract; hardware-driven
policy remains a future extension.

The [S01 read API contract](./mpd-playback-read-api.md) specifies the remote
observation interface and fixtures. S02 implements the local collector and S03
the read-only HTTP adapter below. Production stream identities were migrated
on 2026-10-17, and the agent is reachable from the Docker web app through the
Pi's TLS proxy. S05 exposes cached status/metadata only; existing `status()`
behavior remains unchanged. S07 adds opt-in
[durable manual operations](./mpd-playback-operations-api.md), implemented
locally and deployed; S07 is done and production-verified by the user on
2026-10-17. S08 managed station selection, S09 TuneIn integration and the
config/state split with favorites are active in the approved trusted-LAN Pi
test deployment. Public-only DNS/redirect egress enforcement is deployed;
full lifecycle acceptance remains outstanding. See the
[deployment record](./playback-deployment-evidence.md#configuration-split-rollout-2026-10-17).

## Opt-in USB hardware recovery

An audio device may explicitly configure
`"hardware-selector": {"vendor": "0d8c", "product": "0102"}`.
This selects one USB model independent of port/runtime naming, refusing zero
or multiple matches. It requires PipeWire/WirePlumber, `analog-surround-51` and
unique stereo channel pairs. Existing configurations without the field keep
their exact-name behavior; no automatic migration occurs.

The shared readiness path checks the actual downstream PipeWire links, not
only MPD-to-virtual-sink routing. Startup/reconciliation can set the profile
only on the uniquely selected card and must re-observe readiness afterward.
Read-only collection and audit do not set profiles. Saved intentions and
`gain_db` still belong to the existing backend/supervisor; no extra recovery
daemon is introduced. See the [coordinated recovery design and validation
gates](./usb-audio-recovery-fix.md) before deploying the opt-in configuration.

## Configuration, runtime state and revisions

The backend reads two files (format details in
[the deployment guide](./mpd-headless-deployment.md#configuration-configjson-admin-owned)):

- `config.json` (admin-owned, never written by the backend): `outputmappers`,
  `outputs` (`id`, `label`, `port`, `sink`, optional `default_station` and
  `default_gain_db`) and up to 20 seed `stations`. Parsed and validated by
  [outputconfig.py](../mpdrussoundpi/russound/lib/outputconfig.py); station values by
  [stations.py](../mpdrussoundpi/russound/lib/stations.py). The old `streams`
  format is rejected.
- `playback.json` (backend-owned runtime state): favorites plus, per output ID,
  station, cached resolved URL, managed song ID, gain and intention. Handled by
  [playbackstate.py](../mpdrussoundpi/russound/lib/playbackstate.py) and written
  atomically under `mpd_control.lock`.

Revisions:

- `configuration_snapshot()[2]` (`config_revision`) is the SHA-256 of the exact
  `config.json` bytes. It changes only on admin edits.
- `control_revision()` hashes `playback.json` plus its inode and modification
  time, so every station, gain, intention or favorites write invalidates it.
- Durable operations carry both revisions and are rechecked **under the lock,
  before contacting MPD or audio**; stale requests raise `RevisionConflict`.
  A busy lock is an explicit failure, not permission to write without locking.

Output IDs match `[a-z][a-z0-9_-]{0,63}` and name the MPD units
(`mpd_<id>.service`). Labels are display-only and edited in `config.json` by
the admin. The CLI selects outputs by ID:

```sh
python mpdctrl.py --config-revision ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --set-gain -6.1 --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
```

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
  `output_ids=(...)` restricts submissions to known IDs; `playback=False`
  permits audio-only work without bypassing playback retry backoff.
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
only; S03's agent acceptance is separate below. No Pi deployment or live
latency acceptance is claimed.

## Read-only HTTP agent (S03)

[playbackagent.py](../mpdrussoundpi/russound/playbackagent.py) runs Waitress;
[lib/playbackagent.py](../mpdrussoundpi/russound/lib/playbackagent.py) implements
the Flask adapter, immutable cache and background scheduler. It does not
initialize intentions, start services, reconcile playback or write configuration.

Install [agent dependencies](../mpdrussoundpi/russound/requirements-agent.txt)
in the audio user's environment, separately from web dependencies:

```sh
python -m pip install -r mpdrussoundpi/russound/requirements-agent.txt
```

Create a private service-token file outside the checkout (mode 0600, private
parent directory), containing a cryptographically random token of at least
32 ASCII characters without whitespace. Never pass credentials on the command
line or commit them. The token is read once on startup; rotation requires restart.
For local testing, use a separate migrated config with persisted output IDs/labels:

```sh
python mpdrussoundpi/russound/playbackagent.py migrated-test-config.json \
  --agent-id musicpi --token-file /private/path/playback-agent-token
```

- Default bind is `127.0.0.1:8081`. All routes require
  `Authorization: Bearer <token>`, including health, unknown routes and HEAD.
  This credential covers read access only; no write routes exist.
- The selected D06 deployment keeps the agent bound to loopback and terminates
  TLS at Caddy on the Pi. Caddy must forward the bearer credential unchanged,
  expose only the read API, and be restricted by firewall to the Docker web
  host. The web client trusts the self-signed certificate with its
  `tls_ca_file` setting while retaining hostname validation. Do not use
  `--allow-remote` to bypass this boundary or expose bearer credentials over
  unprotected LAN HTTP. Proxy provisioning remains required for S06.
- GET/HEAD `/v1/health`, `/v1/capabilities`, `/v1/outputs` and
  `/v1/outputs/{id}` follow the read contract. Responses are JSON/no-store;
  writes/OPTIONS return JSON 405, unauthorized access 401, malformed requests
  400. Query parameters and GET bodies are not supported.
- HTTP reads serialize one immutable publication; they never read config,
  connect to MPD, run audio commands or wait for worker retirement.
  Cold valid inventory contains pending probes; unknown inventory returns 503.
  Liveness/capabilities remain available with missing/invalid config or dead MPDs.
- Playback polls normally follow completed observations at approximately
  two-second intervals. Each failed output retries after 2, 4, 8, 16, then
  at most 30 seconds; a successful observation resets its delay. Other outputs
  continue independently. Audio probes are scheduled every 30 seconds in
  their separate pool and never trigger playback outside its schedule.
- Background config validation normally runs every two seconds; invalid config
  retries with the same bounded backoff. Changes replace the collector/cache
  using its validated exact-byte revision. Invalid updates invalidate inventory
  with 503, not old success. Old bounded workers are joined outside cache locks;
  GETs remain available while they retire. Replacement probes start afterward.
- Publication revisions advance on changed immutable observations/config/errors;
  monotonic snapshot age becomes stale at 10 seconds. Individual probes retain
  their own timestamps/staleness; fresh audio does not make failed playback healthy.
- Transition logs contain IDs/status codes, not URLs, metadata, token values or
  raw config errors. Unexpected scheduler failure invalidates the snapshot,
  logs an error and makes shutdown fail explicitly.
- SIGINT/SIGTERM close the HTTP listener/Waitress workers and join bounded
  observer reads. The standalone entry point does not generate systemd units.
  S06's separate [deployment preparation](./playback-agent-deployment.md)
  provides an independent user unit, tested with a separate Pi config copy. Run as the
  existing audio user when eventually approved, never as a second playback supervisor.

Local tests exercise real loopback Waitress and standalone CLI liveness/clean
SIGTERM with missing config, plus fake observations for HTTP/config/retry behavior.
They do not certify real-Pi scheduling latency or provision production transport.

## Usage

Run as the audio-owning user with the same Python environment as the controller
(`python-mpd2` required). From `mpdrussoundpi/russound/`, or with that directory
on your application's Python import path:

```python
from lib.mpdbackend import BackendError, MPDBackend

from pathlib import Path

backend = MPDBackend(Path.home() / ".config/mpdrussoundpi/config.json")
outputs = backend.list_outputs()
states = backend.status()                     # All configured instances
backend.start("stereo_front")
backend.pause("stereo_front")
backend.start("stereo_front")                 # Resume the selected station
backend.set_gain("stereo_front", -6.0)        # Apply and persist attenuation
gain = backend.get_gain("stereo_front")       # Persisted gain, not mixer reading
route = backend.check_route("stereo_front")
if not route.correct:
    backend.recover_route("stereo_front")
backend.stop("stereo_front")
```

## Methods and results

| Method | Behavior |
|---|---|
| `list_outputs()` | Tuple of immutable `OutputSettings`: ID, label, port, sink, saved gain, selected station (or `None`), cached resolved URL, managed song ID and intention. |
| `status(id=None)` | Tuple of immutable `PlaybackStatus`: ID, MPD state (`play`/`pause`/`stop`), persisted `gain_db`, expected sink. Does not claim actual route or measured mixer gain. |
| `start(id=None)` | Persist playing intention, wait for configured audio readiness, play/resume the selected station (resolving TuneIn stations when the cached URL is not playing), recover/verify routing, apply gain and verify playback. New/resumed playback starts at software mixer volume zero, then returns to 100 after gain verification. Raises `NoStationError` for an output without a station; `start()` skips such outputs. |
| `pause(id=None)` | Explicit pause, not a toggle; verify paused state. A stopped instance cannot be reported as successfully paused. |
| `stop(id=None)` | Stop playback and verify state without clearing the queue. |
| `get_gain(id)` | Read persisted gain. |
| `set_gain(id, gain_db)` | Persist per-output attenuation in `playback.json`. When playing, recover/verify route and apply/read-verify gain before saving. When stopped or paused, save without starting audio; next `start` applies it. |
| Durable `set_tunein_station` / `select_favorite` operations | Select a TuneIn station or a favorite (URL or TuneIn), save station, resolved URL and play intention in one atomic write, and replace the output's one-item managed queue under the shared lock. See [station-selection semantics](./mpd-playback-operations-api.md#s08-source-selection-semantics). |
| `favorites()` / `favorites_operation(action, ...)` | Read the favorites list with both revisions; add (TuneIn only, duplicate is a no-op, at most 20), remove or move a favorite. |
| `check_route(id)` | Return `StreamRoute` including actual/expected sink, current client ID and `correct`. Missing/ambiguous clients raise an error. |
| `recover_route(id=None)` | Return tuple of verified `StreamRoute` objects; move only incorrectly routed matching MPD clients. |
| `initialize_intentions()` | Persist `config.json` defaults for outputs without runtime state; preserve previously saved choices. |
| `reconcile(id)` | Restore saved intention after daemon/audio loss; never infer intention from observed MPD state. |

IDs are exact. `None` selects all where supported;
gain changes require one output ID. Only existing exclusive PulseAudio
virtual sinks and finite nonpositive gains are supported. Configuration is
reloaded on each operation, so subsequent starts and CLI calls see persisted
changes. It is validated before contacting MPD.

## Reliability and persistence

- All mutations share the CLI's per-user nonblocking kernel lock; set
  `XDG_RUNTIME_DIR` to the current user's private mode-0700 runtime directory.
  Concurrent CLI/API mutations fail rather than overlap. Read-only methods
  are snapshots and can observe concurrent changes.
- Every output owns exactly one managed queue entry. Starts reuse it when it
  matches the selected station's URL; an already-playing matching entry is not
  restarted. Startup verifies that the queue contains only the tracked song ID.
  It never clears unowned entries; source replacement is limited to one
  identified song. Stop/pause never change queues. CLI and supervisor share
  this idempotence.
- MPD operations have a five-second timeout and guaranteed disconnect attempts.
  Route/gain helpers retain bounded checks and absolute, non-accumulating gain.
  Route/gain failure during start requests stop on the affected instance.
  Cleanup/stop failure is reported explicitly.
- Runtime writes (station, gain, intention, favorites) go only to
  `playback.json`, preserving its file permissions. A temporary file in the same
  directory is flushed/fsynced and atomically replaces it. `config.json` is
  never written. Saving failure
  attempts restoration of the prior configured live gain; failed rollback is
  reported with the persistence failure. Saving does not regenerate audio or
  systemd configuration. This does not synchronize noncooperating manual file
  edits or guarantee durability against every power-loss scenario.
- Ansible seeds `~/.config/mpdrussoundpi/config.json` once and does not
  overwrite it on redeployment. Use that same path for API, CLI and measurement
  tools. Repository configuration is a deployment seed, not ongoing runtime
  configuration. Structural changes must be deliberately merged into the
  operational file before regenerating units/audio. Gains and station choices
  live in `playback.json`, so calibration survives git updates.
- Runtime state (stations, gains, intentions, favorites) lives in
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
- All-output operations execute sequentially and fail on the first error.
  Earlier successful instances are not rolled back. Paused/stopped gain
  persistence does not promise the inactive sink already reflects that gain.
- This is synchronous; callers should use a worker in an event loop.
  The separate supervisor reconciles every two seconds, with per-output
  exponential failure backoff capped at 30 seconds. It starts missing daemon
  services, restores the selected station, clears output errors, recovers
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
