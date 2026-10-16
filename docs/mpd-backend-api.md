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
observation interface and fixtures. S02 implements the local collector and S03
the read-only HTTP adapter below. A loopback Pi test deployment uses a separate
config copy; production migration/transport and web integration remain pending.
Existing `status()` behavior remains unchanged.

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

## Offline identity and control preparation (S04)

This preparation is local only. Do not run against the Pi's live files during
an observation run. Use private local copies; configuration and backup bundles
can contain stream credentials.

### Identity and revision contract

- Configurations without any stream `id`/`label` retain legacy behavior.
  If either field is present, every stream must have a unique ID matching
  `[a-z][a-z0-9_-]{0,63}` and a nonempty printable label. The backend and
  collector share validation in
  [playbackidentity.py](../mpdrussoundpi/russound/lib/playbackidentity.py).
- Persist the ID once. Changing the display label does not change the ID,
  legacy `name`, MPD port, sink or derived systemd unit name.
- `configuration_snapshot()[2]` is SHA-256 of the exact configuration bytes,
  the same opaque revision published by the read API. Every file edit,
  including a legacy CLI gain edit or whitespace-only edit, invalidates it.
- `set_gain(name, gain_db, expected_revision=revision)` checks the revision
  **under `mpd_control.lock`, before connecting to MPD or changing gain**.
  Stale requests raise `RevisionConflict`. Omitting the revision preserves the
  existing Python API behavior; future HTTP mutations must require it.
- `set_label(name, label, expected_revision=revision)` requires persisted IDs
  and edits only the label under that same lock, without contacting audio.
  Neither operation exposes an ID setter. A busy lock is an explicit failure,
  not permission to write without locking. Re-read before retrying a conflict.
- The guarantee covers cooperating backend writers, not arbitrary external
  file edits. Operational config replacement/Ansible/manual installs must not
  run concurrently with a live writer.

The CLI exposes these operations (examples only, not deployment instructions):

```sh
python mpdctrl.py --config-revision local-config.json
python mpdctrl.py --set-gain -6.1 --stream "FM4 Radio" \
  --expected-revision SHA256_FROM_READ local-config.json
python mpdctrl.py --set-label "Kitchen radio" --stream "FM4 Radio" \
  --expected-revision SHA256_FROM_READ local-config.json
```

Read a new revision after each successful edit. Labels are display-only;
`--stream` still uses the preserved legacy name.

### Offline bundle

Provide an explicit identity mapping for **every** existing name. Choose labels
for the installation; do not infer physical Russound input mappings from sinks:

```json
{
  "Radio 88.6": {"id": "front", "label": "Front stream"},
  "FM4 Radio": {"id": "centerlfe", "label": "Center/LFE stream"},
  "OE3 Radio": {"id": "rear", "label": "Rear stream"}
}
```

From the script directory, using copied input files:

```sh
python playbackmigrate.py --config local-config.json \
  --intentions local-playback.json --identities local-identities.json \
  --output private-s04-bundle
```

The output directory must not exist; its parent must exist. The tool creates
a mode-0700 directory with mode-0600 files, refuses ID reassignment, requires
exact name coverage and preserves:

| Bundle file | Purpose |
| --- | --- |
| `config.original.json` | Exact original config bytes for rollback |
| `playback.original.json` | Exact original intention bytes for rollback |
| `config.prepared.json` | Only adds/updates explicit IDs and labels |
| `playback.legacy.json` | Exact legacy intention bytes; current runtime format |
| `control-v1.preview.json` | Offline, manual-only future control schema |
| `manifest.json` | Bundle version, original/prepared revisions, file hashes and original permission modes |

Every generated file is flushed/fsynced before success is printed. A failed
file write cleans up only files created by that invocation. Originals are
never written. Unsupported control preview versions, corrupt/unknown states
and missing intention entries fail explicitly; migration does not invent
playing defaults. The preview is shaped as:

```json
{
  "schema_version": 1,
  "outputs": {
    "front": {"mode": "manual", "intention": "play"},
    "centerlfe": {"mode": "manual", "intention": "pause"},
    "rear": {"mode": "manual", "intention": "stop"}
  }
}
```

D04 is accepted: future manual play/pause/stop sets mode to manual until
explicit return to automatic. This preview deliberately permits manual mode
only. Automatic owner/generation/target fields must be specified with S10;
do not fabricate them while their policy decisions remain open.
**Do not install this preview as the current playback intention file.**
Current runtime readers reject it rather than treating it as missing state.

### Reader/writer inventory and coordinated rollout

| Surface | Current ownership / migration requirement |
| --- | --- |
| Backend start/pause/stop | Reads/writes legacy intentions under `mpd_control.lock` |
| Backend initialize/reconcile | Initializes missing legacy entries/reconciles them; must migrate together with any state schema |
| CLI | Delegates to backend; gain/label config edits use the same lock |
| Supervisor | Uses preserved legacy names for service units and delegates intentions to backend |
| Recorder | Reads name-to-intention JSON directly; must adopt a shared versioned reader before control-v1 activation |
| Agent/collector | Read-only configuration/metadata; no intention initialization or writes; scheduler replaces collectors when config hash changes |
| MPD/audio setup | Reads config; derives existing services/sinks from unchanged fields; no unit regeneration needed just for IDs/labels |
| Setup Ansible | Seeds operational config only if absent; invokes legacy intention initialization; coordinate before changing state schema |
| Agent Ansible | Copies helpers and explicitly selected config; separate observer copy must be deliberately reconciled with operational IDs |
| `streamlevels.py` | Writes a new, exclusively created calibration output config, not a locked live edit; installation needs maintenance |

Offline acceptance is preparation, not approval to migrate production. Later:

1. Wait for observation completion and obtain maintenance approval. Verify a
   fresh bundle against unchanged source revision/state; back up all affected
   code/config/state and permissions. Do not overwrite endurance artifacts.
2. Stop all mutation-capable entry points (supervisor, future worker/policy and
   any CLI sessions); stop the recorder before changing its state schema.
   Ensure no old writer remains, and hold the existing control lock throughout
   replacement. The lock alone cannot prevent an old process from overwriting
   new state after it is released.
3. For an **identity-only** rollout, install coordinated helpers and prepared
   config, retaining `playback.legacy.json` as runtime state. Deliberately align
   the observer config. Ports, unit names, audio fragments and gains do not
   change; do not rerun audio or MPD setup.
4. A **control-v1** rollout is not implemented here. Before enabling it, update
   every state reader/writer above, including the recorder and initializer,
   validate supported versions, and finalize the automatic control record.
   Old code must fail closed and must never run alongside new writers.
5. Verify IDs, byte-preserved intentions, gains, service lifecycles, readiness
   and collector/API health before resuming approved writers. On failure,
   keep writers stopped; restore the coordinated original code/config/state
   from named backups with manifest-recorded original modes. Verify original
   hashes, then restart the old writers. Never roll back only one side of a
   code/state-schema transition.

Local fixtures verify field preservation, exact-byte rollback material,
permissions, explicit schema rejection, stale edits and concurrent lock
exclusion. No production migration or live rollback is claimed.

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
- Non-loopback binding requires explicit `--allow-remote`. This is not TLS:
  do not expose bearer credentials over unprotected LAN HTTP. D06 still gates
  secure proxy/tunnel/TLS provisioning and S06 deployment.
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
| `set_gain(name, gain_db, *, expected_revision=None)` | Persist per-stream attenuation; optional stale-write guard under the mutation lock. When playing, recover/verify route and apply/read-verify gain before saving. When stopped or paused, save without starting audio; next `start` applies it. |
| `set_label(name, label, *, expected_revision)` | Edit only the display label of an output with persisted identity; require a matching config revision and do not change audio. |
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
