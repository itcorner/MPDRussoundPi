# MPD web integration: stepwise implementation plan

Date: 2026-10-14; updated 2026-10-17. Status: S01-S07 complete; S08 managed
station selection and S09 TuneIn catalog are implemented and active on the Pi
under the approved trusted-LAN test deployment. The configuration split and
favorites are deployed. S08/S09 live checks passed and the three approved
public-only egress restrictions are deployed; full lifecycle acceptance remains
outstanding. S10 automation is
not implemented; S11 acceptance is incomplete. The healthy-LAN update-latency
target is met, and S07 has user production sign-off.

## 1. Confirmed scope and working rules

The user has selected **solution D: Pi-local HTTP/JSON playback agent with
the existing web application as the same-origin browser gateway**.
The [architecture document](./mpd-web-integration-architecture.md) explains
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
- Support optional hardware-state-driven playback using the web application's
  existing Russound interfaces.

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

### Open decisions, in discussion order

Each row describes one decision. Some can be deferred without blocking
read-only status; the gate column states when the answer becomes necessary.

| ID | Decision | Recommendation / alternatives | Required before |
| --- | --- | --- | --- |
| D02 | Which hardware demand rule should automatic mode use? | Any powered zone selecting the mapped input. Alternative: any powered zone starts all managed outputs. | S10 |
| D03 | Physical input/output mapping and zone scope | Explicit mapping supplied/verified by user; never infer from MPD ports or sink names. | S10 |
| D05 | Behavior when automatic-mode hardware observations become unknown/stale | Hold last effective intention and show degraded automation. Alternative: stop after an agreed grace interval. | S10 |
| D15 | Automatic-mode stop delay | Suggested 30 seconds of continuously confirmed no-demand; alternative immediate stop or another interval. | S10 |
| D16 | Freshness and lease timing | Suggested hardware stale/lease thresholds 30 seconds at 10-second poll/renew cadence; adjust to measured full-poll duration. | S10 |

D01 is resolved: use the web-hosted policy path in S01/S10, with no independent
Pi hardware-state feed or Pi policy evaluator. This availability choice does not
resolve D05: stale hardware observations while the web is running still need
their own policy decision. Web-host loss must preserve the last Pi intention.

D03 is a configuration decision: collect the actual mapping in a separate
focused question after D02, not during the availability question D01.
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
| S10 | Hardware observations, policy and automatic targets | S07; D01-D05, D15-D16 | Opt-in automatic mutations |
| S11 | Full lifecycle acceptance and rollout documentation | S08, S10; S09 if catalog included | Approved outage/reboot tests |

S04 and S05 can proceed independently after their prerequisites. S09 and S10
are independent; catalog availability must not delay manual control/automation.
D14 is accepted and S09 is included. Its remaining hardening and lifecycle
acceptance must not be confused with local implementation or deployment.

The deployed [configuration split](./config-split-plan.md) supersedes the
legacy configuration/migration details in the historical S04/S07 records:
`config.json` holds outputs, optional defaults and station seeds;
`playback.json` holds runtime selections, gains, intentions and favorites.
`config_revision` tracks admin configuration; `control_revision` tracks runtime
state. Old formats are rejected; no compatibility migration is required.

### S01. Freeze boundaries and read contracts

**Complete (2026-10-14):** D08 accepted; read baseline defined in
[Playback read API v1](./mpd-playback-read-api.md), with reusable
[JSON fixtures](../tests/fixtures/playback-read-v1.json). Documentation and fixture
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
[playbackstatus.py](../mpdrussoundpi/russound/lib/playbackstatus.py), shared
configuration/connection/readiness helpers and stereo mixer inspection.
[Usage and limits](./mpd-backend-api.md) document its captured inventory and
nonblocking, independent worker API. 31 new collector/helper tests; focused
suite 100 tests, 99 passed and one existing FFmpeg-dependent skip. Runtime
Pylance checks clean. No deployment or changes to running Pi playback.

Primary surface: [MPDBackend](../mpdrussoundpi/russound/lib/mpdbackend.py).
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

**Tests:** extend [backend tests](../tests/test_mpdbackend.py); add collector
fixtures for missing metadata, tag changes, malformed state, disconnect failure,
one unreachable MPD, race retry and inactive outputs.
**Acceptance:** command-recording fakes prove GET/observer paths are read-only;
healthy outputs remain observable during another output's timeout.

### S03. Build the read-only agent

**Complete locally (2026-10-15):** authenticated Flask/Waitress adapter,
immutable cached reads, background collector replacement/config failure
handling, independent playback/audio scheduling and bounded read retry backoff.
See [agent usage](./mpd-backend-api.md#read-only-http-agent-s03) and
[contract acceptance](./mpd-playback-read-api.md#s03-acceptance-local-2026-10-15).
No Pi deployment or ID migration is implied; D06 transport provisioning remains
separate from this local agent implementation.

1. Add a thin HTTP adapter, observer scheduler and immutable snapshot cache.
   Reuse Flask/Waitress; declare/install agent-specific dependencies separately
   from the [web requirements](../web/requirements.txt).
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
   [CLI](../mpdrussoundpi/russound/mpdctrl.py),
   [supervisor](../mpdrussoundpi/russound/mpdsupervisor.py),
   [recorder](../mpdrussoundpi/russound/playbackaudit.py) and deployment initializer.
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
The tool does not install files, regenerate units, contact MPD or restart services. Superseded by the [configuration split](./config-split-plan.md): the tool was removed, outputs are now keyed by `id` in `config.json` and runtime state lives in `playback.json`.

The [current config/state contract](./mpd-backend-api.md#configuration-runtime-state-and-revisions)
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

Primary surfaces: [web server](../web/russound_server.py),
[controller](../web/russound_controller.py), [frontend](../web/static/app.js),
[web API](../web/API.md). Agent identities, URLs and secret-file references
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
[server](../tests/test_russound_server.py) and
[controller](../tests/test_russound_controller.py) regressions; frontend browser
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
[agent-only install](../ansible/playbooks/deploy-playback-agent.yml),
[user service](../ansible/templates/playback-agent.service.j2),
[TLS proxy play](../ansible/playbooks/deploy-playback-agent-proxy.yml),
[Caddyfile](../ansible/templates/playback-agent.Caddyfile.j2),
[proxy system service](../ansible/templates/playback-agent-proxy.service.j2)
and [rollback](../ansible/playbooks/remove-playback-agent.yml) are prepared.
See [operator instructions](./playback-agent-deployment.md). Agent and proxy
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
See [target evidence](./playback-deployment-evidence.md#approved-isolated-target-test-2026-10-15).

**Production read-only rollout (2026-10-17):** after explicit approval, the
production config received stable IDs/labels (`radio_886`, `fm4_radio`,
`oe3_radio`) while names, stream URLs, ports, sinks and gains were preserved.
These were later renamed to channel identities (`stereo_front`,
`stereo_centerlfe`, `stereo_rear`); see
[channel-based identities](./playback-deployment-evidence.md#channel-based-output-identities-2026-10-17).
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
the maximum observed outlier are recorded in the deployment record. S07's
subsequent implementation and production write activation are recorded below.
Writes were disabled at the S06 read-only checkpoint.

### S07. Add durable manual operations and calibration controls

**Done and production-verified (2026-10-17, user sign-off):** typed
play/pause/stop/set-gain submissions, a bounded persistent SQLite ledger and
single worker, idempotency/revision checks, explicit partial/unknown outcomes,
web gateway routes and capability-gated Playback controls. D07/D13 are accepted.
The [S07 operation contract](./mpd-playback-operations-api.md) records request
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
The [deployment record](./playback-deployment-evidence.md#manual-controls-and-tunein-2026-10-17)
records live station changes, contention and playlist-resolution fixes. The
[configuration split rollout](./playback-deployment-evidence.md#configuration-split-rollout-2026-10-17)
records favorites, preserved stations/gains and service restarts.
The [public-only egress plan](./playback-egress-hardening-plan.md) records the
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
URL-type favorite selection and no-default-station behavior still need live
acceptance; their local coverage is not a substitute for target checks.

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

### S10. Implement hardware-driven automation

**Not implemented.** D02, D03, D05, D15 and D16 remain open. Confirm physical
mappings and fresh hardware observations before enabling any automatic policy.

1. Follow accepted D01; resolve D02-D05 and D15-D16, and confirm physical mappings
   before enabling rules.
2. Extend hardware publication with successful per-zone power/source
   observations, timestamps, origin and config generation. Persisted fallback
   and optimistic command state are not fresh measured demand.
3. Enable full hardware polling when automation is enabled even with zero
   browser connections. Keep unsolicited updates plus periodic resnapshot.
4. Add a pure policy evaluator with a fake clock and three-valued
   true/false/unknown demand. Include every configured consumer, including
   hidden zones; false requires complete fresh negative evidence.
5. Apply delay/unknown policy and evaluate outside locks. Send bounded,
   revision-checked targets through the chosen single policy owner.
6. Migrate the coordinated control schema from S04; agent, CLI, supervisor and
   recorder must agree on intention readers/writers. Defaults preserve manual
   control and existing play/pause/stop choices.
7. Add mode/reason display and explicit "Return to automatic". Gain/source
   edits do not implicitly change mode.
8. Implement owner epoch/sequence fencing and receiver-monotonic lease expiry
   for web-originated targets. Never replay obsolete offline commands.
9. During web-host outage, report expired/unknown automation and preserve the
   last effective Pi intention for local recovery. On reconnection require fresh
   hardware observations and fenced ownership before new demand transitions.
   Do not add a Pi-local hardware feed or another policy evaluator.

**Tests:** keypad/API/out-of-band changes, no browsers, shared-source consumers,
partial/stale zones, rapid source handoff, debounce cancellation, manual
override, restart/expired lease and stale owner/sequence.
**Acceptance:** opted-in outputs follow confirmed hardware demand; manual
stop cannot be undone by delayed policy; stale state follows the accepted
outage policy and is visibly degraded.

### S11. Lifecycle acceptance, rollback and operational handover

**Incomplete.** Config-split rollout verified live TuneIn favorites operations,
unchanged admin-config bytes and playback after MPD/supervisor/agent restarts.
Still outstanding: live URL-type favorite and no-default-station checks,
restart persistence of edited favorites/gains/selections, approved Pi reboot
and outage/fault tests, and S10 automation acceptance. Earlier S06 latency
measurements are not a new S08/S09 lifecycle or analog-audio guarantee.

1. Run focused tests for touched components; then full regression/contract
   suites before live rollout. Syntax-check provisioning separately.
2. Measure state/title p95 latency, stale detection, queue bounds and healthy
   output/UI responsiveness with one MPD or agent unavailable.
3. In an approved window test agent restart, web restart/outage, MPD failure,
   source outage, deployment and Pi reboot. Test USB/audio faults only with
   explicit approval and preserve evidence.
4. Verify selected sources, calibration and manual/automatic intentions survive;
   confirm actual routes and both-channel signal. Digital state is not proof
   of analog output, and a 5-second UI target is not an audio dropout guarantee.
5. Exercise rollback using compatible binaries and backed-up schemas. Disable
   automatic policy first; stop new writers before restoring old files.
   Stopping the agent alone does not cancel persisted playback intentions.
6. Update API/deployment/runbook docs with exact startup, health, permissions,
   credentials, backup/restore, logs and limitations.

**Acceptance:** every in-scope gate has evidence; deferred catalog/physical
tests are named; no successful empty fallbacks or unverified guarantees.

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
  in the [deployment record](./playback-deployment-evidence.md#s06-lan-latency-acceptance-2026-10-17).
- D06 transport is deployed with Caddy's approved LAN-subnet `/24` filter. The user
  declined host firewall changes; the deployment record documents the
  trusted-LAN assumption and residual network exposure.
- D07 accepts trusted-environment browser access without login or user
  permissions for now; lightweight admin authentication is deferred.
- D13 accepts a -60 to 0 dB slider with 0.1 dB steps, preserving visible/editable
  out-of-range values without clamping by extending the slider minimum.
  Controls live in a per-stream Config dialog with slider-only gain editing.
  S07 is done, deployed and production-verified (user sign-off, 2026-10-17).
- D02-D03, D05 and D15-D16 remain open; physical input mapping, automation
  demand rule, stale-state policy and timing still need decisions.
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
  station next to the playback state. Superseded by the [configuration split](./config-split-plan.md): the tool was removed, outputs are now keyed by `id` in `config.json` and runtime state lives in `playback.json`.
- The config/state split, optional default station/gain and favorites (maximum
  20) are implemented and deployed. No station means initially silent/stopped,
  with Play visible but disabled; omitted gain means 0 dB. Live no-station and
  URL-favorite acceptance remain pending. O12 is closed by hardcoded loopback
  MPD generation and the verified Pi listeners.

Next: finish the outstanding manual-playback acceptance,
then resolve D02 before implementing S10. Schedule reboot/fault tests explicitly;
do not infer approval from this status update.

After each answer, update its status/outcome above, amend affected architecture
sections/steps and discuss only the next unresolved decision.
