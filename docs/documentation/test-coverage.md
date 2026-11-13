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
- The opt-in [USB recovery implementation](../plans/usb_done_audio-recovery.md#step-4-coordinated-opt-in-implementation)
  adds model-selection, shared-readiness and downstream graph tests in
  [test_audioidentity.py](../../tests/test_audioidentity.py), plus standalone Lua
  [decision-core](../../tests/usb-target-policy-test.lua) and
  [hook-adapter](../../tests/usb-target-hook-test.lua) harnesses. The hook harness
  mocks WirePlumber APIs. The separate
  [runtime harness](../../tests/usb-target-runtime.sh) exercises an actual isolated
  PipeWire/WirePlumber graph with synthetic exported devices, not production
  ALSA/USB hotplug. Setup tests cover generated policy ownership, guarded
  loopbacks, legacy reinstall and ambiguity preflight; backend/collector tests
  cover retained intentions/gain and read-only readiness error reporting.
  Approved Oct 16 maintenance and five short reconnects, a 149-second absence,
  port relocation, a stable-port mixed-intention retry and real duplicate-card
  refusal/removal recovery subsequently passed on the target. The first
  mixed-intention attempt was contaminated by extra physical adjustments and
  descriptor failures. USB bandwidth limits remain unresolved; see the
  [acceptance qualifications](../plans/usb_done_audio-recovery.md#final-checkpoint-after-guided-tests).

## Configuration split (config.json / playback.json)

[Config tests](../../tests/test_outputconfig.py) cover the `outputs`/`stations`
schema, rejection of the old `streams` format, unknown fields, duplicate
IDs/ports/sinks, unknown `default_station` keys, URL and TuneIn station
validation and the 20-station limit. [State tests](../../tests/test_playbackstate.py)
cover defaults (optional `default_station`/`default_gain_db`), stationless
outputs, favorites seeding, add/remove/move, duplicate no-ops, the cap and
atomic writes. Backend, agent, operations, status, CLI, supervisor, setup,
audit and loudness tests verify that `config.json` stays byte-identical, that
outputs without a station cannot play, URL/TuneIn favorite selection, favorites
HTTP endpoints and capability opt-out, URL privacy in favorites responses and
probe preservation across runtime station/gain changes.
[playbackfixtures.py](../../tests/playbackfixtures.py) provides the small shared
configuration. [Web command tests](../../tests/test_playback_commands.py) cover
favorites polling/validation (no URL field accepted), the favorites proxy route,
CSRF rejection and `output_id: null` checks; [dashboard tests](../../tests/test_playback_controls.js)
cover the favorites list, current marker, Play/↑/↓/✕, ☆/★ toggles including the
20-entry cap, and Play disabled ("Select a station first") without a station.

## S04 offline preparation (2026-10-16)

Migration/revision tests (removed with the tool by the [configuration split](../plans/mpd_done_config-split.md)) covered exact
original-byte rollback material, preservation of all nonidentity fields,
private bundle permissions, unchanged input files, exclusive destination
creation and failed-write cleanup. They reject duplicate/partial mappings,
ID reassignment, corrupt/missing/unknown intentions and unsupported control
preview versions. Current runtime readers reject the preview rather than
initializing playing defaults. Backend tests verify stale gain edits fail
before audio effects, legacy edits invalidate revisions, label edits preserve
IDs/operational fields and concurrent gain edits respect the existing lock.
[CLI tests](../../tests/test_mpdctrl.py) cover revision preconditions and argument
requirements. The offline CLI test verifies preparation does not create an
MPD client.

The coordinated local selection passes **208 tests**, including the existing
194 playback/USB/agent regressions and 14 added S04 tests. This is not measured
line coverage or live production-migration acceptance. Pylance reports no
diagnostics in the changed production Python files. No Pi deployment, live
control-state replacement or production rollback was performed for S04.

## Test Links

- [Zone tests](../../tests/test_zone.py)
- [Backend tests](../../tests/test_russound_backend.py)
- [Connector tests](../../tests/test_russound_connector.py)
- [Controller tests](../../tests/test_russound_controller.py)
- [Server tests](../../tests/test_russound_server.py)
- [Dummy backend integration tests](../../tests/test_dummy_backend.py)
- [Audio setup tests](../../tests/test_audiosetup.py)
- [MPD setup tests](../../tests/test_mpdsetup.py) verify explicit meta-service
  dependencies and ordering, including an empty instance list. Installation
  reloads the user systemd manager after writing units; enabling the meta-service
  pulls in the configured instances on its next activation. Reloading unit
  definitions alone does not start previously inactive instances.
- MPD setup and audio setup share the kernel-lock implementation in
  [setuplock.py](../../mpdrussoundpi/russound/lib/setuplock.py), with separate
  per-user filenames. MPD installation holds `mpd_setup.lock` through
  daemon reload; cleanup acquires the same lock before stopping services or
  removing files. Tests cover contention, audio/MPD lock independence,
  invalid configuration, reload failure, cleanup exclusion and normal exit.
  The shared lock's exception, interrupt and SIGKILL behavior is covered by
  the audio setup tests. Both require a private mode-0700 `XDG_RUNTIME_DIR`;
  persistent lock files must not be deleted while setup may be running.
- [MPD controller tests](../../tests/test_mpdctrl.py) cover shared-backend
  delegation, selected/all actions, pause, read-only status/routes,
  unavailable/wrong routes, unknown names and truthful exit status.
  [Backend tests](../../tests/test_mpdbackend.py) cover locking, protocol behavior,
  queue idempotence, persistence, saved intentions and startup gating.
- [Readiness tests](../../tests/test_audioready.py) cover missing hardware/sinks,
  profile enforcement with mandatory re-verification and bounded retries.
  [Supervisor tests](../../tests/test_mpdsupervisor.py) cover independent
  recovery, service failures, initialization, per-stream backoff and watchdog
  wiring (continuous-mode only, disabled configuration, backoff clearing).
- [Stall watchdog tests](../../tests/test_playbackwatchdog.py) cover
  unresponsive and frozen-elapsed detection with a fake clock and scripted
  probes, baseline resets on progress and station changes, intention
  exclusions, restart rate limiting, audio-stack escalation and its window,
  audio-not-ready deferral, failed-restart backoff and the state file.
- [Audio cleanup tests](../../tests/test_audiosetup.py) cover CLI dispatch,
  install/cleanup/reinstall, ownership preflight, symlink refusal, byte-preserved
  PulseAudio settings, idempotent includes/cleanup, safe wrapper removal,
  legacy/malformed configuration refusal, backend selection, partial installs,
  locking and checked restart failures. Audio service commands are mocked;
  this is not a live production teardown test.
- [Endurance recorder tests](../../tests/test_playbackaudit.py) cover read-only
  protocol calls, playback/route/gain/silence anomalies, bounded observations,
  failure isolation, cursor handling, persistent deadline/counters and
  independent service generation. See [test procedure](./mpd-playback-endurance-test.md).
- [Playback observation tests](../../tests/test_playbackstatus.py) cover S02's
  immutable ID-sorted snapshots, detailed metadata and bounded race retry,
  failed/last-known probes, monotonic stale thresholds, sanitized errors,
  captured config revision, read-only audio/MPD calls and independent healthy
  publication while another MPD/audio read is blocked. They consume the
  [S01 contract fixtures](../../tests/fixtures/playback-read-v1.json). Local
  focused observation/backend/readiness/CLI/supervisor/audit/measurement run:
  100 tests, 99 passed and one existing real-FFmpeg skip (2026-10-14).
  HTTP agent, browser integration and real-Pi latency are not tested by S02.
- [Read-only agent tests](../../tests/test_playbackagent.py) cover S03 bearer
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
- [Agent deployment tests](../../tests/test_playback_agent_deployment.py) perform
  static YAML/template validation only: loopback user unit, opt-in activation,
  isolated sources/venv, private credentials, no audio/MPD service operations
  and limited rollback. They execute no deployment or target commands.
  Six static tests and both Ansible syntax checks passed (2026-10-15).
- [Isolated target acceptance](../plans/general_done_mpd-web-integration.md#acceptance-record)
  verifies real Pi cached reads, metadata, routes/gain and HTTP authentication
  with a separate private config copy. Production config/intentions hashes and
  existing audio/MPD/supervisor/recorder PIDs/restarts were unchanged; recorder
  sampling advanced. Existing Radio 88.6 (now Stereo Front) faults continued. No fault injection,
  playback repair, Docker transport or p95 update-latency acceptance was performed.

## Playback input bindings

[Python mapping tests](../../tests/test_input_playback.py) cover binding validation,
editor persistence/removal, cached HTTP/event resolution, station changes,
no-station fallbacks and offline/missing mappings.
[JavaScript mapping tests](../../tests/test_input_playback.js) cover dirty tracking,
offline selector preservation, previews and in-place source-name updates that
keep the selected hardware input unchanged and render station labels as text.

## Deployment workflows

[Workflow tests](../../tests/test_deployment_workflows.py) check non-forcing Git
updates, external-gateway mode, writable mounts/private secret directories,
playback agent environment rendering, native persistent state and isolated
dependencies, and staging audio without starting/enabling playback.
All deployment playbooks are separately syntax-checked. These are controller
checks, not execution of the native/Docker provisioning paths on a new Pi.

[Profile tests](../../tests/test_container_profiles.py) render the new dev/prod
layouts with external, managed and dummy backends, verify mount/logging/build
boundaries, and exercise the real inventory-driven preview against a deliberately
unreachable target. Invalid inputs and apply requests must fail locally.
Saved image candidates are reused for all three gateway modes, including
Ansible check mode; conflicting inputs, wrong platform and missing files must
fail without changing the saved selection.
[Image-selection tests](../../tests/test_deployment_images.py) use deterministic
registry metadata to check revision pairing, ARM64/AMD64 selection, digest and
descriptor validation, publication races, malformed metadata, credential-free
CDN redirects, exclusive private file creation, offline reuse and non-mutating
index-level update checks. Public registry availability is a separate manual
metadata integration check, not a dependency of the unit suite.
[Persistent-state tests](../../tests/test_deployment_state.py) use disposable
private directories, competing processes and process exit to verify host-wide
exclusion, retained pending/committed records, strict identities, unsafe-file
rejection and separate finalization. Injected file/sync/rename failures must
retain evidence; an error after reservation removal must remain explicitly
uncertain. They do not prove power-loss durability on the target filesystem.
[Completion tests](../../tests/test_deployment_completion.py) run actual local
Ansible plays with the controlled callback, including successful/failed handlers,
rescued/ignored failures, an unreachable fixture and prohibited retry/loop/
conditional/async patterns. Missing, duplicate or contradictory outcomes and
callback write/sync errors block completion. A separate pipe acknowledgement
prevents a terminal sync failure from being mistaken for success merely because
complete-looking JSON is readable. Event content excludes task/result/item values.
These are internal runner tests; automated target apply remains disabled.
[Protected-input tests](../../tests/test_deployment_inputs.py) check private
snapshot creation, source/content/identity/mode drift, unsafe file types,
exclusive stable installation, retained partial evidence and readability after
staging cleanup. They do not prove cross-process approval-seal custody or
runtime service acceptance.
[Target preflight tests](../../tests/test_deployment_preflight.py) exercise
read-only directory/listener checks, incompatible or remote daemons, existing
projects and mount/device conflicts, serial mode/holder inspection and redacted
failure output. A standalone Linux test uses fixture Docker metadata with real
paths and socket operations; a Linux pseudo-terminal tests actual open-device
detection without hardware. The real Ansible entry point is tested for explicit
target execution/failure and controller-first validation; default previews remain
target-disconnected. These checks do not establish deployment safety or routing.
[Backup tests](../../tests/test_deployment_backup.py) validate private stopped-writer
bundles, strict paths/metadata/size limits, separate manifest identities,
cross-process copied-bundle verification, stream short writes, process loss,
corruption and failed persistence/restore operations. They restore a real SQLite
database with committed WAL content left by an exited writer and verify database
integrity. Source mutation, unexpected attributes and unsafe links/types fail
explicitly; restore never overwrites an existing destination or clears pending.
On macOS these unit fixtures shim the unavailable Linux xattr API to an empty
attribute set; Linux runs exercise real inspection and extended-attribute
rejection. This does not certify target power-loss durability or live recovery.
[Template integration](../../tests/test_deployment_render_integration.py) uses
actual Ansible and Compose rendering for agent configuration and preflight.

[Web lifecycle tests](../../tests/test_web_lifecycle.py) use actual Waitress
subprocesses, SIGTERM/SIGINT and open SSE streams, requiring exit 0 within
eight seconds of the signal without unfinished-worker warnings. An in-flight
request fixture must finish its write before process exit. Unit tests cover
signal-handler restoration, repeated signals, embedded threads, startup failure,
new-request rejection, watcher join/connection release, complete cleanup attempts
and explicit errors for workers/pollers that fail to stop.
[Logging initialization tests](../../tests/test_logging_initialization.py) use
fresh processes and all six audio-helper/setup import orders, requiring root
logger state to stay unchanged and no relative debug file to be created.
Setup commands must emit one copy of operational/warning messages to stderr;
diagnostics require `--debug`, even when logging was configured earlier.
Help remains side-effect-free. These fixtures never invoke native service or
audio commands.

[Opt-in runtime integration](../../tests/test_container_runtime.py) requires a
local Docker socket, an already cached published web digest and a local dummy
image. It creates a unique disposable Compose project, private bind data, an
internal backend network and a separate web frontend published on an ephemeral
loopback port. Tests verify host HTTP/backend routing, unchanged reruns,
recreation with preserved data, failure for an unavailable image without
replacing web, and external-gateway routing. Cleanup targets that project only.
This tests the cached web image, not a build of current web source. It does not
validate USB, hardware, native systemd, migration, backup/restore or a remote
deployment. Run explicitly after preparing the two images:

```sh
RUSSOUND_RUN_CONTAINER_TESTS=1 \
RUSSOUND_TEST_WEB_IMAGE=itcornernet/mpdrussoundpi-web@sha256:VERIFIED_DIGEST \
RUSSOUND_TEST_DUMMY_IMAGE=mpdrussoundpi-dummy:local \
  .venv/bin/python -m unittest discover -s tests -p test_container_runtime.py
```

[Opt-in recovery integration](../../tests/test_container_recovery.py) requires
local Docker, Ansible and the cached web image, but no additional image build.
A test-only override runs current web and dummy source with that cached Python-capable
image, explicitly setting the dummy host/state and TCP healthcheck. The test owns a
unique local Linux volume and Compose project. After routing and unchanged
rerun checks, it opens a real SSE connection and requires the web container
(Python as PID 1) to stop with exit 0 in under six seconds rather than reaching
its eight-second forced-kill timeout. It then stops the dummy writer,
captures/verifies their data and injects a
real failed Ansible/Compose update with `--pull never`, restores a separate
tree and verifies routing and preserved config/state/secrets from the restored
stack. Pending state and reservation must stay blocked. Setup temporarily adds
only `CHOWN` to a no-network helper for its own volume ownership; backup/restore
helpers run as the service user with all capabilities dropped. Cleanup targets
only the test's project and volume. This is not the finished apply/recovery
entry point, real serial/proxy/audio acceptance or image-archive restoration.

```sh
RUSSOUND_RUN_CONTAINER_TESTS=1 \
RUSSOUND_TEST_WEB_IMAGE=itcornernet/mpdrussoundpi-web@sha256:VERIFIED_CACHED_DIGEST \
  .venv/bin/python -m unittest discover -s tests -p test_container_recovery.py
```

### Local development image preparation

[Local-build tests](../../tests/test_deployment_build.py) cover explicit clean
commit selection, source snapshot isolation, rejected source links, local
Docker/Buildx binding, network opt-in, mid-build source drift, partial-pair
failure evidence, private modes, copied offline bundles, archive/config/platform/
tag/checksum validation, missing OCI descriptor graphs, unsafe files and CLI
failure output. Actual Ansible previews cover all three gateway modes, an
unreachable target, check mode, conflicting inputs and blocked apply.
The opt-in test builds a real ARM64 scratch pair, copies/reverifies it in a
separate process with an unusable Docker endpoint, removes only its own image
tags, then actually reloads its archives and inspects exact image identities:

```sh
RUSSOUND_RUN_LOCAL_BUILD_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -p test_deployment_build.py
```

The real repository ARM64 web/ser2net pair was also built and archive-verified
from clean revision `161853e0599e289c9e12008186573a625e5272b9`. The first
exact-image recovery run found HTTP 404 health failures: the builder's private
umask made staged static directories inaccessible to the configured numeric
service UID. Explicit public directory modes within the private build context
fixed this; a regression asserts nested directories/files under umask `077`.
Both rebuilt application archives also loaded successfully into the local
controller daemon and retained the verified image IDs/source/platform; the
gateway binary reported ser2net 4.3.11.
The rebuilt web image passed the real routing, unchanged-rerun, SSE/PID-1 clean
stop, failed-update and isolated data-recovery loop **without a web source
mount or entrypoint override**. Dummy/helper source remains fixture-mounted:

```sh
RUSSOUND_RUN_CONTAINER_TESTS=1 \
RUSSOUND_TEST_LOCAL_BUNDLE=/absolute/controller/path/to/managed-bundle \
RUSSOUND_TEST_LOCAL_MANIFEST_SHA256=sha256:SEPARATELY_RETAINED_DIGEST \
  .venv/bin/python -m unittest discover -s tests -p test_container_recovery.py
```

This mode verifies the bundle before selecting its web image and requires the
actual image to be already loaded on the controller. It currently uses
`managed`/`linux/arm64` bundles. It does not test physical ser2net/USB routing,
cross-engine target image-ID conversion, SSH transfer or automated target apply.
Full regression: **755 tests, 749 passing and six skipped**, including the
opt-in real scratch build/export/reload test. No Pi change was made.

### Reserved local-image loading and records

[Reserved-load tests](../../tests/test_deployment_load.py) add local-candidate
pending/committed records, preserved prior commits, exact intent/operation/
phase checks and the mandatory loaded-ID boundary. Registry state tests still
use their existing schema. Tests reject malformed candidates, incompatible
daemons, colliding tags, wrong target identities/labels and transferred-file
drift. They cover a partial pair load, timeout, durable-marker process death,
marker/receipt persistence failures and blocked retries without clearing the
reservation. A synthetic valid OCI archive demonstrates classic config-ID
conversion; it is not an actual second-engine compatibility test.

With `RUSSOUND_RUN_LOCAL_BUILD_TESTS=1`, a real scratch pair is built/exported,
copied privately, independently selected, removed by its unique tags, then
stream-loaded through the reserved loader into the controller daemon. Actual
target IDs/source/platform are inspected and recorded, while the operation
correctly remains pending at `backup-complete` with no service completion
or finalization inferred. Cleanup removes only that fixture's unique tags.
The focused file has **15 tests**; the complete suite now runs **770 tests,
764 passing and six skipped** with both real local-build integrations enabled.
Pylance reports no diagnostics in the changed production modules.

This validates the internal target-local load boundary, not SSH transport,
human approval custody, a full Ansible apply/update, hardware or rollback.
The Pi was not contacted or changed for this implementation slice.

### Reviewed artifact handoff and SSH transfer

[Transfer tests](../../tests/test_deployment_transfer.py) cover review
acknowledgement/source/host-trust drift, fixed SSH options and remote-path
quoting, private bytes/modes, wrong reservation/intent/phase, bounded framing,
truncation/corruption/trailing bytes, missing receipts, timeout, replay and
existing destinations. Child-process death after marker persistence blocks
resume; marker and verification-write failures retain pending state.
The reserved loader refuses an interrupted transfer even if its phase is
advanced independently. Successful process exit does not release the marker.
Controller receipts are withheld when source custody changes after transfer.

The focused file has **16 tests**. Opt into the real, isolated loopback SSH
fixture (requires `ssh`, `sshd` and `ssh-keygen`):

```sh
RUSSOUND_RUN_SSH_TRANSFER_TESTS=1 RUSSOUND_RUN_LOCAL_BUILD_TESTS=1 \
  .venv/bin/python -m unittest discover -s tests -p test_deployment_transfer.py
```

The test starts its own unprivileged loopback sshd with disposable host/client
keys and a forced fixture receiver, checks readiness, actually sends the
reviewed pair over SSH, rejects a different valid host key and stops its exact
server process. With both opt-ins it builds a real clean-source scratch pair,
removes only its own tags, transfers/verifies archives, then calls the reserved
loader against those received bytes and inspects actual Docker identities.
Fixture state is private and user-owned instead of touching the fixed
production root-owned state. The operation remains pending at `backup-complete`;
no runtime acceptance or finalization is inferred.

The complete suite passed **786 tests: 780 passing, six skipped**, with
both opt-ins enabled. Changed production modules have no Pylance diagnostics.
This proves the local real SSH/build/load chain, not target tooling bootstrap,
Ansible become, interactive human-approval custody, production adoption,
runtime update/rollback or physical hardware. The Pi was not contacted.

### Integrated target-local development transaction

[Runner tests](../../tests/test_deployment_runner.py) add 15 cases covering
explicit review rejection, approval-time source drift, altered review scope,
non-overlapping storage, private input installation, exact phase ordering,
off-host verification before load, and successful finalization. Failure
injection at stop, backup, load, apply, independent checks and finalization
retains pending state/evidence and prevents automatic retry. A failed recorded
update preserves its previous committed selection. Changed installed inputs,
false-success hook results and missing/extra/failed checks block progress.
Controlled Ansible rejects unsafe extra-variable files; its ten-minute timeout
retains uncertainty rather than implying target work stopped.

```sh
RUSSOUND_RUN_LOCAL_BUILD_TESTS=1 PYTHONPATH=tests \
  .venv/bin/python -m unittest test_deployment_runner test_deployment_completion
```

The real Ansible fixture consumes installed playbook/inventory/secret paths and
loaded IDs through private extra variables; its independent check rereads the
resulting file before commit. With the Docker opt-in, the same transaction
builds/exports a unique clean-source scratch image, removes its tag, performs
the actual reserved archive load and controlled Ansible run, verifies the
actual Docker identity, and finalizes. Only its own tag is removed afterward.
This tests the integration chain, not a running Russound container or routing.
Unit fixtures independently copy and verify backup bytes in another private
directory, not an actual off-host storage system. On platforms without
`os.listxattr`, these fixtures replace only that unavailable metadata query;
they do not certify Linux ACL/xattr preservation. The separate Linux-volume
container recovery fixture remains the real stopped-writer recovery test.

The complete suite passed **801 tests: 795 passing, six skipped**, with both
local-build and SSH opt-ins. The runner and completion production modules have
no Pylance diagnostics; existing completion callers remain signature-compatible.
Remote approval/input custody, concrete platform hooks, adoption, root/service
access, off-host old-image recovery, physical routing and a Pi deployment are
not certified by these tests. Public apply remains blocked; the Pi was unchanged.

### Reviewed remote deployment and explicit recovery

Real Pi preflight follow-up covers Docker's `Devices: null` for unrelated
containers and Compose's null command/entrypoint defaults. Terminal approval
uses separate read/write streams because controlling terminals are nonseekable;
tests preserve exact-phrase approval and rejection of other input. Full local
regression after these fixes: **818 tests, 812 passing, six skipped**.

[Operator tests](../../tests/test_deployment_operator.py) and
[registry-load tests](../../tests/test_deployment_registry_load.py) cover bounded
private framing, exact file lengths/checksums, truncation/corruption/replay,
off-host portable manifest/blob verification, unsafe Compose services/mounts,
explicit adoption/acceptance policy, protected token/CA access, native writable
descriptors and outstanding Ansible work. Registry tests require the exact
pinned pair, checked source/platform/config IDs and durable one-shot pull state;
wrong identities/timeouts do not advance or retry. Recovery-state tests reject
incomplete check sets and correctly remove a resolved first-install record
instead of leaving an invalid empty record.

The opt-in workflow runs a real isolated sshd and the exact controller SSH client,
with a forced command confined to a disposable Linux/root target fixture.
Docker socket access is required **only for this explicitly opted-in fixture**.
It uses a unique Linux data volume, Compose projects, network and external dummy
gateway; no target/Pi address is contacted. It proves:

- Wrong-host-key refusal before target work; real same-layout adoption,
  verified local-image delivery and actual controlled Ansible/Compose apply.
- Actual app-to-external-dummy communication, container/published-host HTTP,
  health and exact runtime identities, with unchanged container IDs on rerun.
- Exported old Docker image identities and independent controller-side backup
  verification before apply, not merely a successful local file copy.
- A genuinely failing application image, retained pending/old committed state,
  separate approved restore, preserved secrets and post-backup operator edits.
- Lost backup acknowledgement blocks apply; only an approved pre-apply
  original-runtime restart can resolve that path.
- Failed first-install recovery restores prepared data and absence of containers,
  reports absence-specific checks, and leaves unrelated deployments/gateway intact.

Build the missing integration dependencies only for this fixture:

```sh
docker build -f tests/integration/Dockerfile.deployment \
  -t mpdrussoundpi-deployment-test:local tests/integration
RUSSOUND_RUN_DEPLOYMENT_TESTS=1 \
RUSSOUND_TEST_DEPLOYMENT_IMAGE=mpdrussoundpi-deployment-test:local \
RUSSOUND_TEST_WEB_IMAGE=EXPLICIT_CACHED_WEB_IMAGE \
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_deployment_operator test_deployment_registry_load test_deployment_runner
```

The cached image must contain the actual compatible web app for the daemon
platform. The fixture derives uniquely labelled clean Git-revision images from
it; it does not certify a newly published release's source content. Only exact
fixture processes, containers, network, volume and operation-owned image tags
are removed. The dependency image remains cached for reuse.

Full suite with local-build, SSH-transfer and workflow opt-ins:
**815 tests, 809 passing, six skipped**. Production Python modules have no
Pylance diagnostics. The bootstrap play passes Ansible syntax checking without
a target connection. The real Pi sudo/bootstrap path, full host `/proc`
visibility, physical controller/audio, other layout migrations and filesystem/
reboot/power-loss durability remain S5 acceptance, not inferred passes.

## Public-only playback egress

[Public network tests](../../tests/test_publicnetwork.py) cover IPv4/IPv6 public
classification, mixed/empty/private DNS answers, rebinding between validation
and connection, pinned socket addresses, original HTTP Host/TLS hostname,
certificate validation, redirect revalidation and disabled environment proxies.
[Filter tests](../../tests/test_playbackegress.py) cover cgroup-only scoping,
control replies, exact UDP and TCP DNS exceptions, shared address sets, input rejection,
fail-closed startup, cgroup identity changes and persistent unit configuration.
[TuneIn tests](../../tests/test_tunein.py) retain search/cache and bounded
playlist/error coverage with deterministic DNS.

Local baseline: 551 Python tests, one skipped; 27 JavaScript tests passed.
The [egress rollout evidence](../plans/mpd_done_playback-egress-hardening.md#rollout-evidence-2026-10-17)
separately records real Pi public playback and direct/DNS/redirect blocking,
including an isolated MPD and zero private fixture requests. Live IPv6 transport,
user-manager restart and whole-Pi reboot were not tested.

## Manual MPD Stream Control

A reusable [Python MPD backend API](./mpd-backend-api.md) also provides
playback/status/route control and persistent per-output gain changes without
an HTTP server. [Backend tests](../../tests/test_mpdbackend.py) cover queue
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

The controller uses [streamroute.py](../../mpdrussoundpi/russound/lib/streamroute.py)
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

[Route tests](../../tests/test_streamroute.py) reproduce a front client whose
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

The [measurement helper](../../mpdrussoundpi/russound/streamlevels.py) requires
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

[Loudness tests](../../tests/test_streamlevels.py) cover FFmpeg summary/duration
parsing, rejection of silence and failures, concurrent process cleanup,
attenuation/peak calculations, safe output configuration and checked dB gain
commands. An optional real-FFmpeg test compares equal reference tones with a
known 6 dB difference. Controller tests cover selected/all gain application,
default unity, invalid settings/shared sinks and failure lock release.

Each module document below links its closest test cases and names the remaining untested behavior.
