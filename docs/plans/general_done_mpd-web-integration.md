# MPD web integration: stepwise implementation plan

Date: 2026-10-14; updated 2026-11-12. Status: **manual integration complete
with explicitly accepted limitations**. S01-S07 complete; S08 managed
station selection and S09 TuneIn catalog are implemented and active on the Pi
under the approved trusted-LAN test deployment. The configuration split and
favorites are deployed. S08/S09 live checks passed and the three approved
public-only egress restrictions are deployed. Former S10 automation is not implemented and is now tracked in the
separate [lower-priority automation plan](./general_new_hardware-playback-automation.md);
S11 manual integration acceptance is complete subject to the recorded
limitations and does not depend on S10.
The healthy-LAN update-latency
target is met, and S07 has user production sign-off.

## 1. Confirmed scope and working rules

The user has selected **solution D: Pi-local HTTP/JSON playback agent with
the existing web application as the same-origin browser gateway**.
The [architecture document](../documentation/mpd-web-integration-architecture.md) explains
the alternatives and proposed contracts.

Confirmed requirements:

- Web deployment remains independent of audio deployment, including Docker web
  on another host and native MPD/audio services on the Raspberry Pi.
- Show live playback state and available now-playing metadata per output.
- Provide per-output start/stop, source selection and stream volume controls.
- **Stream volume directly changes output calibration `gain_db`** through the
  existing backend; no separate user attenuation or combined-gain calculation.
- Support admin-configured URL stations and TuneIn station search/selection;
  the browser does not accept arbitrary stream URLs.
- Optional hardware-state-driven playback is a separate lower-priority
  [automation plan](./general_new_hardware-playback-automation.md), not a
  closure requirement for this manual integration plan.

Engineering constraints:

- Keep the backend and supervisor as the shared playback/recovery implementation.
  Agent HTTP handlers and web code must not duplicate MPD/audio mutation logic.
- Preserve configuration, calibration, intentions, routes and startup gating.
- Do not modify/redeploy production audio during the running endurance test.
  Local development with fakes is possible after approval; live fault injection
  and playback changes need a separate maintenance window.
- This document is a plan, not permission to execute every step.
- Recommendations below are **not accepted decisions** until confirmed.
  Resolve one decision at a time, record the answer, then proceed.

## 2. Decision register

Status vocabulary: **accepted**, **open**, **deferred**, **superseded**.
Each accepted answer must be recorded here and reflected in affected steps
and the architecture document. Do not silently turn a recommendation into scope.

### Accepted decisions

| ID | Decision | Accepted outcome |
| --- | --- | --- |
| A01 | Deployment architecture | Solution D: local HTTP agent; browser talks only to existing web application |
| A02 | Meaning of stream volume | Direct output calibration gain; reuse persisted `gain_db` and `set_gain` |
| D01 | Automation availability during web-host outage | Web owns hardware-demand policy. Pi preserves/reconciles the last playback intention; it does not make new hardware-driven decisions while the web host is offline. |
| D08 | Stable output identity | Outputs are keyed by immutable channel-based `id` in `config.json`, independent of the station. Admin-editable `label` is the only display-name field; ports, sinks and units remain tied to the output. The earlier offline migration is historical. |
| D04 | Manual playback precedence | Manual play/pause/stop switches that output to manual mode until explicitly returned to automatic. Accepted 2026-10-16 during S04 preparation; automation is not enabled by this decision. |
| D07 | Browser access protection | Trusted-environment access without browser login, user permissions or roles for now. Lightweight admin authentication may be added later, but is out of current scope. Preserve existing request protections and web-to-agent authentication. Accepted 2026-10-17. |
| D13 | Calibration-gain UI range and step | Slider normally -60 to 0 dB, step 0.1 dB; extend its minimum to include existing lower gains without clamping. Slider only, with number + dB display, inside the stream card's Config dialog alongside play/pause/stop. Use existing `gain_db` semantics. Accepted and refined 2026-10-17. |
| D09 | Initial source selection scope | Do not accept user-entered stream URLs. Select direct MP3/AAC/Ogg radio stations from TuneIn search; resolve playback URLs on the Pi from validated TuneIn station IDs. |
| D10 | Permission to adopt/manage MPD queues | Adopt an explicitly managed one-selected-source queue per output. Remove only identified managed song IDs; never silently clear unmanaged entries. Accepted 2026-10-17. |
| D11 | Source-switch failure behavior | Keep the newly saved desired source visible and let normal recovery retry; restoring the prior source is an explicit user operation. Accepted 2026-10-17. |
| D12 | Does source selection change playback intention? | Selecting a source always starts playback. Accepted 2026-10-17. |
| D14 | First external catalog provider | TuneIn OPML station search and Pi-side direct-stream resolution. Accepted 2026-10-17; S09 is implemented and active in the approved Pi test deployment. |

### Automation decisions moved

Open decisions D02, D03, D05, D15 and D16 and the accepted D01/D04 constraints
are recorded in the [automation plan](./general_new_hardware-playback-automation.md).
They no longer block manual integration acceptance.

D06's reverse-proxy and certificate trust model are deployed. The user
explicitly approved the production identity migration and read-only rollout on
2026-10-17, while declining a host firewall rule. The deployed Caddy source
matcher is not equivalent to host firewall enforcement.
D13 concerns a UI control, not a new attenuation layer or a change to calibration
semantics. No independent mute control is assumed.

### Proposed technical defaults, still subject to implementation validation

These are engineering baselines rather than extra user-facing choices:

- One agent per audio host; one active web/policy owner per installation.
- Flask/Waitress HTTP adapter, shared typed domain results, `/v1` API and explicit
  capability negotiation. No new web framework or message broker initially.
- Read-only polling first: approximately 2-second local/remote cadence,
  10-second UI stale threshold, slower audio-health probes. Optimize with MPD
  `idle` only if measurements justify it.
- Bounded operation worker and SQLite ledger for accepted mutations; existing
  operational config/control files remain backend-owned.
- Proposed p95 healthy-LAN status/metadata latency <= 5 seconds. Agent/client
  failure must not block hardware commands or healthy-output status.

If a technical default cannot meet acceptance, document measured evidence and
revise it; do not claim a proposed threshold has already been verified.

## 3. Ordered work packages and dependencies

Every work package should be independently reviewable and finish with its
acceptance evidence. Use small commits if requested, not one monolithic change.
Future module/test names below are illustrative, not existing files.

| Step | Deliverable | Depends on | Runtime effect |
| --- | --- | --- | --- |
| S01 | Accepted boundary/identity decisions and frozen read API contracts | A01/A02/D01; D08 | None |
| S02 | Detailed, isolated read-only MPD observations | S01 | Local fake tests first |
| S03 | Agent read-only HTTP service | S02 | No playback mutations |
| S04 | Stable IDs, revisions and coordinated migration design | S01; D04 before automation control schema | Offline fixtures; live migration later |
| S05 | Web playback cache, read routes, stream cards and SSE | S03 | Web-only behavior; disabled when unconfigured |
| S06 | Agent-only deployment and read-only target acceptance | S03, S04, S05; D06 | Approved additive service, no audio regeneration |
| S07 | Durable operations and manual playback/calibration controls | S04, S06; D07, D13 | Explicit user-authorized mutations |
| S08 | Per-output manual source selection and managed queue | S07; D09-D12 | Changes only selected output |
| S09 | First catalog adapter | S08; D14 | Provider resolution; selected output only |
| S10 | Hardware automation | Moved to [separate plan](./general_new_hardware-playback-automation.md) | Not implemented |
| S11 | Manual integration lifecycle acceptance and rollout documentation | S08, S09 | Approved outage/reboot tests; independent of S10 |

S04 and S05 can proceed independently after their prerequisites. S09 and
automation are independent; catalog availability must not delay manual control/automation.
D14 is accepted and S09 is included. Its remaining hardening and lifecycle
acceptance must not be confused with local implementation or deployment.

The deployed [configuration split](./mpd_done_config-split.md) supersedes the
legacy configuration/migration details in the historical S04/S07 records:
`config.json` holds outputs, optional defaults and station seeds;
`playback.json` holds runtime selections, gains, intentions and favorites.
`config_revision` tracks admin configuration; `control_revision` tracks runtime
state. Old formats are rejected; no compatibility migration is required.

### S01. Freeze boundaries and read contracts

**Complete (2026-10-14):** D08 accepted; read baseline defined in
[Playback read API v1](../documentation/mpd-playback-read-api.md), with reusable
[JSON fixtures](../../tests/fixtures/playback-read-v1.json). Documentation and fixture
validation passed. This freezes design only: no runtime API/migration deployed.

1. Record solution D and gain semantics; resolve D08 before implementing IDs.
   D01 is accepted: freeze hardware-demand policy ownership on the web host;
   the Pi only preserves/reconciles the last intention during web-host loss.
2. Describe inventory, observations, capabilities, errors, timestamps and
   `(agent_id, output_id)` identity. Keep observation/config/control revisions
   separate and pair observation revision with process epoch.
3. Define nullable metadata, last-known versus current state and distinct
   MPD/audio/agent/hardware freshness. Do not expose raw credential-bearing URLs.
4. Freeze same-origin web routes and SSE event names, preserving hardware
   payloads. Introduce new playback fields/events additively.
5. Specify dependency/import layout so the agent can use the existing `lib`
   code without adding Pi audio dependencies to the web image.

**Deliverable:** reviewed contract examples in the architecture/API docs and
test fixtures. **Acceptance:** each user-visible read field has a known producer;
unknown/error states are explicit; existing hardware clients remain compatible.

### S02. Implement detailed read-only observations

**Complete locally (2026-10-14):** immutable collector/probes in
[playbackstatus.py](../../mpdrussoundpi/russound/lib/playbackstatus.py), shared
configuration/connection/readiness helpers and stereo mixer inspection.
[Usage and limits](../documentation/mpd-backend-api.md) document its captured inventory and
nonblocking, independent worker API. 31 new collector/helper tests; focused
suite 100 tests, 99 passed and one existing FFmpeg-dependent skip. Runtime
Pylance checks clean. No deployment or changes to running Pi playback.

Primary surface: [MPDBackend](../../mpdrussoundpi/russound/lib/mpdbackend.py).
Keep existing `status()` callers compatible, either by an additive detailed
method or a separate collector reusing backend connection/settings helpers.

1. Read MPD `status` and `currentsong` with bounded timeouts/disconnects.
2. Normalize state, queue size, elapsed, bitrate/format and optional
   title/artist/album/station tags. Guard song-ID races around metadata reads.
3. Return one result per configured output even when another MPD is down.
   Do not convert failures into stop state or an empty successful inventory.
4. Collect route/readiness/gain using read-only helpers and separate timestamps;
   skip active-route expectations for stopped outputs. No enforcing readiness.
5. Keep client instances thread-confined and bound failed-output work. Initial
   collector should not start playback, alter queues or call `set_gain`.

**Tests:** extend [backend tests](../../tests/test_mpdbackend.py); add collector
fixtures for missing metadata, tag changes, malformed state, disconnect failure,
one unreachable MPD, race retry and inactive outputs.
**Acceptance:** command-recording fakes prove GET/observer paths are read-only;
healthy outputs remain observable during another output's timeout.

### S03. Build the read-only agent

**Complete locally (2026-10-15):** authenticated Flask/Waitress adapter,
immutable cached reads, background collector replacement/config failure
handling, independent playback/audio scheduling and bounded read retry backoff.
See [agent usage](../documentation/mpd-backend-api.md#read-only-http-agent-s03) and
[contract acceptance](../documentation/mpd-playback-read-api.md#s03-acceptance-local-2026-10-15).
No Pi deployment or ID migration is implied; D06 transport provisioning remains
separate from this local agent implementation.

1. Add a thin HTTP adapter, observer scheduler and immutable snapshot cache.
   Reuse Flask/Waitress; declare/install agent-specific dependencies separately
   from the [web requirements](../../web/requirements.txt).
2. Implement liveness, capabilities, inventory and per-output reads; return
   cached snapshots rather than initiating several MPD calls per HTTP request.
3. Enforce D06-compatible authentication from the start. A fake/test service
   can use injected credentials while production transport provisioning waits.
4. Expose explicit unconfigured/invalid-configuration errors and agent epoch.
   Disable write endpoints entirely in this increment.
5. Add bounded polling/reconnect backoff, clean worker shutdown and transition
   logs with redacted metadata/URLs.
6. Monitor config revision in the background, validate a replacement collector
   and retire the old one without blocking GETs. Invalid updated config must
   publish configuration failure, not continue claiming the old inventory is
   current. S02 collectors intentionally capture settings for one lifetime.

**Tests:** new agent HTTP/collector tests using fakes, including unauthorized
access, stale cache, partial results, shutdown and malformed requests.
**Acceptance:** service remains live/diagnosable without MPD/audio; healthy
reads do not wait for background I/O; no second recovery loop exists.

### S04. Prepare identity, persistence and migration

1. Implement D08 using existing entries without changing ports, sinks or
   derived systemd unit names. Validate uniqueness; separate immutable identity
   and editable display label.
2. Define a config revision strategy shared by all cooperating writers. Gain
   edits via CLI cannot be invisible to revision checks. Reject stale edits
   under the same mutation lock used to save/apply them.
3. Keep old intention files compatible through manual-control phases where
   possible. Before automation, design the coordinated versioned control
   record after D04; migrate preserving play/pause/stop, with manual defaults.
4. Inventory every state reader/writer, including the
   [CLI](../../mpdrussoundpi/russound/mpdctrl.py),
   [supervisor](../../mpdrussoundpi/russound/mpdsupervisor.py),
   [recorder](../../mpdrussoundpi/russound/playbackaudit.py) and deployment initializer.
   The recorder currently reads the name-to-intention JSON directly: a later
   schema change must update it or provide a shared compatible reader.
5. Use temporary migration fixtures and named backups. Reject unsupported
   versions explicitly; do not reinterpret corrupt state as playing defaults.
   Specify coordinated stop/update/migrate/start and rollback procedure.

**Tests:** upgrade/rollback fixtures, duplicate ID rejection, permissions,
atomic persistence, concurrent CLI edits and preserving calibration/intentions.
**Acceptance:** migration does not require audio unit regeneration; old processes
cannot overwrite the new schema; endurance artifacts remain untouched.

**Local preparation, 2026-10-16:** optional all-or-none persisted IDs/labels
are now validated by a shared helper. Existing legacy configuration remains
valid for CLI/supervisor; observation still requires persisted identities.
Backend/CLI calibration writes support SHA-256 revision preconditions checked
under the existing mutation lock, and label edits require a revision. Legacy
gain edits still work and invalidate old revisions. No ID mutation API exists.

A since-removed `playbackmigrate.py` tool created an
offline-only private bundle from local config/intention copies and an explicit
name-to-ID/label mapping. It preserves exact originals and every nonidentity
config field, retains legacy intentions, and generates a strictly validated
manual-mode control-v1 preview. Missing, corrupt, partial or unknown intentions
fail rather than defaulting to play. Existing persisted IDs cannot be reassigned.
The tool does not install files, regenerate units, contact MPD or restart services. Superseded by the [configuration split](./mpd_done_config-split.md): the tool was removed, outputs are now keyed by `id` in `config.json` and runtime state lives in `playback.json`.

The [current config/state contract](../documentation/mpd-backend-api.md#configuration-runtime-state-and-revisions)
records the reader/writer inventory, revision contract, rollout and rollback.
The preview is deliberately **not** a live schema migration: runtime readers
continue using legacy intentions and reject the preview. S10 must coordinate
all readers/writers and add the still-undecided automatic-target metadata before
activation. No old running process is authorized to coexist with a new control
schema. Production ID assignment/migration and rollback rehearsal on the target
remain gated maintenance work; offline acceptance does not imply deployment.
Local validation: 208 tests pass across the coordinated playback/USB/agent and
S04 selection, including 13 new migration/identity/revision tests and one new
CLI test. Changed Python production files have no reported Pylance diagnostics.

### S05. Integrate read-only web status

Primary surfaces: [web server](../../web/russound_server.py),
[controller](../../web/russound_controller.py), [frontend](../../web/static/app.js),
[web API](../../web/API.md). Agent identities, URLs and secret-file references
are deployment configuration, not editable browser settings.

1. Add validated agent endpoint/identity configuration and a playback client.
   Endpoint credentials are secret references, not returned editable plaintext.
   MPD integration is optional; absent config preserves today's Russound UI.
2. Run one background snapshot fetcher/cache per configured agent, not per
   browser. Keep all network work outside the Russound hardware state lock.
3. Add same-origin inventory/status routes and separate playback health.
4. Add stream cards with selection label, observed state, optional track tags,
   age/error and configured gain. No write controls until S07.
5. Add `playback-change` invalidations to the existing SSE transport; coalesce
   events, bound slow-client queues, resync both snapshots on reconnect and
   retain visible-page polling fallback.
6. Update config/API docs without copying agent domain logic into web handlers.
   Correct touched conflicting token/SSE documentation based on current code.

**Tests:** fake-agent web integration; existing
[server](../../tests/test_russound_server.py) and
[controller](../../tests/test_russound_controller.py) regressions; frontend browser
checks for stale status, metadata escaping, reconnect and disabled integration.
**Acceptance:** agent failure does not freeze zone controls; several browser
tabs do not multiply Pi polling; measured update latency meets the agreed target.

**Local implementation, 2026-10-16:** the web process now accepts optional
agents through `RUSSOUND_PLAYBACK_AGENTS`, a JSON array of identities, base
URLs, and token-file secret references. It validates configured
identity, read-only capabilities, response shape and remote HTTPS (HTTP is
loopback-only). One background worker per agent caches inventory; same-origin
inventory, output and health routes expose cached data and user-safe agent
errors. The UI shows safe-text-only stream cards and adds coalesced playback
invalidations to the bounded SSE queue; reconnects and visible-page polling
resync both interfaces. Playback remains optional and has no write controls.
Validation: 121 playback-agent, collector-contract, and web-server tests pass;
the changed playback client and test file have no Pylance diagnostics, and the
server has no Pylance errors. Browser smoke checks verified healthy and stale
agent rendering, safe text rendering for markup-like track metadata, SSE
invalidation refresh, polling-fallback refresh, disabled integration, and
continued Russound dashboard rendering. A multi-session cache regression issued
playback requests from eight separate web test-client sessions and verified
that they all received the same cached revision without adding upstream agent
requests (three requests for the single initial health/capability/inventory
poll). An eight-sample same-host HTTP fake-agent poll-to-cache measurement
ranged from 2.031 to 2.057 seconds (nearest-rank p95 2.057 seconds), below the
proposed five-second target in this local simulation. Local S05 acceptance is
complete. At S05 completion, healthy-LAN/Pi latency remained outstanding and
no production web configuration had been changed; the subsequent cross-host
rollout and current status verification are recorded below.

### S06. Provision and verify read-only deployment

**Deployment preparation (2026-10-15–16):** separate
[agent-only install](../../ansible/playbooks/deploy-playback-agent.yml),
[user service](../../ansible/templates/playback-agent.service.j2),
[TLS proxy play](../../ansible/playbooks/deploy-playback-agent-proxy.yml),
[Caddyfile](../../ansible/templates/playback-agent.Caddyfile.j2),
[proxy system service](../../ansible/templates/playback-agent-proxy.service.j2)
and [rollback](../../ansible/playbooks/remove-playback-agent.yml) are prepared.
See [operator instructions](../documentation/playback-agent-deployment.md). Agent and proxy
activation are opt-in; the agent remains loopback-only. The proxy play
generates a self-signed certificate on the Pi, copies only its public
certificate to the ignored Docker secrets directory, limits routes to the
read-only API, and enforces a source CIDR allowlist. Host firewall management
is not automated.

**Isolated target acceptance (2026-10-15):** user approved deployment with a
separate private observation-test config during endurance. Agent running on
Pi loopback; authenticated reads, real MPD metadata, routes/gains and unchanged
production config/intentions/service lifecycle verified. Existing Radio 88.6
faults continued; no zero-impact or uninterrupted-playback claim.
See [target evidence](#acceptance-record).

**Production read-only rollout (2026-10-17):** after explicit approval, the
production config received stable IDs/labels (`radio_886`, `fm4_radio`,
`oe3_radio`) while names, stream URLs, ports, sinks and gains were preserved.
These were later renamed to channel identities (`stereo_front`,
`stereo_centerlfe`, `stereo_rear`); see
[channel-based identities](#acceptance-record).
The legacy intentions file remained byte-identical. Only the playback
supervisor and read-only agent were stopped/restarted; all three MPD service
PIDs remained unchanged. The read-only agent now observes the production
configuration. Caddy 2.6.2 terminates TLS on port 8443 and forwards only the
read API to `127.0.0.1:8081`; the package `caddy.service` remains disabled and
nginx on port 80 was not changed.

The initial Docker source allowlist mistakenly used the Mac's Wi-Fi address,
`192.168.1.56/32`. On 2026-10-17, an authenticated request from inside the web
container returned 403. A temporary Pi-side TCP probe confirmed Docker
Desktop's actual source as `192.168.1.142`, the Mac's Ethernet address
(`en5`). The user explicitly approved Caddy's `192.168.1.0/24` source matcher
for the trusted LAN; the dedicated proxy was updated and restarted. No host
firewall rule was installed, so this application-level allowlist is the only
source filter and assumes a trusted LAN with no external port forwarding.
Authenticated Docker-to-Pi TLS health, capabilities and output reads now return
200, the web cache reports the agent healthy, and the dashboard displays all
three outputs. `/v1/start` remains unavailable. The Russound hardware warning
on the local dashboard is independent of the playback integration.

At this initial rollout checkpoint the Pi-to-Docker read path was deployed,
while the healthy-LAN latency sample and S07 controls were still outstanding.
The later S06 acceptance and local S07 implementation are recorded below.

**Historical preparation (2026-10-16):** a separate opt-in Caddy proxy play,
Caddyfile and hardened systemd service are prepared. It keeps the agent on
loopback, exposes only read endpoints over TLS on unprivileged TCP 8443,
requires a Docker-host source CIDR, generates/verifies a self-signed
hostname-bound certificate on the Pi, and fetches only its public cert into
the ignored Docker secrets directory. Firewall management was left manual.
Compose config validation and the focused preparation tests passed at that
stage; the later production deployment and target acceptance are recorded above.

**S06 read-only acceptance (2026-10-17):** authenticated Docker-to-Pi TLS reads,
unauthenticated 401 rejection, source-filter 403, read-only route restriction,
per-output metadata/status, healthy web cache and browser cards were verified.
Production identity migration changed only IDs/labels; calibration, routes and
legacy intentions were preserved, and no MPD/PipeWire service was restarted.
The user explicitly declined a host firewall; the trusted-LAN assumption and
Caddy `/24` source filter are recorded above. S06 read-only deployment is complete.
The Pi-to-web healthy-LAN p95 latency target is met; measurement details and
the maximum observed outlier are recorded in the [acceptance record](#acceptance-record). S07's
subsequent implementation and production write activation are recorded below.
Writes were disabled at the S06 read-only checkpoint.

### S07. Add durable manual operations and calibration controls

**Done and production-verified (2026-10-17, user sign-off):** typed
play/pause/stop/set-gain submissions, a bounded persistent SQLite ledger and
single worker, idempotency/revision checks, explicit partial/unknown outcomes,
web gateway routes and capability-gated Playback controls. D07/D13 are accepted.
The [S07 operation contract](../documentation/mpd-playback-operations-api.md) records request
shapes, retention/deadline/restart semantics and gain behavior. Agent and proxy
activation each require a separate opt-in; defaults remain read-only.
Fake-backed HTTP, backend/CLI/supervisor regressions and dependency-free
frontend tests validate local behavior: 144 playback tests, 48 MPD/backend/
CLI/supervisor/setup tests, 49 existing Russound server tests and 19 Node DOM
tests passed. Both deployment plays pass Ansible syntax checks; changed
production Python modules have no Pylance errors. Isolated browser checks
verified explicit Stop completion and unchanged submission of -65.1 dB with
the saved-for-next-play message. No production MPD/audio service was
modified or restarted. Agent/proxy and Docker web activation succeeded;
production capabilities, fresh status and Config dialog controls were verified
without issuing commands. The user subsequently confirmed production testing
worked well and accepted S07 as done. Individual user test cases were not
enumerated; this is not a separately measured restart/endurance result.

1. Add typed play/pause/stop/set-gain operations, output lookup and validation.
   Delegate to existing backend methods; preserve requested-versus-applied state.
2. Persist operation acceptance before returning `202`. Use bounded SQLite
   ledger/queue, scoped idempotency keys, expected revisions, deadlines and
   operation lookup. Do not parse `BackendError` strings to infer busy-lock type.
3. Record application stages. Reconcile accepted-but-interrupted operations
   after restart; never declare success solely from saved intention. Define
   ledger retention/key expiry and outcome-unknown handling.
4. Apply state/revision checks at execution as well as submission. Supersede
   queued outdated targets; explicitly report partial persistence/application
   failures and existing gain-save rollback behavior.
5. Add web submission/status routes and operation progress/error UX. Per D07,
   use trusted-environment browser access without login or user permissions;
   retain existing request protections and web-to-agent authentication.
   UI play/pause/stop are explicit actions, not toggles.
6. Expose D13's slider-only calibration control in a per-stream Config dialog
   alongside play/pause/stop, with a number + dB readout and no text input.
   Read/persist the same `gain_db`; extend the slider minimum for existing
   values below -60 dB without clamping. Supersede unsent slider drafts and
   submit only on Apply gain.

**Tests:** lost responses, retries, duplicate payload conflict, ledger restart,
busy lock, stale revisions, queue saturation, stopped/paused gain updates,
persistence/rollback failure and supervisor reapplication of selected gain.
Run existing backend/CLI/supervisor tests alongside operation contract tests.
**Live acceptance:** approved one-output commands preserve other outputs,
route/gain startup gating and saved intentions; restart restores changed gain.

### S08. Add manual source replacement

**Implemented and active in the approved Pi test deployment (2026-10-17).**
TuneIn selection, managed queues and recovery are deployed together with S09.
The [acceptance record](#acceptance-record)
records live station changes, contention and playlist-resolution fixes. The
[configuration split rollout](#acceptance-record)
records favorites, preserved stations/gains and service restarts.
The [public-only egress plan](./mpd_done_playback-egress-hardening.md) records the
subsequently deployed DNS/redirect restrictions. Full lifecycle sign-off remains
outstanding.

1. D09 accepts a TuneIn station ID/name, not a browser-supplied URL.
   `select_favorite` also accepts a saved favorite key, including an
   admin-configured URL station. Validate resolved HTTP(S) feeds on the Pi;
   never echo full URLs in errors or logs. Status uses redacted source displays.
   Pi-side DNS/redirect restrictions now enforce public-only destinations;
   the earlier trusted-LAN test-risk exception is historical.
2. Persist station identity, resolved URL, managed song ID and play intention
   atomically in `playback.json` under the shared backend lock. Update only the
   selected output; do not rewrite `config.json` or regenerate services/audio.
3. D10 authorizes one managed queue item per output. Adopt only an empty queue
   or a single queue item matching the prior configured source. Delete only the
   persisted managed song ID; refuse queues with unowned entries.
4. D11 keeps a saved new desired URL after apply failure so supervisor recovery
   retries it. D12 makes every source selection set the play intention and start
   playback. Report source-save, intention and actual-application outcomes
   separately; show desired and observed current source separately.
5. Reuse startup mute/route/gain checks when replacing active playback.
6. Provide TuneIn search, favorites and explicit durable operation outcomes
   in each output's Config dialog; do not add arbitrary URL entry.

**Tests:** repeated same/new selections, no queue accumulation, URL/persistence/
cleanup failure, stop racing a switch, paused/stopped outputs, process restart
mid-switch and preserving other config/outputs.
**Acceptance:** supervisor uses the selected source after restart; repeated
switches stay bounded; and selecting a source explicitly changes the intention
to play.

### S09. Integrate the TuneIn station catalog

**Implemented and active in the approved Pi test deployment (2026-10-17).**
Search, exact station-ID lookup, Pi-side resolution and favorites are wired
through the dashboard. Live checks covered TuneIn selection, playlist-backed
station `s260655`, and favorites add/move/remove. The config-split regression
baseline is 532 Python tests (one skipped) and 27 JavaScript tests passing.
URL-type favorite selection passed a live check on 2026-11-12. An isolated
Pi-agent no-default check and a browser-simulated dashboard check passed;
the real production stationless path remains unverified end to end.

1. Use TuneIn OPML search server-side; return only normalized station identity,
   title and bounded display metadata to the browser. Do not expose the provider
   stream URL or accept user-entered URLs.
2. Resolve an `s<digits>` station identity on the Pi using the fixed TuneIn
   endpoint. Bounded M3U/PLS resolution may extract a direct HTTP(S) stream;
   playlists are not handed to MPD as the managed source.
3. Store the resolved URL privately with the TuneIn station provenance. Refresh
   it during supervisor reconciliation so a changed provider URL can recover.
4. Use bounded request timeouts, response sizes, result counts and short-lived
   search caching. Show provider failures without interrupting active playback.
5. Manual operations remain opt-in. TuneIn selection defaults on when those
   operations are enabled; `--disable-tunein-selection` or Ansible
   `playback_agent_enable_tunein_selection=false` disables new selections.
   Pi-side egress policy is deployed because provider stream hosts and redirects
   vary: DNS-checked/pinned Python fetching plus MPD cgroup packet filtering.

**Acceptance:** catalog outage does not affect running audio or manual URLs;
unsupported entries rejected, labels escaped and arbitrary browser-supplied
stream URLs rejected.

### S10. Hardware-driven automation

Moved to the [lower-priority automation plan](./general_new_hardware-playback-automation.md).
It is not implemented and is not required to close this manual integration plan.

### S11. Lifecycle acceptance, rollback and operational handover

**Complete with accepted limitations.** The 2026-11-12 maintenance window added live URL-favorite,
edited-state persistence, user-manager and Pi reboot, partial-failure,
network recovery, listening and same-version rollback checks. The
[acceptance record](#additional-acceptance-evidence-2026-11-12) distinguishes
these results from remaining end-to-end, outage and transport gaps.
Automation-specific S11 acceptance moved with S10 to
the [automation plan](./general_new_hardware-playback-automation.md#automation-specific-acceptance-split-from-s11).
Earlier S06 latency
measurements are not a new S08/S09 lifecycle or analog-audio guarantee.

1. Run focused tests for touched components; then full regression/contract
   suites before live rollout. Syntax-check provisioning separately.
2. Measure state/title p95 latency, stale detection, queue bounds and healthy
   output/UI responsiveness with one MPD or agent unavailable.
3. In an approved window test agent restart, web restart/outage, MPD failure,
   source outage, deployment and Pi reboot. Test USB/audio faults only with
   explicit approval and preserve evidence.
4. Verify selected sources, calibration and manual intentions survive;
   confirm actual routes and both-channel signal. Digital state is not proof
   of analog output, and a 5-second UI target is not an audio dropout guarantee.
5. Exercise manual integration rollback using compatible binaries and backed-up
   schemas. Stop new writers before restoring old files.
   Stopping the agent alone does not cancel persisted playback intentions.
6. Update API/deployment/runbook docs with exact startup, health, permissions,
   credentials, backup/restore, logs and limitations.

**Acceptance:** every manual-integration gate has evidence; explicitly record
any separately agreed deferral and its limitation rather than treating it as a
pass. Automation acceptance is not a gate for this plan; no successful empty
fallbacks or unverified guarantees.

#### Manual integration closure checklist

- [x] Record the deployed revisions, topology, test window, backups and
  current health; obtain separate approval for disruptive tests.
- [x] Run focused and full regression/contract tests and provisioning syntax
  checks applicable to the deployed version; record results.
- [x] Validate a URL-type favorite and no-default-station behavior on the Pi;
  the production stationless end-to-end test is explicitly deferred below.
- [x] Verify edited favorites, gains, selections and manual intentions persist
  through the relevant agent, MPD, supervisor, web and user-manager restarts.
- [x] Exercise approved web/network/source outage, agent/MPD failure and Pi
  reboot recovery; verify stale/error reporting and healthy-output isolation.
- [x] Measure current-version status/title latency, queue bounds and
  responsiveness during partial failure; compare with the applicable target.
- [x] Validate applicable IPv6 transport and physical/analog output, routes
  and both-channel signal, or record an explicitly agreed deferral and limit.
- [x] Rehearse manual-integration rollback with backups and compatible schemas;
  document operational handover and the outcome of every gate.

The Russound controller was disconnected during earlier testing and responded
after reconnection on 2026-11-12. This is not automation acceptance. The
independent, unscheduled Pi platform investigations below are not silently
accepted as successful integration tests.

## 4. Step completion and safe pause protocol

For each step record:

- Accepted prerequisite decisions and scope.
- Changed interfaces/files and migration implications.
- Focused checks and observed results, including failures/limitations.
- Deployment status: local only, fake-backed, target read-only or live accepted.
- Backup/rollback checkpoint before changing persistent production schemas.

Keep unfinished steps unstarted/in progress rather than claiming architecture
agreement means implementation is complete. Local code completion and live
acceptance are separate checkpoints.

At a safe pause, do not leave experimental policy running or write commands
queued. Preserve unrelated worktree changes and endurance evidence. Revisions,
credentials and operational files must have an explicit owner.

## 5. Current progress and next discussion

- A01, A02, D01, D04, D06-D14 accepted.
- S01-S06 read-only status are complete: contract, collector, authenticated
  agent, production identities, dashboard and Pi-to-Docker deployment. The
  live agent and dashboard are healthy. A 156-second Pi-to-web LAN sample
  measured a conservative web-visible playback-age p95 of 4.819 seconds
  against the proposed 5-second target; details and limitations are recorded
  in the [acceptance record](#acceptance-record).
- D06 transport is deployed with Caddy's approved LAN-subnet `/24` filter. The user
  declined host firewall changes; the [acceptance record](#acceptance-record) documents the
  trusted-LAN assumption and residual network exposure.
- D07 accepts trusted-environment browser access without login or user
  permissions for now; lightweight admin authentication is deferred.
- D13 accepts a -60 to 0 dB slider with 0.1 dB steps, preserving visible/editable
  out-of-range values without clamping by extending the slider minimum.
  Controls live in a per-stream Config dialog with slider-only gain editing.
  S07 is done, deployed and production-verified (user sign-off, 2026-10-17).
- D02-D03, D05 and D15-D16 remain open in the
  [automation plan](./general_new_hardware-playback-automation.md).
- D09 accepts TuneIn station selection only: the browser submits a validated
  TuneIn station identity, and the playback agent resolves a direct MP3, AAC or
  Ogg stream from TuneIn. Bounded provider M3U/PLS resolution is supported;
  arbitrary browser-supplied URLs/playlists and other MPD URI schemes are
  out of scope. Admin-configured URL favorites are supported separately.
- D10 accepts a managed one-selected-source queue per output, with cleanup
  limited to identified managed song IDs.
- D11 keeps newly saved desired source on failure for recovery retry; restoring
  the previous source is an explicit selection.
- D12 means selecting a source explicitly changes that output's intention to
  play; there is no separate silent selection action.
- **S08/S09 are implemented and active in the approved trusted-LAN Pi test.**
  Manual operations are enabled; TuneIn defaults on with manual operations.
  The three approved public-only DNS/redirect egress restrictions are now
  deployed and live-checked. No full lifecycle sign-off is claimed.
- Because outputs now switch stations, output names, IDs and labels follow the
  audio channel (`stereo_front`/Stereo Front, `stereo_centerlfe`/Stereo
  CenterLFE, `stereo_rear`/Stereo Rear) and amend D08. Existing installations
  migrated offline with the since-removed `outputrename.py`; the dashboard shows the selected
  station next to the playback state. Superseded by the [configuration split](./mpd_done_config-split.md): the tool was removed, outputs are now keyed by `id` in `config.json` and runtime state lives in `playback.json`.
- The config/state split, optional default station/gain and favorites (maximum
  20) are implemented and deployed. No station means initially silent/stopped,
  with Play visible but disabled; omitted gain means 0 dB. Live no-station and
  URL-favorite acceptance remain pending. O12 is closed by hardcoded loopback
  MPD generation and the verified Pi listeners.

- Unscheduled idea: a [live per-output level meter](general_idea_live-level-meter.md)
  (validated external proposal and architecture-conformant sketch).

Next: finish the outstanding manual-playback acceptance and record evidence.
Automation is lower priority and independent. Schedule reboot/fault tests explicitly;
do not infer approval from this status update.

Update the appropriate plan and architecture when automation decisions are made.

## 6. Deployment acceptance

Checks on the Pi certify only the code that was deployed at the time; they do
not certify later code or full lifecycle behavior. Installation steps are in
the [deployment guide](../documentation/playback-agent-deployment.md).

### Acceptance record

- **Isolated target test (2026-10-15).** An isolated agent with a private
  config copy verified cached observations, metadata, routes/gain, HTTP
  authentication, bounded errors and liveness on the real Pi. Production
  config/intentions hashes and playback/audio processes were unchanged. This
  was not a production write or LAN latency test.
- **Production read-only rollout (2026-10-17).** Agent, Caddy TLS and Docker
  playback reads were verified: authenticated reads returned 200,
  unauthenticated 401, disallowed sources 403 and unsupported routes 404. The
  web cache and all three output cards were healthy, and activation restarted
  no MPD instance. Endpoint `https://192.168.1.105:8443`; Caddy allows the
  source CIDR `192.168.1.0/24`. This is an HTTP source filter, not an inbound
  firewall.
- **S06 LAN latency (2026-10-17).** A 156-second sample made 240 fetches with
  720 healthy observations and no stale or failed reads. Metadata change
  intervals: p50 2.458 s, p95 2.971 s, max 3.394 s. Conservative web-visible
  age: p50 2.978 s, p95 4.819 s, max 5.806 s, so the 5-second p95 target was
  met. The estimate can overstate age by up to one second because the web
  cache timestamp has second precision. It is not a browser measurement or an
  audio-dropout guarantee.
- **Manual controls and TuneIn (2026-10-17).** The user accepted S07
  transport/gain controls after production testing (individual cases not
  enumerated; not restart or endurance acceptance). TuneIn selection went live;
  follow-up fixes removed repeated resolution contention and added bounded
  M3U/PLS extraction. Playlist-backed `s260655` played.
- **Channel-based identities (2026-10-17).** Outputs were renamed to
  `stereo_front`, `stereo_centerlfe` and `stereo_rear` with stations, routes
  and gains preserved. The one-off migration tool was removed; there is no
  old-schema migration path.
- **Configuration split (2026-10-17).** Backup with checksums at
  `~/.local/state/mpdrussoundpi/config-split-backup-20261017-205452/` on the
  Pi. MPD listened only on `127.0.0.1:6601-6603`; a connection to the LAN
  address failed. All outputs resumed their stations and gains. Dashboard
  checks switched an output to another favorite and back and exercised
  favorite add/move/remove. The admin config stayed byte-identical.
- **Public-only egress (2026-10-17).** See the
  [egress plan](./mpd_done_playback-egress-hardening.md): public playback
  succeeded; literal-private, DNS-to-private and public-to-private redirects
  were blocked.

### Additional acceptance evidence (2026-11-12)

- **Non-disruptive baseline (2026-11-12).** The Pi's web and ser2net
  containers were healthy with zero restarts, using development image tag
  `dev-8f6bf092609bf910eb529684d364d191af5a3ab6`. The playback agent,
  supervisor, three MPD instances and TLS proxy were active. The web playback
  health endpoint reported the `musicpi` agent healthy; all three outputs
  reported playing with one queue entry each. One snapshot marked audio
  observations stale at about 30 seconds; a subsequent snapshot showed them
  fresh. This is a point-in-time check, not an analog or latency acceptance.
- **Local regressions (2026-11-12).** At commit
  `4334515935b0ac6c61b6e71c0bb050413260f5d7` with uncommitted deployment
  changes, Python discovery ran 599 tests successfully (one skipped), and
  Node ran 107 tests (97 passed, 10 skipped). All seven Ansible playbooks
  passed syntax checks with a localhost-only inventory; the `musicpi` host
  pattern was unmatched. The Pi checkout is at
  `8f6bf092609bf910eb529684d364d191af5a3ab6`, matching the container tags.
  Of the deployed agent's Python files, 22 match the Pi checkout, but
  `lib/outputconfig.py` is an older variant without watchdog parsing. The
  deployed variant's SHA-256 is
  `5d37dd58ae3a9983572557a869fbc3259cf74ee215bcd075784b49b869dca2f8`;
  the checkout's is
  `d846ff93063715752a10b10bdc0afb9533fff1fa6ecca4374f9f4db56f798f63`.
  The local full suite is therefore not an exact test of the deployed agent.
  In a separate temporary checkout at the Pi revision with the deployed
  parser overlaid, 104 focused agent, operations and status tests passed.
- **Approved live checks (2026-11-12).** Private backup, including a SQLite
  online backup with successful integrity check, is at
  `~/.local/state/mpdrussoundpi/integration-acceptance-20261112-191211/`.
  An edited front gain survived an agent restart and was restored. An edited
  favorite order and source selection survived individual front MPD,
  supervisor and web restarts; the other two outputs remained playing and
  their control, gain, selection and one-entry queues were unchanged. The
  original source and favorite order were restored. One immediate favorite
  restoration request received a revision conflict; it was rejected, then a
  fresh revision was used successfully.
- **Edited-state persistence (2026-11-12).** A temporarily edited front
  source and gain, favorite order and play intention survived a web restart,
  user-manager restart and subsequent whole-Pi reboot. The original source,
  gain and favorite order were restored through the agent operations API.
- **URL favorite (2026-11-12).** A temporary URL-type favorite was seeded
  into runtime state under the backend mutation lock using the already-playing
  source URL. Selection through the web and agent succeeded on the front
  output with one queue entry and unchanged other-output controls. The original
  source and favorite order were restored. This exercised live URL selection,
  not initial favorite seeding from an admin configuration.
- **Failure and restart checks (2026-11-12).** Stopping the front MPD
  produced a stale/error observation while both other outputs played, and
  front recovered after restart. A user-manager restart and Pi reboot
  recovered the agent, supervisor, MPDs, proxy and egress service. All
  outputs resumed their original selections, gains and play intentions with
  one queue entry each. After reboot, the egress startup guard succeeded and
  the cgroup-scoped nftables rule was present. A timed Wi-Fi disconnect used
  a pre-armed systemd reconnection timer; SSH and playback recovered and the
  timer service reported success. No independent outage-time monitoring was
  recorded, so intermediate UX is not established. NetworkManager recorded
  the requested Wi-Fi disconnection at 19:21:50 and timer-driven activation
  at 19:23:14; this is not an end-to-end availability measurement. Separately,
  stopping the agent was reported as unavailable while Russound remained
  connected; agent health recovered on restart.
- **Hardware and isolated check (2026-11-12).** After reconnecting the serial
  adapter and restarting the gateway and web container with approval, the
  Russound backend reported connected. The user reported both channels audible
  on each of the three outputs by switching between them; no frequency-response
  or calibrated level measurement was made. An isolated agent on the Pi,
  using separate temporary configuration and state, started a no-default
  output stopped and returned `no_station` for play; production config and
  state hashes stayed unchanged. The deployed dashboard, with a simulated
  stationless playback response in the browser only, showed “No station”
  and a visible disabled Play button with the “Select a station first” hint.
  The browser interception was removed afterward. A real production
  stationless output was not exercised end to end. The Pi has link-local
  IPv6 only and no IPv6 default route, so public IPv6 playback was not
  exercised.
- **LAN sampling (2026-11-12).** One 156-second sample had 126 successful
  requests and six URL errors; their cause was not captured. Successful
  source-timestamp observations had p95 age 3.648 seconds. A follow-up
  45-second probe returned 45 HTTP 200 responses. A second 156-second sample
  returned 156 successes and 468 observations, with p95 timestamp age
  3.560 seconds, maximum 4.201 seconds, no stale observations and at most
  one queue entry per output. Pi and measurement-host clocks differed by
  roughly 0.2 seconds with SSH round-trip uncertainty; this is an API
  observation-age estimate, not browser or actual audio-dropout latency.
- **LAN repeat (2026-11-12, after 20:03).** Another 156-second sample
  returned 156 successes and 468 observations, with no unhealthy, stale or
  error observations. Source-timestamp age was p95 3.688 seconds and maximum
  4.259 seconds; HTTP response time was p95 38.3 ms. All three outputs
  supplied metadata throughout and had at most one queue entry. The Pi clock
  was approximately 0.17 seconds ahead of the laptop at the midpoint of three
  SSH measurements, with round trips of 254–265 ms. This is an API age
  estimate, not a browser or audio-dropout measurement. The user reported
  moving the laptop and possible Wi-Fi roaming during the earlier failed
  sample; that is a plausible explanation, not a confirmed cause. Both
  subsequent 156-second samples had no request failures.
- **Title-change sampling (2026-11-12).** In a separate 240-second
  Pi-local MPD-to-web-API sample, all seven observed title changes converged
  in 1.005–3.006 seconds (nearest-rank p95 3.006 seconds), with no sampler
  errors or superseded changes. One-second sampling and the small event
  count limit the result; it is not a browser or separate-host measurement.
- **Same-version rollback rehearsal (2026-11-12).** Stopped web, agent,
  supervisor and all MPDs; made and checksum-verified private backups of
  config and playback state, plus an integrity-checked SQLite backup; then
  atomically restored the matching config/state and restarted services. The
  existing ledger and binaries were retained. All outputs resumed their
  original selections, gains and intentions, the controller reconnected,
  and the egress startup guard passed. This does not demonstrate a
  cross-version or old-schema downgrade.
- **Source-outage test (2026-11-12).** With explicit maintenance approval,
  a temporary nftables rule rejected HTTP(S) connections only from the front
  MPD service; an independently armed timer provided automatic rule removal.
  The rule counter confirmed it matched stream traffic. In 35 API samples
  over approximately 70 seconds, the front output changed from play to stop,
  its desired intention remained play, and the API reported an MPD playback
  error; the other two outputs remained playing with no sample errors. After
  removal, the front output recovered automatically within approximately
  30 seconds with its original source, gain and one queue entry. The temporary
  rule was removed and the permanent egress guard verified.
  After reloading the dashboard during the outage, it showed front as stopped
  and the other outputs playing, but did not show the MPD error or distinguish
  this failure from an intentional stop. A previously open background tab
  initially continued to display its old play state until reloaded. These are
  UX limitations, not successful outage-error reporting.
- **Outage UI fix and recheck (2026-11-12).** The web card and configuration
  dialog now show the agent's sanitized MPD playback error, including when
  play remains the desired state; stale errors are labeled last reported.
  The local JavaScript suite passed 100 tests (10 publication tests skipped),
  including recovery and hidden-tab visibility resync. With separate approval,
  the Pi web container was recreated from local image
  `mpdrussoundpi-web:source-outage-ui-20261112`, derived from the previously
  deployed web image with only the tested dashboard script replaced. The
  image ID is
  `sha256:06067cd031893d152298b874e15b6aacf790d32e5d037b07ae9c420f3b6c4d5d`.
  The original published image remains available; this local image is not a
  Hub release. The override is
  `~/.config/mpdrussoundpi-deploy/source-outage-ui.compose.yml`; removing it
  from the Compose invocation and recreating web with the original published
  override rolls back this web change. A later deployment using only the
  original published image will also remove the fix until it is released.
  Repeating the isolated front outage
  showed the error and desired-play message on the live dashboard after reload,
  while the other outputs continued playing. After removing the temporary
  rule, front recovered and the message cleared after reload. The integrated
  browser tab was hidden and intentionally paused updates; visible-tab refresh
  was covered by the visibility-change regression test, not timed live.

### User decisions (2026-11-12)

- The user considers the extended Pi run sufficient for the stationless
  acceptance decision. The isolated agent and simulated dashboard checks
  remain the evidence; a real production stationless end-to-end check was
  not performed and is not recorded as passed.
- The user accepts possible laptop roaming as sufficient context for the
  earlier LAN failures and does not require more investigation. The actual
  cause remains unconfirmed; two subsequent long samples were clean.
- Public IPv6 testing is not required for this closure. IPv6 playback on an
  IPv6-capable network remains unverified.
- Cross-version rollback is deferred to update-path evaluation and tracked in
  the [image updates idea](./general_idea_user-triggered-image-updates.md).
  The completed same-version restore does not validate downgrade compatibility.

### Acceptance limitations and handover

The user accepted the explicit stationless, LAN-cause, IPv6 and cross-version
rollback limitations above. The outage fix is installed as a local web image,
not a published release. Startup, health, credentials, backup/restore and
troubleshooting procedures are in the
[playback deployment guide](../documentation/playback-agent-deployment.md).
The image-update plan owns cross-version rollback; broader diagnostic guidance
remains in the [guided diagnostics idea](./general_idea_guided-diagnostics.md).

Automation-specific demand, mode and lifecycle acceptance is tracked in the
[automation plan](./general_new_hardware-playback-automation.md#automation-specific-acceptance-split-from-s11)
and does not block this plan's closure.

### Pi platform follow-ups

Open findings from the October 2026 hardware audit that are outside the
playback code. None of them is scheduled.

| Issue | Next step |
|---|---|
| The Pi's DNS name returns both an A record and a CNAME; SSH sessions time out intermittently. The Pi runs on Wi-Fi; Ethernet had no carrier. | Fix the authoritative DNS records, check that the resolvers agree and investigate the timeouts. Until then use the direct IP with strict host-key checking. |
| The original WirePlumber stall and some shutdown warnings are unexplained. | Capture diagnostics when it recurs and test under long load and reconnects. |
| RTKit is missing, so PipeWire logs fallback scheduling warnings. No dropout has been linked to it. | Measure under representative load; add real-time scheduling only if that shows a need. |
| Analog output levels and center/LFE frequency response were never measured (only digital levels and jack mapping). | Measure if needed, with per-channel metering and listening during longer concurrent runs. |
