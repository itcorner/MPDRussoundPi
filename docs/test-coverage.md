# Test Coverage Review

This review covers the Python production modules in `web/` and `tool/`.

## Baseline

- The documented `python -m unittest -q` command discovers zero tests in this checkout.
- The working command is `python3 -m unittest discover -s tests -p 'test_*.py' -q`.
- No `coverage` or `pytest` package is installed, so this is a test-to-module and behavior review, not a measured line-coverage report.
- The suite contains 103 named test methods across six test modules. This review maps the 8 non-package-marker modules in scope.
- In the project virtual environment, 101 tests pass and 2 existing controller tests fail because their `DummyClient` doubles do not implement `get_zone_extended_info` during state refresh.

## Covered Areas

- Web zone model, state/controller orchestration, backend adapter, connector protocol behavior, and Flask/SSE server behavior have dedicated tests.
- `tool/dummy_backend/dummy_backend.py` has TCP integration tests for zone reads/writes, broadcasts, persistence, checksums, and keypad displays.
- Connector tests cover checksum generation, selected frame parsing, zone information, user parameter normalization, displays, audit logging, and connection-reset handling.

## Blind Spots

- `web/config_types.py` has no dedicated tests for malformed configuration, boolean-vs-integer rejection, endpoint defaults, or controller-limit normalization.
- The `web/` and `tool/` package marker files are intentionally not documented as modules because they contain no application behavior.
- Frontend JavaScript and HTML files are not covered by the Python suite.
- Ansible setup syntax is checked separately; target execution and reboot
  behavior require hardware verification.
- Audio setup has a regression test for the generated WirePlumber ALSA rule
  section, device match, and requested profile. The service restart is mocked.
- Loopback tests require input channel layout inside `capture.props` and the
  output layout inside `playback.props`, with no global `audio.position`.
  A global layout enables conversion rather than direct channel remapping.
  Target A/B metering reproduced silent rear/LFE with the old layout and
  verified individual digital channel isolation with the corrected layout.
- The configured USB device uses `analog-surround-51`, mapping three stereo
  inputs to FL/FR, FC/LFE and RL/RR on a six-channel master. A configuration
  regression verifies that all loopback targets and the WirePlumber rule use
  5.1. Target equal-tone calibration verified unity software gain on all pairs;
  analog jack placement and levels require physical measurement.
- Audio setup locking tests cover contention, inode preservation, exception
  cleanup, invalid runtime directories, and kernel lock release after SIGKILL.
  Installation requires a current-user-owned mode-0700 `XDG_RUNTIME_DIR`.
  The lock file stays in that directory; its existence does not indicate an
  active lock, and it must not be deleted while setup may be running.
- Several web tests exercise private helpers or mocks rather than full HTTP request flows for every route.
- Hardware-dependent behavior, systemd/PulseAudio/PipeWire commands, and real MPD connections are not tested in this environment.
- The opt-in [USB recovery implementation](./usb-audio-recovery-fix.md#step-4-coordinated-opt-in-implementation)
  adds model-selection, shared-readiness and downstream graph tests in
  [test_audioidentity.py](../tests/test_audioidentity.py), plus standalone Lua
  [decision-core](../tests/usb-target-policy-test.lua) and
  [hook-adapter](../tests/usb-target-hook-test.lua) harnesses. The hook harness
  mocks WirePlumber APIs. The separate
  [runtime harness](../tests/usb-target-runtime.sh) exercises an actual isolated
  PipeWire/WirePlumber graph with synthetic exported devices, not production
  ALSA/USB hotplug. Setup tests cover generated policy ownership, guarded
  loopbacks, legacy reinstall and ambiguity preflight; backend/collector tests
  cover retained intentions/gain and read-only readiness error reporting.
  Approved Oct 16 maintenance and five short reconnects, a 149-second absence,
  port relocation, a stable-port mixed-intention retry and real duplicate-card
  refusal/removal recovery subsequently passed on the target. The first
  mixed-intention attempt was contaminated by extra physical adjustments and
  descriptor failures. USB bandwidth limits remain unresolved; see the
  [acceptance qualifications](./usb-audio-recovery-fix.md#final-checkpoint-after-guided-tests).

## Configuration split (config.json / playback.json)

[Config tests](../tests/test_outputconfig.py) cover the `outputs`/`stations`
schema, rejection of the old `streams` format, unknown fields, duplicate
IDs/ports/sinks, unknown `default_station` keys, URL and TuneIn station
validation and the 20-station limit. [State tests](../tests/test_playbackstate.py)
cover defaults (optional `default_station`/`default_gain_db`), stationless
outputs, favorites seeding, add/remove/move, duplicate no-ops, the cap and
atomic writes. Backend, agent, operations, status, CLI, supervisor, setup,
audit and loudness tests verify that `config.json` stays byte-identical, that
outputs without a station cannot play, URL/TuneIn favorite selection, favorites
HTTP endpoints and capability opt-out, URL privacy in favorites responses and
probe preservation across runtime station/gain changes.
[playbackfixtures.py](../tests/playbackfixtures.py) provides the small shared
configuration. [Web command tests](../tests/test_playback_commands.py) cover
favorites polling/validation (no URL field accepted), the favorites proxy route,
CSRF rejection and `output_id: null` checks; [dashboard tests](../tests/test_playback_controls.js)
cover the favorites list, current marker, Play/↑/↓/✕, ☆/★ toggles including the
20-entry cap, and Play disabled ("Select a station first") without a station.

## S04 offline preparation (2026-10-16)

Migration/revision tests (removed with the tool by the [configuration split](./config-split-plan.md)) covered exact
original-byte rollback material, preservation of all nonidentity fields,
private bundle permissions, unchanged input files, exclusive destination
creation and failed-write cleanup. They reject duplicate/partial mappings,
ID reassignment, corrupt/missing/unknown intentions and unsupported control
preview versions. Current runtime readers reject the preview rather than
initializing playing defaults. Backend tests verify stale gain edits fail
before audio effects, legacy edits invalidate revisions, label edits preserve
IDs/operational fields and concurrent gain edits respect the existing lock.
[CLI tests](../tests/test_mpdctrl.py) cover revision preconditions and argument
requirements. The offline CLI test verifies preparation does not create an
MPD client.

The coordinated local selection passes **208 tests**, including the existing
194 playback/USB/agent regressions and 14 added S04 tests. This is not measured
line coverage or live production-migration acceptance. Pylance reports no
diagnostics in the changed production Python files. No Pi deployment, live
control-state replacement or production rollback was performed for S04.

## Test Links

- [Zone tests](../tests/test_zone.py)
- [Backend tests](../tests/test_russound_backend.py)
- [Connector tests](../tests/test_russound_connector.py)
- [Controller tests](../tests/test_russound_controller.py)
- [Server tests](../tests/test_russound_server.py)
- [Dummy backend integration tests](../tests/test_dummy_backend.py)
- [Audio setup tests](../tests/test_audiosetup.py)
- [MPD setup tests](../tests/test_mpdsetup.py) verify explicit meta-service
  dependencies and ordering, including an empty instance list. Installation
  reloads the user systemd manager after writing units; enabling the meta-service
  pulls in the configured instances on its next activation. Reloading unit
  definitions alone does not start previously inactive instances.
- MPD setup and audio setup share the kernel-lock implementation in
  [setuplock.py](../mpdrussoundpi/russound/lib/setuplock.py), with separate
  per-user filenames. MPD installation holds `mpd_setup.lock` through
  daemon reload; cleanup acquires the same lock before stopping services or
  removing files. Tests cover contention, audio/MPD lock independence,
  invalid configuration, reload failure, cleanup exclusion and normal exit.
  The shared lock's exception, interrupt and SIGKILL behavior is covered by
  the audio setup tests. Both require a private mode-0700 `XDG_RUNTIME_DIR`;
  persistent lock files must not be deleted while setup may be running.
- [MPD controller tests](../tests/test_mpdctrl.py) cover shared-backend
  delegation, selected/all actions, pause, read-only status/routes,
  unavailable/wrong routes, unknown names and truthful exit status.
  [Backend tests](../tests/test_mpdbackend.py) cover locking, protocol behavior,
  queue idempotence, persistence, saved intentions and startup gating.
- [Readiness tests](../tests/test_audioready.py) cover missing hardware/sinks,
  profile enforcement with mandatory re-verification and bounded retries.
  [Supervisor tests](../tests/test_mpdsupervisor.py) cover independent
  recovery, service failures, initialization and per-stream backoff.
- [Audio cleanup tests](../tests/test_audiosetup.py) cover CLI dispatch,
  install/cleanup/reinstall, ownership preflight, symlink refusal, byte-preserved
  PulseAudio settings, idempotent includes/cleanup, safe wrapper removal,
  legacy/malformed configuration refusal, backend selection, partial installs,
  locking and checked restart failures. Audio service commands are mocked;
  this is not a live production teardown test.
- [Endurance recorder tests](../tests/test_playbackaudit.py) cover read-only
  protocol calls, playback/route/gain/silence anomalies, bounded observations,
  failure isolation, cursor handling, persistent deadline/counters and
  independent service generation. See [test procedure](./mpd-playback-endurance-test.md).
- [Playback observation tests](../tests/test_playbackstatus.py) cover S02's
  immutable ID-sorted snapshots, detailed metadata and bounded race retry,
  failed/last-known probes, monotonic stale thresholds, sanitized errors,
  captured config revision, read-only audio/MPD calls and independent healthy
  publication while another MPD/audio read is blocked. They consume the
  [S01 contract fixtures](../tests/fixtures/playback-read-v1.json). Local
  focused observation/backend/readiness/CLI/supervisor/audit/measurement run:
  100 tests, 99 passed and one existing real-FFmpeg skip (2026-10-14).
  HTTP agent, browser integration and real-Pi latency are not tested by S02.
- [Read-only agent tests](../tests/test_playbackagent.py) cover S03 bearer
  authentication on every route/method, cached inventory/individual reads,
  HEAD/JSON/no-store, missing/invalid configuration, cold/stale/partial
  observations, epoch renewal, bounded independent retries, config replacement
  and reads during blocked worker retirement. Real loopback Waitress and CLI
  tests verify responsive liveness without config and clean SIGTERM shutdown.
  Selected/audio-only collector submissions have a related isolation test.
  A real-collector integration test verifies MPD/audio failures remain cached
  diagnostics without config/state writes.
  Targeted agent/collector/backend/readiness/CLI/supervisor run: 94 passed
  (2026-10-15). No Pi deployment, production transport, browser integration or
  real-LAN latency claim is made.
- [Agent deployment tests](../tests/test_playback_agent_deployment.py) perform
  static YAML/template validation only: loopback user unit, opt-in activation,
  isolated sources/venv, private credentials, no audio/MPD service operations
  and limited rollback. They execute no deployment or target commands.
  Six static tests and both Ansible syntax checks passed (2026-10-15).
- [Isolated target acceptance](./playback-deployment-evidence.md#approved-isolated-target-test-2026-10-15)
  verifies real Pi cached reads, metadata, routes/gain and HTTP authentication
  with a separate private config copy. Production config/intentions hashes and
  existing audio/MPD/supervisor/recorder PIDs/restarts were unchanged; recorder
  sampling advanced. Existing Radio 88.6 (now Stereo Front) faults continued. No fault injection,
  playback repair, Docker transport or p95 update-latency acceptance was performed.

## Deployment workflows

[Workflow tests](../tests/test_deployment_workflows.py) check non-forcing Git
updates, external-gateway mode, writable mounts/private secret directories,
playback agent environment rendering, native persistent state and isolated
dependencies, and staging audio without starting/enabling playback.
All deployment playbooks are separately syntax-checked. These are controller
checks, not execution of the native/Docker provisioning paths on a new Pi.

## Public-only playback egress

[Public network tests](../tests/test_publicnetwork.py) cover IPv4/IPv6 public
classification, mixed/empty/private DNS answers, rebinding between validation
and connection, pinned socket addresses, original HTTP Host/TLS hostname,
certificate validation, redirect revalidation and disabled environment proxies.
[Filter tests](../tests/test_playbackegress.py) cover cgroup-only scoping,
control replies, exact UDP DNS exceptions, shared address sets, input rejection,
fail-closed startup, cgroup identity changes and persistent unit configuration.
[TuneIn tests](../tests/test_tunein.py) retain search/cache and bounded
playlist/error coverage with deterministic DNS.

Local baseline: 551 Python tests, one skipped; 27 JavaScript tests passed.
The [egress rollout evidence](./playback-egress-hardening-plan.md#rollout-evidence-2026-10-17)
separately records real Pi public playback and direct/DNS/redirect blocking,
including an isolated MPD and zero private fixture requests. Live IPv6 transport,
user-manager restart and whole-Pi reboot were not tested.

## Manual MPD Stream Control

A reusable [Python MPD backend API](./mpd-backend-api.md) also provides
playback/status/route control and persistent per-output gain changes without
an HTTP server. [Backend tests](../tests/test_mpdbackend.py) cover queue
idempotence, protocol errors, locking, gain persistence and rollback.

From `mpdrussoundpi/russound/`, with MPD services already running:

```sh
python mpdctrl.py --start --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --stop --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --pause --output stereo_front ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --status --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --start ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --stop ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --status ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --apply-gain ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --check-route ~/.config/mpdrussoundpi/config.json
python mpdctrl.py --recover-route --output stereo_front ~/.config/mpdrussoundpi/config.json
```

IDs are exact configured output IDs. Omitting `--output`
selects all outputs (`--start` skips outputs without a station). Exactly one action is required; invalid/ambiguous names
fail before any MPD action. Start/stop hold `mpd_control.lock` in the private
user runtime directory; status does not require a lock.

Automatic Russound power polling and controller service management options
(`--auto`, `--install`, `--enable`, `--disable`, `--cleanup`) have been removed.
The obsolete system-info helper and its `system_on` JSON output have also
been removed; no remaining repository consumers were found.
Provisioning retires that controller and installs a separate intended-playback
supervisor; first boot starts all configured streams. See
[headless deployment](./mpd-headless-deployment.md).
Existing installations should retire the old service before deploying the
manual-only controller, running as the owning user:

```sh
systemctl --user disable --now auto-mpd-control.service
```

Remove the obsolete user unit after backing it up, then run
`systemctl --user daemon-reload`. This is separate from MPD instance service
management, which remains in `mpdsetup.py`. The new controller starts/stops
playback, not daemon services. It now delegates to the shared backend with
idempotent queue handling, explicit protocol failures and saved intention.

## Explicit route verification and recovery

The controller uses [streamroute.py](../mpdrussoundpi/russound/lib/streamroute.py)
to inspect actual PulseAudio/PipeWire sink-input routing, rather than trusting
the requested `target.object` property. `--check-route` is read-only, requires
no runtime lock and checks every selected stream. It prints the stream,
actual sink and expected sink, exiting 1 for wrong routes, missing sinks,
missing playback clients or ambiguous identities. Stopped streams have no
verifiable active playback route and are reported as failures, not silently
skipped. Start them separately if desired.

`--recover-route` uses `mpd_control.lock` and moves only incorrectly routed
MPD clients to their configured virtual sinks. Both commands accept
`--output ID`; omit it for all outputs. Recovery does not start/stop MPD,
change queues, alter gains, restart services or modify audio configuration.
Already-correct routes are left alone. A failed recovery exits 1; previously
recovered streams remain corrected.

The configured output name `pw_<sink>` must match the live MPD client's
`media.name`, `application.name=Music Player Daemon` and
`application.process.binary=mpd`. Zero matches fail explicitly; multiple
matches refuse recovery. Configured sink names must be unique across streams.
The helper discovers current sink-input IDs on every inspection, including
after client recreation, instead of storing transient IDs such as `253`.
Recovery retries missing clients/sinks for five seconds (12 seconds right after
MPD (re)starts a stream, while it buffers), uses two-second
timeouts for each `pactl` call, and requires two consecutive correct snapshots
for the same client, separated by 0.2 seconds. Move command errors and invalid
JSON fail explicitly. Recreated clients must retain the generated output name.

`--start` now starts playback, recovers/verifies the route, then applies gain.
`--apply-gain` also recovers/verifies routing before changing gain, preventing
gain changes to a bypassed virtual sink. A route/gain failure during start
requests stop on the affected stream; other already-processed streams are not
rolled back. There is no background watchdog or restored automatic controller:
after a later USB reconnect or audio-stack restart, run `--check-route` and
`--recover-route`, or call recovery from future orchestration. Successful
verification is a current observation, not a guarantee against future policy
rerouting. Gain-only and recovery-only operations do not add queue entries.

[Route tests](../tests/test_streamroute.py) reproduce a front client whose
requested sink is correct but actual route bypasses it. Tests cover dynamic
client IDs, idempotence, post-move verification, startup waiting, ignored
moves, missing/ambiguous clients, command errors and JSON validation.
Controller tests cover selected/all route actions, read-only failures,
mutation locking, route-before-gain ordering and failure cleanup.

## Per-output loudness balancing

Each output has a runtime gain in `playback.json`, seeded from the optional
`default_gain_db` in `config.json` (default `0.0`).
Only finite values <= 0 are supported: `-6.0` attenuates by 6 dB and `0.0`
leaves unity gain. Gains apply equally to both channels of that stream's
virtual PulseAudio/PipeWire sink; the six-channel USB master and channel maps
are not changed. Gain control requires `pactl` (`pulseaudio-utils` on Debian).
Streams must use separate configured PulseAudio sinks, and MPD must actually
be routed to those sinks: clients falling back directly to the USB master
bypass virtual-sink gain.

`mpdctrl.py --start` starts each stream, recovers/verifies its route, then
applies and verifies its gain.
There can be a brief interval at the previous volume while playback starts;
inactive virtual sinks on the tested Pi ignored volume changes until a client
was active. The helper retries/read-checks for five seconds and reports failure
instead of claiming an ignored setting succeeded.
`--apply-gain` applies them without changing MPD state or adding queue entries;
it accepts the same optional `--output ID` selector. For non-unity gains,
playback must already be active. Both actions hold `mpd_control.lock`; a gain
failure exits nonzero, and start requests stop on the affected stream before
exiting. A failure partway through multiple streams can leave earlier streams
playing with gains applied: correct the error and retry. Stop and status do not alter
gain. Run `--apply-gain` again after recreating sinks or changing configuration.
Starting MPD directly does not read these JSON gain settings.
Gain commands use absolute PulseAudio volume units, converted using its cubic
volume scale, rather than signed dB arguments (which apply relative changes).
Repeated gain application therefore does not accumulate attenuation.

The [measurement helper](../mpdrussoundpi/russound/streamlevels.py) requires
FFmpeg with the `ebur128` filter (Debian/Raspberry Pi: `sudo apt install ffmpeg`).
From `mpdrussoundpi/russound/`, measure all configured feeds concurrently:

```sh
python streamlevels.py config.json --duration 900 \
  --output-config balanced-config.json > stream-levels.json
```

Duration is seconds of decoded audio per feed (default 15 minutes; minimum
10 seconds). The helper opens an additional connection to each radio URL,
decodes to stereo, and measures integrated EBU R128 loudness (LUFS), loudness
range (LU), and true peak (dBTP). It bypasses existing gain settings, does not
touch MPD/PipeWire playback, and stores no raw audio. Concurrent measurements
increase network and CPU load; run on the Pi or another machine with FFmpeg.
Input is paced at real time, including any initial server burst of buffered audio.
The whole job has a duration-plus-60-second deadline per feed, a 15-second
network read timeout, and terminates its own FFmpeg processes on failure or
interruption. No automatic reconnection hides outages. Failed, silent,
unmeasurable, truncated or decoding-error feeds prevent recommendations for
the entire set; FFmpeg warnings are included in the report for review.

Recommendations attenuate toward the quietest measured feed. The common
target is lowered further if needed to respect `--peak-ceiling` (default
`-1` dBTP). Gains are rounded down to 0.1 dB; FFmpeg summary values are also
rounded, so these are estimates, not a real-time peak guarantee. The JSON
report includes measured duration, UTC timestamps, gains and predicted
loudness/peaks. Omit `--output-config` for report-only operation. Output
configuration must be a new file; input and existing files are never
overwritten, and unrelated configuration fields are retained.

Review the report, particularly warnings, and compare several representative
programme windows before adopting the values. Fixed gains do not continuously
normalize changing programmes or limit future peaks. The generated file only
updates `default_gain_db`, which seeds outputs without saved runtime state. To
apply a recommendation to the running output (persisted in `playback.json`):

```sh
python mpdctrl.py --set-gain -6.1 --output stereo_centerlfe ~/.config/mpdrussoundpi/config.json
```

Copy accepted values into `default_gain_db` of the authoritative configuration
so a reset of `playback.json` starts from them.

[Loudness tests](../tests/test_streamlevels.py) cover FFmpeg summary/duration
parsing, rejection of silence and failures, concurrent process cleanup,
attenuation/peak calculations, safe output configuration and checked dB gain
commands. An optional real-FFmpeg test compares equal reference tones with a
known 6 dB difference. Controller tests cover selected/all gain application,
default unity, invalid settings/shared sinks and failure lock release.

Each module document below links its closest test cases and names the remaining untested behavior.
