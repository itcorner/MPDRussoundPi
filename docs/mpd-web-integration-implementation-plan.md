# MPD web integration: stepwise implementation plan

Date: 2026-10-14; updated 2026-10-16. Status: S01-S03 implemented; S04
prepared locally; S05 read-only web integration implemented locally. Production
migration and cross-host web integration remain gated.

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
- Support manual stream URLs and a later external source-provider integration.
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
| D08 | Stable output identity | Immutable IDs persisted in Pi operational config; editable labels separate. Preserve legacy names, ports, sinks and systemd units during coordinated migration. |
| D04 | Manual playback precedence | Manual play/pause/stop switches that output to manual mode until explicitly returned to automatic. Accepted 2026-10-16 during S04 preparation; automation is not enabled by this decision. |

### Open decisions, in discussion order

Each row describes one decision. Some can be deferred without blocking
read-only status; the gate column states when the answer becomes necessary.

| ID | Decision | Recommendation / alternatives | Required before |
| --- | --- | --- | --- |
| D02 | Which hardware demand rule should automatic mode use? | Any powered zone selecting the mapped input. Alternative: any powered zone starts all managed outputs. | S10 |
| D03 | Physical input/output mapping and zone scope | Explicit mapping supplied/verified by user; never infer from MPD ports or sink names. | S10 |
| D05 | Behavior when automatic-mode hardware observations become unknown/stale | Hold last effective intention and show degraded automation. Alternative: stop after an agreed grace interval. | S10 |
| D06 | Web-to-agent transport security | **Selected:** Caddy terminates TLS on the Pi and forwards to the loopback agent. Use a self-signed certificate trusted/pinned by the Docker web client through `tls_ca_file`, retain the scoped bearer credential, and firewall ingress to the Docker host. | Agent deployment in S06; write access in S07 |
| D07 | Browser access protection | Existing or new authenticated reverse proxy. Alternatives: application login or explicitly trusted-LAN-only access. | Live write rollout in S07 |
| D09 | Initial source URL scope | Direct HTTP(S) radio feeds only. Alternative: explicitly supported playlist resolution or additional MPD URI schemes. | Source validation in S08 |
| D10 | Permission to adopt/manage MPD queues | Explicit per-output adoption of a one-selected-source managed queue. Alternative: preserve unmanaged entries and expose that limitation. | S08 |
| D11 | Source-switch failure behavior | Keep newly saved desired source visible and retry; explicit user operation restores old selection. Alternative: revision-safe rollback to old source. | S08 |
| D12 | Does source selection change playback intention? | Preserve play/pause/stop and mode; provide separate select-and-play. Alternative: every selection explicitly starts playback. | S08 |
| D13 | Calibration-gain UI range and step | Suggested UI range -60 to 0 dB, step 0.1 dB; finite backend values below the UI range remain valid and visible without clamping. | S07 |
| D14 | First external catalog provider | Choose one provider with MPD-playable radio URLs; defer catalog implementation until chosen. | S09 only |
| D15 | Automatic-mode stop delay | Suggested 30 seconds of continuously confirmed no-demand; alternative immediate stop or another interval. | S10 |
| D16 | Freshness and lease timing | Suggested hardware stale/lease thresholds 30 seconds at 10-second poll/renew cadence; adjust to measured full-poll duration. | S10 |

D01 is resolved: use the web-hosted policy path in S01/S10, with no independent
Pi hardware-state feed or Pi policy evaluator. This availability choice does not
resolve D05: stale hardware observations while the web is running still need
their own policy decision. Web-host loss must preserve the last Pi intention.

D03 is a configuration decision: collect the actual mapping in a separate
focused question after D02, not during the availability question D01.
D06's reverse-proxy and certificate trust model are selected. Certificate,
token and firewall provisioning must be completed and verified before S06;
these deployment actions do not authorize changing the Pi in this local task.
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
If D14 is deferred, mark S09 deferred and S11's catalog acceptance out of scope,
not falsely completed.

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

[playbackmigrate.py](../mpdrussoundpi/russound/playbackmigrate.py) creates an
offline-only private bundle from local config/intention copies and an explicit
name-to-ID/label mapping. It preserves exact originals and every nonidentity
config field, retains legacy intentions, and generates a strictly validated
manual-mode control-v1 preview. Missing, corrupt, partial or unknown intentions
fail rather than defaulting to play. Existing persisted IDs cannot be reassigned.
The tool does not install files, regenerate units, contact MPD or restart services.

The [backend API migration procedure](./mpd-backend-api.md#offline-identity-and-control-preparation-s04)
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
complete; healthy-LAN/Pi latency remains outstanding. No production web
configuration or deployment has been changed; remote Docker-to-Pi use remains
gated on reverse-proxy provisioning, certificate trust setup and firewall
acceptance under D06.

### S06. Provision and verify read-only deployment

**Deployment preparation (2026-10-15; not executed):** separate
[agent-only install](../ansible/playbooks/deploy-playback-agent.yml),
[user service](../ansible/templates/playback-agent.service.j2) and
[rollback](../ansible/playbooks/remove-playback-agent.yml) prepared.
See [operator instructions](./playback-agent-deployment.md). Activation is
opt-in; agent transport remains loopback-only until the D06 reverse proxy is
provisioned. No production IDs are
assigned by these scripts. S06 is not complete: migration, secure transport,
web configuration and approved target acceptance still remain.

**Partial target acceptance (2026-10-15):** user approved deployment with a
separate private observation-test config during endurance. Agent running on
Pi loopback; authenticated reads, real MPD metadata, routes/gains and unchanged
production config/intentions/service lifecycle verified. Existing Radio 88.6
faults continued; no zero-impact or uninterrupted-playback claim.
See [target evidence](./playback-agent-deployment.md#approved-isolated-target-test-2026-10-15).
D06 implementation, production migration and Docker/web reachability remain
outstanding.

1. Extend [Pi provisioning](../ansible/playbooks/setup-musicpi.yml) with a
   separate **agent-only** installation path. Avoid its existing audio/MPD
   restart steps for additive agent deployment.
2. Generate an independent user service under the audio-owning user with
   lingering, private runtime directory, D-Bus/Pulse environment, bounded
   restarts and writable ledger/state locations.
3. Provision Caddy on the Pi with the selected self-signed TLS certificate,
   Docker-side `tls_ca_file` trust, scoped credentials and firewall; configure
   the Docker endpoint/secret wiring without embedding secrets in the
   [web image](../Dockerfile) or browser assets.
4. Fix/test the known MPD binding-schema mismatch in the setup generator before
   claiming loopback isolation. Applying regenerated MPD listeners is a later
   approved maintenance action, not a prerequisite to local fake tests.
5. After endurance completion/approval, back up operational files and capture
   checksums, daemon PIDs, state, queues, routes and gains. Deploy read-only
   agent; compare before/after without starting/stopping production playback.

**Acceptance:** agent authenticated and reachable from Docker; unauthorized
requests rejected; per-output metadata/status verified; calibration/config/
intentions unchanged and no MPD/PipeWire restart. Document actual listeners,
not only intended config. If isolation needs changes, schedule them explicitly.

### S07. Add durable manual operations and calibration controls

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
5. Add web submission/status routes, operation progress/error UX and D07
   authorization. UI play/pause/stop are explicit actions, not toggles.
6. Expose D13 dB slider/numeric input through `set_gain`. Read/persist the same
   `gain_db`; keep existing values outside UI range visible/editable without
   silent clamping. Debounce/supersede unsent slider changes.

**Tests:** lost responses, retries, duplicate payload conflict, ledger restart,
busy lock, stale revisions, queue saturation, stopped/paused gain updates,
persistence/rollback failure and supervisor reapplication of selected gain.
Run existing backend/CLI/supervisor tests alongside operation contract tests.
**Live acceptance:** approved one-output commands preserve other outputs,
route/gain startup gating and saved intentions; restart restores changed gain.

### S08. Add manual source replacement

1. Implement D09 URI validation on both boundaries; redact secrets. Account
   for Pi-side redirects/DNS with egress policy, not web-only checks.
2. Add normalized manual selection and backend mutation under the shared lock.
   Persist configuration/revision atomically; no service/audio regeneration.
3. Implement D10 queue adoption and bounded song-ID-based cleanup. Refuse to
   silently clear unmanaged entries.
4. Implement D11 failure policy and D12 intention semantics. Expose selected
   desired versus actual current source, including pending silent selections.
5. Reuse startup mute/route/gain checks when replacing active playback.
6. Add per-output source editor and explicit operation outcomes to the UI.

**Tests:** repeated same/new selections, no queue accumulation, URL/persistence/
cleanup failure, stop racing a switch, paused/stopped outputs, process restart
mid-switch and preserving other config/outputs.
**Acceptance:** supervisor uses selected source after restart; repeated switches
stay bounded and silent intentions are honored.

### S09. Integrate one external catalog

1. Resolve D14; keep this step deferred if no provider is chosen.
2. Implement one server-side search/details/resolve adapter with timeouts,
   bounded cache/rate limits and normalized provider/item identity.
3. Send only resolved supported sources plus provenance to the existing S08
   operation; do not give providers a separate playback control implementation.
4. Keep credentials private; expose source capability limitations and errors.
   Expiring URLs require a deliberate refresh design before claiming support.
5. Add searchable selection UI and retain manual URL entry.

**Acceptance:** catalog outage does not affect running audio or manual URLs;
unsupported entries rejected, labels escaped and credentials redacted.

### S10. Implement hardware-driven automation

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

- A01, A02, D01, D04 and D08 accepted.
- S01 complete: read contract, component boundaries and fixtures documented.
  S02 complete locally: collector and focused regression acceptance.
  S03 complete locally: authenticated cached HTTP reads and scheduler acceptance.
  S06 agent deployed/tested on loopback using an approved separate config copy;
  full production acceptance remains outstanding. S04 prepared locally with
  offline migration/revision tests; production migration is not executed.
  S05 complete locally with browser smoke checks, eight-session cache
  verification and same-host latency measurements; healthy-LAN/Pi acceptance
  remains unverified.
  Remote connectivity still requires D06 reverse-proxy provisioning and D07/D13
  decisions. Next implementation work package is S07 after S06 transport and
  D07/D13 decisions; Docker-to-agent production connectivity remains gated on
  the proxy, certificate and firewall acceptance.
- D02-D03, D05, D07 and D09-D16 open. No provider, physical mapping or demand rule has
  been silently chosen.
- **Next question: D02, source-specific demand versus system-wide power.**

After each answer, update its status/outcome above, amend affected architecture
sections/steps and discuss only the next unresolved decision.
