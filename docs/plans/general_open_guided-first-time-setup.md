# Analysis and implementation plan: guided first-time setup

Status: **open, implementation started** (2026-11-20). The first G1 foundations
are implemented (see [implementation progress](#implementation-progress)); the
wizard UI, the recoverable config/state transaction and first-install deployment
handling are not. The six-hour observation prerequisite is satisfied (section
12.1). Deployment and changes to existing installations still need a separately
reviewed rollout; this document does not authorize one.

### Design decision summary

The following is the recommended design baseline, not implementation approval.
Responsive setup with memory-only edits on every device (D7/D11) is the
confirmed scope decision from 2026-11-14; it replaces desktop-only setup and
all earlier local draft retention proposals.
Sections 5-8 define the contracts; section 11 separates product approval from
implementation proof still needed.

| ID | Recommended decision | Reason / consequence |
|---|---|---|
| D1 | One configuration model and save pipeline, shared by setup and advanced editor. | Avoid divergent validation, precedence and persistence behavior. |
| D2 | Missing configuration opens setup; invalid/unreadable configuration opens repair guidance. | Never silently replace a broken installation with defaults. |
| D3 | Manual-first, offline-capable core release; no active sample seeding. | Works without multicast or hardware availability; existing files remain untouched. No supported deployment path seeds the example since the legacy playbooks were retired (2026-11-16). |
| D4 | Deployment-managed effective fields are visible but not editable through setup. | Keep runtime precedence honest without persisting environment values. |
| D5 | Require an opaque configuration revision on every supported configuration write before enabling the wizard. **Implemented** (2026-11-20), required immediately without a transition release. | Prevent stale-tab overwrite; the advanced editor is migrated. |
| D6 | Use durable staged config/state recovery unless G0 proves state fully derivable. | Existing state includes runtime values, so discarding it is not assumed safe. |
| D7 | Unsaved edits stay in page memory on every device; no local or server-side draft storage. **Confirmed.** | No retention controls, resume chooser, expiry, draft migrations or device classification. Reload/close may lose unsaved edits. |
| D8 | Saving and hardware verification are independent outcomes. | Offline save can succeed; reachability or reported readiness never proves audible sound. |
| D9 | Explicit unconfigured first-install deployment intent, followed by reviewed configured activation. | No permanent exemption from normal deployment acceptance. |
| D10 | Defer arbitrary draft endpoint probes and mDNS from the core release. | Avoid new destination-policy and multicast dependencies; use shared post-save polling first. |
| D11 | Full guided setup works on desktop, phone and tablet with the same memory-only editing behavior. **Confirmed.** | One responsive workflow and save API; no cross-device sync or separate device-specific storage policy. |

## Implementation progress

| Date | Change | Plan items | Notes |
|---|---|---|---|
| 2026-11-20 | [PR #55](https://github.com/itcorner/MPDRussoundPi/pull/55): the config API may change only `host`, `port`, `device`, `baud` and `poll_interval_seconds`; other backend fields (e.g. `protocol_audit_log_file`) are accepted only unchanged; a changed `device` must be a serial device path. | Partial D4; I3 guard that the API rejects managed-field changes instead of dropping them | Precedes the planned opaque provisioned-device IDs (section 5); raw saved paths are still shown to the editor. |
| 2026-11-20 | [PR #56](https://github.com/itcorner/MPDRussoundPi/pull/56): `GET /api/config` returns `configuration.load_state` (`missing`, `valid`, `invalid`, `unreadable`) and `configuration.revision`. `POST /api/config` requires `X-Config-Revision`: missing returns `428 config_precondition_required`, stale returns `409 config_conflict`, both with `save_outcome: "not-saved"`. The editor sends the revision and keeps edits on conflict. Config/state writes sync file and directory and keep permissions. Config responses are `no-store`. | D5; load states from I1; revision/structured-error part of I3; single-file durability toward I2; image smoke test extended | See deviations below. |

| 2026-11-20 | Config and state store ([config_store.py](../../web/config_store.py)), all three migration steps: the controller reads and writes both files only through the stores; poll commits compare `file_generations()` instead of reloading both files; `config_error`, the per-call path arguments, the global controller and the module-level wrappers are gone. Steady-state reads cost one `stat()` per file (before: up to six config and three state parses per poll). | First step of I2 | Files changed within the last two seconds are compared byte for byte, like git's racy-clean check, so coarse timestamps cannot hide a same-size rewrite. |

**I0 inventory, config/state scope (2026-11-20):** while the web service runs,
it is the only writer of both files (`persist_state` from commands, keypad
updates and polls; config save and its `.invalid-*.bak` copy). Deployment
backup/restore stops the runtime first and requires writer quiescence. The
deployment preflight, native and handover tools only read and validate the
config at deploy time. Operator hand edits are the only non-cooperating writer.
The dummy backend uses its own state file. The full I0 inventory (startup paths,
supported mounts, failure reproductions) is still open.

Deviations from the section 5 design, recorded so later batches build on what exists:

- **No transition release.** The only first-party writer is the configuration
  page, so the revision was required immediately (decision 2026-11-20).
  Section 5 "Compatibility rollout" is resolved accordingly.
- **Revision binding.** The token is an HMAC (process-random key) over the
  resolved config path and the exact bytes or absence. It does not separately
  bind effective connection constraints: environment overrides only change on a
  process restart, which rotates the key and invalidates every open editor.
- **Metadata is minimal.** Only `load_state` and `revision` are returned;
  `save_allowed`, `blocked_reason`, `topology_status` and the `setup` block are
  not implemented. Invalid and unreadable files are still offered the existing
  replace-with-backup save; D2 repair guidance belongs to I5.
- **Persistence is per file, not a transaction.** State is still written before
  config, each atomically and durably; there is no pending record, cooperating
  cross-process lock or startup reconciliation yet (I2).

Next: the rest of I0 (startup paths, supported mounts, failure reproductions),
then I1 candidate extraction and preview, then the I2 transaction protocol inside
the [config and state store](#config-and-state-store-first-step-of-i2).

## 1. Goal and completion criteria

Guide an operator from an unconfigured web application to an explicitly saved,
understandable configuration without requiring SSH for ordinary room/input setup.
Keep deployment-managed networking, credentials and hardware provisioning outside
the browser's control. Reuse the existing configuration model and advanced editor,
not a second wizard-owned configuration.

The initial release is complete when an operator can:

1. Start a supported new installation with no application configuration and no
   example rooms silently activated.
2. See the effective connection and its source; change only permitted saved fields.
3. Explicitly describe controllers, enabled zones and inputs without implying
   that placeholders or user declarations were discovered or verified.
4. Optionally map inputs to already provisioned playback agent/output identities.
5. Review and safely save once, including while hardware is offline.
6. Retain unsaved edits during the open-page journey on every device,
   cancel without changing the installation, and revisit an existing installation
   without losing unknown configuration fields.
7. Distinguish saved configuration, reachable gateway, responding controller,
   authenticated agent and reported audio readiness. Never equate these with
   audible playback.

Automatic gateway suggestions are a separately gated extension in this plan.
Manual endpoint entry must work without multicast, Internet access or discovery
dependencies. SSH/admin work may still be necessary to provision a deployment,
serial device, credentials, proxy trust or writable directories; the wizard is
not an installer.

Guided setup works in desktop, phone and tablet browsers. Operators configure
the application running on the Pi/server; this does not install the application
on their device. Mobile setup supports all steps and explicit server save, but
does not persist drafts locally on any device or synchronize them across devices. Ordinary
mobile rooms, playback and status remain unchanged.

## 2. Source-grounded current-state analysis

These are source observations, not claims that new behavior has been implemented.

| Surface | Current behavior | Consequence for guided setup |
|---|---|---|
| [Configuration controller](../../web/russound_controller.py), `load_config` / `build_config_editor_payload` | Missing, invalid and unreadable files all lead to `config_required`; editor proposes controller 1 with six zone slots and six disabled sources. `configuration.load_state` now distinguishes the four cases (2026-11-20), but the editor does not use it yet. `config_writable` currently means a config path exists, not that a save will succeed. | Use the load states for neutral unconfirmed topology, and honest capability/error reporting. Do not treat all errors as a new installation. |
| [Configuration UI](../../web/static/config.html) and [editor logic](../../web/static/config.js) | Connection, controllers, zones, inputs, shortcuts, playback selections, validation and unsaved-navigation guards already exist. Drafts are held in page memory; save sends the whole editable payload. Sound controls can issue hardware writes. | Extract/reuse editor state and field validation. Wizard mode must exclude sound/write controls and preserve edits across step transitions. |
| [Configuration API](../../web/russound_server.py), `api_update_config` | Save runs under a process-local state lock and requires a matching `X-Config-Revision` (`428` missing, `409` stale; 2026-11-20). Endpoint/audit changes trigger backend reconnection; successful saves request polling. | Reuse the revision for preview/commit; distinguish explicit save from later verification. Saving can cause automatic read-only polling, not just disk writes. |
| [Persistence](../../web/russound_controller.py), `update_config_zones` / `_persist_json_file` | State is persisted before config. Each file is replaced atomically with file and directory sync and kept permissions (2026-11-20). Invalid-file replacement retains a synced backup. | Still not a crash-safe config/state transaction. Safe persistence is a prerequisite, not a feature to duplicate inside the wizard. |
| [Endpoint resolver](../../web/config_types.py) and editor payload | Valid host/port environment overrides apply independently. Invalid port overrides are ignored by runtime but can still appear in the raw override map. An override can turn saved serial mode into TCP. | Share typed effective/source/validity metadata with runtime; never implement precedence again in JavaScript or copy overrides into saved values. |
| [Backend status](../../web/russound_controller.py), `_backend_status` | Can use cached poll results or fall back to opening a gateway connection. `connected` alone does not establish a fresh physical-controller response. | Wizard cannot label current `connected: true` as hardware verified. Require timestamped provenance for stronger claims. |
| [Playback editor adapter](../../web/russound_server.py), `_with_playback_editor`, and [bindings](../../web/config_types.py) | Exposes output availability and stable `agent_id` / `output_id`; configured agents/credentials are external inputs. | Reuse inventory/mappings; no pairing, agent installation, URL/token/CA editing or credentials export in setup. |
| Example seeding | The legacy Docker and native web playbooks that copied [`config_example.json`](../../web/config_example.json) into a missing config were retired on 2026-11-16 (`e55d9de`). No supported deployment path seeds it now. The Rooms page empty state still tells operators to copy the example. | D3 holds for supported paths. Replace the Rooms hint with a link to setup; never remove existing configs or classify samples by room names. |
| [Image smoke test](../../tool/ci/smoke-web-image.sh) | Already verifies empty-volume setup, rejected invalid input and a valid offline save on amd64/ARM64. | Retain and expand this real behavior instead of requiring hardware to finish setup. |
| [Reviewed deployment adapter](../../tool/deployment_compose.py) | Preflight reads/validates config; controller acceptance requires zones. Runtime checks require configured state. | New guided installs need an explicit unconfigured first-install intent, receipt and later activation check. Existing strict configured deployments must remain strict. |
| [HTTP guards](../../web/russound_server.py), `_guard_requests` | Existing host/origin/JSON checks protect API surfaces; they are not an administrator login or ownership-claim mechanism. | Reuse guards; document trusted-network/reverse-proxy access requirements. Do not advertise safe public unauthenticated onboarding. |

### Existing tests to extend

- [Controller tests](../../tests/test_russound_controller.py): invalid config,
  initial offline save, rejected endpoint and state-write failure.
- [Server tests](../../tests/test_russound_server.py): setup routes, actionable
  write failure, reconnect behavior and API disclosure.
- [Playback tests](../../tests/test_playback_status.py) and
  [input mapping tests](../../tests/test_input_playback.py): stable identities,
  partial availability and status handling.
- [Profile tests](../../tests/test_container_profiles.py),
  [operator tests](../../tests/test_deployment_operator.py) and the image smoke
  test: generated layout, deployment acceptance and real empty-volume startup.
- Existing [Node UI tests](../../tests/test_rooms_ui.js) provide a lightweight
  frontend-test pattern; add focused setup/editor tests rather than introducing
  a frontend framework solely for the wizard.

## 3. Scope and recommended product decisions

These are recommendations for approval before implementation, not previously
agreed behavior. Only material departures need a new design round.

| Decision | Recommended first-release contract |
|---|---|
| New installations | Do not seed an active sample. Missing config opens setup; examples remain documentation or an explicit import action with confirmation. Import never means hardware verification. |
| Existing installations | Valid config continues unchanged. Offer **Review setup** explicitly; no forced redirect, reset or inferred first-run status from sample names/hashes. |
| Invalid/unreadable config | Show a repair/error state, not an empty first-run draft. Offer deliberate replacement of readable invalid config with a retained private backup; do not overwrite unreadable files or repair permissions automatically. |
| Layouts | Docker web with external or managed gateway; native TCP and existing serial access; explicitly identified dummy/offline mode. No privileged browser provisioning or automatic layout migration. |
| Offline completion | Allow valid save with explicit **Save without hardware verification**. Zero enabled zones is permitted as incomplete topology, matching current offline capability, never shown as usable rooms/hardware acceptance. |
| Unsaved edits | Page memory only on every device, with a clear reload/close loss warning. No saved drafts, retention settings, resume flow or cross-device sync. |
| Playback | Optional mapping of already configured outputs; skip is normal. Retain offline/unresolved bindings and never silently choose a similarly named replacement. |
| Gateway discovery | Manual-first release; optional mDNS milestone afterward. Discovery failure never prevents manual setup. |
| Save authority | One shared conditional-save pipeline for wizard and editor; no separate unsafe wizard write path. Preconditions are required on `POST /api/config` since 2026-11-20. |
| Remote access | Existing trusted deployment access boundary only. Internet exposure, authentication/claiming and first-admin enrollment require a separate access-control design. |
| Setup client | **Confirmed:** full responsive setup on desktop/phone/tablet. No local draft storage on any device or transfer between devices; existing runtime mobile UX is unchanged. |

### Explicitly excluded

No automatic pairing, credential creation/rotation, file-path disclosure, package
installation, Docker/systemd operations, host-network changes, firmware changes,
serial scanning, power/source/volume commands, playback start/stop, or automatic
rollback. No controller/zone-count autodetection claim without a separately
implemented and validated protocol capability.

## 4. Proposed user journey

### A. Welcome / choose intent

- Missing config: explain what setup can configure and what must already exist.
- Valid config: show **Review existing configuration**, not “start over.”
- Invalid or inaccessible config: show specific repair guidance; retain drafts
  and original files, with no save masquerading as recovery.
- Explain demo/offline status where explicitly known. Unconfirmed defaults are
  proposals, not evidence. Never start discovery or active probes on page load.
- Explain memory-only editing and possible loss on reload/close with one short
  notice; do not ask operators to choose retention or storage settings.

### B. Connection and deployment constraints

Show saved and effective endpoint/mode distinctly. Fields whose effective values
are deployment-managed are read-only with visible source badges and accessible
instructions; independently editable fields remain editable. Reject invalid
override metadata as inactive/warning, not a fake effective value.

Manual TCP endpoint entry is always available when not managed. Native serial
setup selects only an explicitly provisioned device; no directory listing or
device enumeration API. Preserve hidden saved serial/TCP values during unrelated
edits according to the shared effective-config contract.

If the optional draft-probe capability is enabled, offer a deliberate
**Check gateway reachability** only after explaining that a
TCP connection is network activity and may open a serial bridge. No serial
device open/check while another owner uses it. No hardware commands in this step.
The core release instead explains that connection evidence appears after save.

### C. Controllers, rooms and inputs

Ask for controller IDs and zone counts within existing supported limits. Require
confirmation of any proposed topology; default room/source labels remain plainly
editable placeholders. Add/enable/name zones and inputs using existing address
identities and validators. Review removals, hidden zones, input bindings and
shortcut effects when revisiting a configuration. Do not silently drop fields
outside the wizard's scope.

### D. Optional playback mapping

List known agent/output labels, stable IDs, availability and observation age.
Display absent/stale/error states per agent; preserve existing unavailable
selections. Mapping expresses the operator's physical wiring assumption, not
proof that a cable/input is correct. Missing agent configuration leads to
deployment guidance or **Skip**, never a request for token/CA paths.

### E. Review and explicit save

Show configuration diff and impact: first creation, endpoint/mode changes,
backend reconnection/polling, removed zones/input bindings and shortcut pruning.
Show deployment-managed fields without adding them to the persisted patch.
Offer **Save and observe status** or **Save without hardware verification**.
Both save configuration; neither authorizes state-changing hardware operations.

### F. Results and revisit

Show separate `saved`, `gateway reachable`, `controller responding`,
`agent authenticated`, `audio route/readiness reported` results with timestamp
and provenance. Unknown/stale/skipped are not passes. Physical responses may
remain unverified after successful save. Link to the current editor/status pages;
the future diagnostics feature is not a prerequisite for useful error messages.

## 5. Shared backend and persistence contract

### Configuration state and effective values

Extend `GET /api/config` additively, preserving existing saved `config`, slots
and `backend_overrides` until their compatibility transition is approved:

- Configuration-specific opaque revision, independent of SSE/playback
  revisions; exact missing-file state gets an explicit precondition too.
- Explicit load state: `missing`, `valid`, `invalid`, `unreadable`. Setup
  completion is not inferred solely from non-null JSON.
- Allowlisted effective endpoint fields with value, source, validity,
  editability and actionable diagnostics from the same resolver runtime uses.
- Actual setup/save capability and diagnostic codes, not a guarantee inferred
  merely from a non-null path. Save errors remain authoritative.
- Explicit deployment setup intent/capability where available; do not infer
  managed gateway/dummy mode solely from hostnames or example contents.

Do not return arbitrary environment variables, secret-file paths, credentials,
raw exception paths or arbitrary deployment internals through new metadata.
Preserve unknown on-disk fields through shared merge logic; expose only fields
needed for editing. Notes currently omitted from API responses must stay private.

### Pure candidate preparation and review

Extract candidate normalization/validation/diff from `update_config_zones`
before adding a wizard. Reuse it for editor and setup. Preparation must not
persist files, reconnect, poll devices or send playback commands.

Propose `POST /api/config/preview` with a revision precondition and the existing
editable payload shape. Return normalized editable values, validation diagnostics
and impact, without revealing private fields. The final save must recompute under
the write lock against the same revision; a preview digest is not a reusable
authorization token or a substitute for revalidation.

Reuse `POST /api/config` for final commit rather than creating a parallel setup
save endpoint. Proposed HTTP contract:

| Condition | Result |
|---|---|
| Invalid candidate | `400`, structured actionable field errors; files unchanged |
| Stale `X-Config-Revision` | `409` with `config_conflict`, retain browser draft; reload/compare/reapply explicitly |
| Missing required precondition after compatibility transition | `428`; never silently overwrite |
| Storage failure or uncertain prior transaction | Explicit failure/blocked recovery status; no success-shaped response |
| Durable successful save | Updated revision/config metadata; connectivity verification reported separately |

No automatic POST retry on timeout. A client reloads revision/status to reconcile
an uncertain save; do not send the same whole snapshot again over newer edits.

### Concrete API and revision design

Keep the current editable request fields (`backend`, `controllers`,
`zone_slots`, `source_slots`, `shortcuts`). Do not accept an arbitrary on-disk
config object. Add metadata alongside the existing response during migration:

```json
{
  "configuration": {
    "load_state": "missing",
    "revision": "opaque-server-issued-token",
    "save_allowed": true,
    "blocked_reason": null,
    "topology_status": "incomplete"
  },
  "setup": {
    "contract_version": 1,
    "intent": "unconfigured-first-install",
    "draft_probe_available": false,
    "discovery_available": false
  }
}
```

This is the additive metadata fragment, not a complete response or a promise
that a filesystem write will succeed. `intent` is `null` when no trustworthy
deployment declaration exists. Missing config alone does not grant deployment
authority. Capability denial includes a stable reason code and safe user message.

- Both preview and commit require `X-Config-Revision`. A missing-file token is
  bound to absence, not a universal create permission. A concurrently created
  file therefore conflicts.
- Bind the token to target identity, exact config bytes or absence, and the
  effective connection constraints used for review. Private/unknown-field-only
  edits must also conflict. Do not bind it to playback snapshots, poll counters
  or mutable state-file contents.
- Use a keyed digest with a process-lifetime private random key; do not expose
  an unkeyed fingerprint of potentially private configuration bytes. Restart
  invalidates tokens deliberately and requires draft re-review. The initial
  supported web topology is one application process per config target; multiple
  web workers require a shared revision authority before support is claimed.
  CLI writers use the same locked persistence service, not browser tokens.
- Use the application-specific header rather than claiming the token is a
  strong HTTP ETag for the current mixed config/live-playback representation.
  This supersedes the earlier `If-Match`/`412` proposal. Responses use
  `Cache-Control: no-store`; tokens are concurrency guards, not authentication.
- Preview returns `base_revision`, normalized editable values, field errors,
  warnings and an ordered impact list. Each impact has a stable code, affected
  editable identity and severity. No raw private-field diff is returned.
- Commit returns `saved: true`, the new revision and `activation_status`
  (`pending`, `active`, or `failed`). Here activation means application backend
  reconfiguration, not deployment approval or physical-controller verification.
  A post-commit reconnect failure never changes a durable save into “not saved.”
- Errors use a shared envelope: `code`, safe `message`, optional `field_errors`
  with field path/code/message, and `save_outcome` (`not-saved` or `unknown`).
  Retain the existing top-level `error` string while migrating clients.
  Conflicts are `409`, missing preconditions `428`, invalid candidates `400`,
  known storage failures `500`, and unresolved recovery blocks writes with
  `503`. No absolute filesystem paths in new public errors.
- Do not add a generic “force save” button or accept `*` as a revision.
  Invalid-file replacement requires a separately confirmed repair intent,
  current revision and durable private backup. First-release wizard only links
  to repair guidance; it does not implement that replacement action.

For each connection field, return an allowlisted record with `saved_value`,
`effective_value`, `source` (`saved`, `environment`, `default`, or `none`),
`editable` and diagnostics. Do not return an ignored raw override as the effective
value. An invalid port override produces an inactive-override warning while the
valid fallback remains visible. The server resolver, not JavaScript, decides
whether host-only/port-only overrides turn saved serial mode into TCP.

Treat serial device paths as a special case: show an opaque provisioned-device
ID and safe administrator-supplied label, not a filesystem browsing API or a raw
path in new metadata. A deployment with no provisioned selectable devices can
preserve its existing serial setting but cannot invent another through setup.
The shared candidate builder rejects attempted changes to managed fields rather
than silently dropping them; unrelated fields and inactive saved values survive.

### Setup and browser state machines

Keep independent state axes instead of one misleading `setup_complete` flag:

| Axis | States / authority |
|---|---|
| File load | `missing`, `valid`, `invalid`, `unreadable`; server filesystem reader |
| Persistence | `ready`, `recovering`, `blocked`; locked persistence service |
| Topology | `incomplete`, `declared`; validated saved fields, not hardware discovery |
| Evidence | `unknown`, `pending`, `pass`, `fail`, `stale`, `skipped`; per-check observation |
| Browser | `editing`, `previewing`, `reviewed`, `saving`, `saved`, `conflict`, `outcome-unknown`; local UI |

Only missing + ready auto-enters setup. Valid incomplete configurations show a
non-blocking **Continue setup** action; do not force the operator back into a
wizard. Invalid/unreadable or recovery-blocked states prohibit normal saves.
Independent read-only status pages remain available with honest degraded health.

Any edit after preview returns the browser to `editing` and invalidates the
review. Next validates only the current step locally; server preview validates
the whole candidate. Direct step links cannot bypass required confirmations.
Changing controller topology preserves draft values for addresses still present;
removed addresses and dependent shortcuts/bindings are listed before save.
Cancel leaves the running installation untouched.

For a timeout, enter `outcome-unknown`, retain the draft and disable repeat save.
Reload authoritative config and compare its editable projection with the
normalized reviewed candidate: equal values mean “current configuration matches
your draft,” not proof that this request committed it. Different values require
conflict review. An unavailable server leaves the result unknown.

On conflict, offer load-latest/discard or explicit reapply with comparison.
Show base, draft and latest values only for allowlisted fields. No automatic
three-way merge in the first release, especially for topology/shortcut arrays.
Every reapplication requires a new preview and explicit save.

### Safe-save dependency

Implement the necessary subset of
[safe configuration changes](./web_open_safe-configuration-changes.md) first:

1. Enumerate config/state writers, including polling, CLI helpers, both editors
   and external files. Reproduce stale-editor and failure-between-writes cases.
2. Check exact config identity/revision inside a shared cooperating-writer lock;
   never treat a process-only lock as coordination with other writers.
3. Build adjusted state from the latest runtime state under the appropriate
   locks, not from stale browser state. Use fixed lock ordering.
4. Select and document a recoverable config/state protocol. Recommended direction:
   private durable staging plus a small pending transaction record, with file and
   parent-directory sync and startup reconciliation before runtime consumers.
   Do not claim two renames are atomic. If state can instead be proven wholly
   derivable without losing intent, document/test that simpler design before
   selecting it.
5. Preserve permissions, ownership, private invalid-file backups and unknown
   fields. Never automatically roll back over newer edits or overwrite ambiguous
   external changes; leave an explicit recoverable blocked state.
6. Cover all app-owned writers. Non-cooperating external editors cannot be made
   safe by advisory locks alone: require offline maintenance or a supported write
   tool and detect changed files where possible; state this limitation honestly.

Before shipping, decide legacy unconditional API behavior: preferably migrate
first-party clients and document a bounded compatibility/deprecation release,
then require preconditions on all configuration writes. The wizard cannot be
declared safe while another supported unconditional path bypasses this protection.
Coordinate metadata with [effective overrides](./web_done_effective-config-overrides.md);
do not wait for unrelated S3 logging work or the entire diagnostics project.

### Recoverable persistence protocol

Recommended baseline for G1; G0 must verify every writer and supported filesystem
before this becomes an implementation commitment. Configuration is authoritative
topology; state retains latest runtime values for surviving addresses. New zones
receive existing normalization defaults; removed zones disappear only through the
reviewed candidate. No browser state snapshot is persisted.

1. Acquire the per-target cooperating-writer filesystem lock, then the
   process-local state lock. All polling, control and CLI persistence paths must
   use this order. Keep the lock file stable; do not lock a file replaced by
   rename. Do not hold these locks across network calls.
2. Read current config and latest state under the locks, check the revision,
   prepare/validate both candidates and capture old identities. Reject symlinks,
   unsupported ownership changes or unsupported mount semantics explicitly.
   Never switch to a non-atomic in-place write for a bind-mounted single file.
3. Stage each candidate beside its own destination, including when config and
   state use separate filesystems. Preserve existing required owner/mode and
   use private defaults for first creation. Flush and sync both files before
   publishing a private pending record in the config directory.
4. The durable pending record contains a protocol version, transaction ID,
   fixed-target identities, old/new fingerprints, staged-file basenames and
   required metadata. It is not a user-supplied path manifest. Sync its file and
   directory before replacing either destination; keep staged recovery copies
   until both destination replacements and parent-directory syncs succeed.
5. Replace config, then adjusted state; sync each affected parent directory.
   Install from separate temporary copies so replacing a destination never
   consumes its retained recovery copy.
   App-owned readers cannot observe the intermediate pair because they use the
   same coordination boundary. External readers are not promised atomic
   two-file visibility.
6. Remove the pending record and sync its directory before acknowledging durable
   success. Only then publish in-memory config and request backend activation
   outside the write locks. Recovery copies can be removed afterward; never
   hide cleanup failures. Once commit is durable, report `saved: true` with a
   safe cleanup warning rather than suggesting the operator should retry save.

Reconciliation runs before polling, commands or further saves, both at startup
and after an interrupted in-process commit:

| Observed destinations | Required action |
|---|---|
| No pending record | Read normally; delete only provably orphaned service-owned staging files under the lock. |
| Both match recorded old versions/absence | Validate staged copies, then roll forward to the reviewed new pair. |
| One old and one new | Validate remaining staged data, then finish the same transaction. |
| Both match new versions | Finish durability/record cleanup; do not rewrite runtime values unnecessarily. |
| Unexpected bytes/metadata, corrupt record or missing required staged data | Block writes and hardware activity; expose repair guidance. Never guess or overwrite later external edits. |

An error before the pending record is durable leaves destinations unchanged.
An error afterward may have an uncertain outcome and requires reconciliation;
do not perform an automatic rollback. A pending record is a commit-to-roll-forward
decision, including if acknowledgement was lost.

Newer legitimate state updates cannot occur until pending recovery finishes.
Cooperative readers/writers must reopen files after lock acquisition; cached
config cannot bypass recovery. Non-cooperating external editors remain an offline
maintenance operation. G0 must prove no startup/control path escapes this gate.
Local filesystems with working rename/file/directory sync are the initial
durability boundary; network filesystems and unsupported single-file mounts
need explicit rejection/documentation, not an unverified power-loss guarantee.

### Config and state store (first step of I2)

**Implemented 2026-11-20** as described below; the measurements describe the
code before the change.

Measured on 2026-11-20 with six zones, the web application re-reads and
re-validates the JSON files on every access: `GET /api/rooms` reads the config
twice and the state once; one unchanged hardware poll (every 2 s while a browser
is open) reads the config six times and the state three times; one volume
command reads them four and two times. That is cheap on a Pi, but no component
owns the current configuration: concurrent changes are detected by comparing
whole dictionaries, errors travel through `config_error`, the state is matched to
the config on every read, and controller methods accept per-call path overrides
for a global controller. The I2 transaction needs a single owner instead.

Introduce `web/config_store.py` before the transaction protocol:

- `ConfigStore.current()` returns an immutable snapshot with `load_state`,
  validated `config`, `error`, `revision`, a `generation` counter and the file
  signature `(st_ino, st_size, st_mtime_ns)`. Each access costs one `stat()`;
  the file is reparsed only when the signature changes, so hand edits are still
  detected and the revision still follows the exact bytes.
- `ConfigStore.replace(config, expected_revision)` checks the revision, writes
  durably and updates the cached snapshot.
- `StateStore` keeps `russound_state.json` in memory: it is loaded once, written
  through on change and, unlike the config, written only by this process. The
  state matched to the configuration is recomputed only when the config
  generation changes.

Migrate in behavior-preserving steps: (1) `load_config()` and `load_state()`
read from the stores with unchanged signatures; (2) generation comparisons
replace whole-dictionary comparisons in `_commit_backend_refresh` and the save
path, and `config_error` goes away; (3) the per-call path arguments, the global
controller and the test-only module-level wrappers are removed. The pending
transaction record, cross-process lock and startup reconciliation are then added
inside the stores rather than around them.

### Compatibility rollout

**Resolved 2026-11-20:** the revision is required on `POST /api/config` from the
first release that introduced it; no unconditional transition release was
shipped. The advanced editor and the image smoke test send it. External scripts
must fetch `GET /api/config` first and send `configuration.revision`.

Remaining before enabling guided setup: migrate all app-owned file writers to
the shared persistence service (I2) and add preview/structured field errors (I3).
Never add a wizard-only opt-out or a permanent bypass flag.

## 6. Simple responsive editing and accessibility

- Implement one responsive wizard with the same steps, validation, preview,
  conflict handling and conditional server save on desktop, phone and tablet.
  Stack fields/cards on narrow screens and reflow comparisons into labeled
  groups without hiding values or actions.
- Use one shared in-memory editor model for editable values, base revision,
  current step and confirmations. Preserve it across Back/Next, preview,
  validation errors, failed saves and conflict review while the page stays alive.
  Keep credentials, private paths and arbitrary raw config outside this model.
- No setup draft reads/writes through `sessionStorage`, `localStorage`,
  IndexedDB, cookies, URL parameters, browser history state or server storage.
  Do not add a persistence adapter, draft schema/migrations, target namespace,
  retention timer, chooser, device classifier or cross-device handoff.
- Show one concise notice: **Changes are saved only when you finish setup.
  Reloading or closing this page may lose unsaved changes.** No **Save draft**,
  **Remember this device**, resume or retention controls on any device.
- Explicit configuration save persists to the server normally. Starting again
  loads the latest saved configuration, not a local draft. Before first save,
  a reopened setup begins fresh; no promise of recovery after browser eviction.
- Back/Next never saves. Confirm discard when leaving a dirty wizard through
  application navigation or Cancel; use the browser's standard unsaved-change
  guard where supported. Mobile browsers may terminate pages without prompting,
  so the notice must not promise a guaranteed warning or recovery.
- Advanced settings remain reachable through a guarded transition. Preserve
  edits only if both views share the same live model; otherwise confirm discard
  rather than silently saving or introducing temporary persistent storage.
- Resize, orientation, virtual keyboard and zoom preserve in-memory edits.
  Support keyboard, screen readers, visible focus, field-error summaries,
  non-color indicators, at least 44 CSS-pixel touch controls and no page-level
  horizontal scrolling at 320 CSS pixels. Focus the new heading/error summary.
- Generated mockups are visual references only: remove draft retention checkboxes,
  **Save draft** actions and the entire **Resume a draft** scenario. Keep the
  responsive journey, explicit save, conflict comparison and truthful results.

Here and elsewhere, “draft” means the current unsaved in-memory candidate only;
it does not imply a stored or resumable draft.

## 7. Diagnostics and gateway discovery boundaries

The core G4 deliverable is provenance/freshness and post-save shared status.
Draft endpoint probing and mDNS are disabled capabilities until their separate
policies and tests pass; neither blocks core guided setup acceptance.

### Connectivity and verification

Start with existing cached runtime observations, adding source/time/freshness
metadata where needed. A manual draft TCP test needs a bounded same-origin POST:
validate a single operator-confirmed TCP host/port, apply existing access guards,
cap DNS/connect/total duration and concurrency, and share active work across tabs.
No URL fetching, shell interpolation, arbitrary protocols or subnet scanning.
Allow legitimate private LAN gateways, but explicitly define which loopback,
link-local and metadata/control-plane destinations are permitted before exposing
an arbitrary destination probe. Do not reuse public-only playback egress policy
blindly for a LAN serial gateway.

Read-only protocol queries still talk to hardware. Initial release should rely
on the active backend's existing shared polling after explicit save rather than
opening a second draft controller session. A later manual protocol check needs
an allowlisted query set, ownership arbitration, maximum load/timeouts and explicit
consent. No probe against unselected discovery candidates. Status must distinguish:

| Evidence | Allowed wording |
|---|---|
| Announcement or user-entered address | Suggested / selected, not checked |
| Successful TCP open | Gateway reachable, controller unverified |
| Fresh valid controller poll reply | Controller responded for the queried scope |
| Valid configured agent TLS/auth and fresh inventory | Agent authenticated; outputs reported |
| Route/readiness observations | Reported ready/correct, not “audio audible” |

Each observation carries check kind, scope, source, backend generation or stable
agent/output identity, observation time, age, freshness limit and safe diagnostic
code. Discard old-generation replies after endpoint changes. “Fresh” requires a
completed relevant operation, not a recent status request. A cached TCP result
cannot be promoted to a protocol response.

Use the existing configured observation interval and stale threshold where
available. For new evidence without such a policy, propose a freshness limit of
twice its configured collection interval, clamped to 15-120 seconds; G4 tests
must assert the actual chosen thresholds. If age exceeds that limit, show stale
with the last outcome, never green pass. Measure age with a monotonic clock
within a process and mark persisted/restarted observations unknown until renewed.
The results page subscribes to existing shared status; it must not create one
hardware polling loop per browser tab. Offline save skips deliberate checks,
but clearly warns that normal configured backend polling still runs after save.

### Optional mDNS milestone

No mDNS advertisement/browser implementation was identified in the inspected
application/deployment surfaces. Specify interoperability before adding packages:

- Proposed private service type `_russound-gw._tcp.local.`; confirm naming and
  supported platform/library behavior before adoption.
- DNS-SD SRV carries endpoint/port; minimal versioned TXT carries protocol
  capability and opaque gateway identity only. No credentials, filesystem
  paths, topology claims, controller/room names or trusted verification flags.
- Advertiser belongs to the gateway host/explicitly managed gateway deployment,
  not the browser or a generic web container. Define start/stop ownership,
  duplicate identity handling, TTL expiry and interface selection.
- Suggested initial bounds: opt-in browse up to five seconds, at most 20
  deduplicated results, one shared browse per process and a 60-second refresh
  cooldown. Apply strict response/TXT size and label limits; do not trust TXT
  to override SRV, explicit endpoint values or access policy.
- Multiple results require explicit selection; one result is still only a
  suggestion. Removed/expired records become unavailable; never switch an
  already selected or saved endpoint automatically.
- Explicit environment/deployment/saved endpoints suppress automatic suggestion
  application. Discovery may be invoked deliberately to review alternatives
  only where fields are editable.
- Multicast normally does not traverse VLANs or ordinary Docker bridges.
  Native same-link discovery can be supported first; bridged Docker must retain
  manual entry unless a separately approved advertisement/browse transport is
  proven. No automatic host networking, firewall changes, Docker socket access,
  privileged discovery container or host-daemon installation.
- A disabled/unavailable discovery capability returns that state explicitly.
  Offline/manual setup remains fully usable.

## 8. Deployment and compatibility integration

New-install deployment requires more than removing example copying:

1. Add an explicit reviewed **unconfigured first-install** intent to the existing
   deployment contract, separate from configured apply/adoption.
2. Permit absent config only for that intent and only with explicitly reviewed
   prepared data directories, listener, gateway ownership and acceptance scope.
   Retain state reservation, backup/absence recovery and image verification.
3. Its runtime receipt checks running/healthy web, setup API/UI availability,
   configured gateway service where managed and unchanged unrelated services.
   It must report configuration/controller/playback acceptance as
   `not-configured`/`not-requested`, not pass fabricated hardware checks.
4. Do not weaken existing configured updates/adoption: missing/invalid config
   there remains an error. Subsequent setup save/activation needs a documented
   transition so reruns do not stay permanently in an unconfigured exemption.
5. Change sample seeding only on new installs in supported entry points.
   Existing example-derived/user files are untouched; unreviewed legacy paths
   remain explicitly marked until migrated/deprecated.
6. Container startup must remain non-root with valid empty writable mounts;
   native serial mode uses pre-provisioned access only. Wizard cannot repair
   mount ownership or create secrets/agents.

The browser never invokes the deployment operator. Application configuration
saves remain normal runtime changes, not deployment operations, but any reviewed
deployment signature/input rules affected by setup must be reconciled explicitly.

### First-install activation transition

Use the existing deployment operation/state model; do not let the web process
write deployment receipts or access privileged operator files.

| Deployment lifecycle | Allowed behavior |
|---|---|
| Reviewed absent-config installation | Record the unconfigured intent and exact permitted absence; perform normal image, ownership, reservation and absence-recovery checks. |
| Accepted unconfigured runtime | Setup UI/API healthy; config/controller/audio checks explicitly not configured/requested. Same-layout unchanged rerun may validate this recorded state while config is still absent. |
| Browser saves valid config | Application becomes configured; deployment receipt remains historical and does not silently acquire hardware acceptance. |
| Next operator run sees config where absence was recorded | Stop for a newly reviewed configured activation. Do not continue under the missing-config exemption or classify the change as automatic corruption recovery. |
| Reviewed configured activation succeeds | Commit a configured receipt with its explicitly requested checks. All future operations use ordinary configured rules. |

If activation is offline/incomplete, permit only the existing explicitly selected
configured acceptance scope; do not fabricate controller success. Config removal
after configured activation is an error, never a return to first-run mode.
Capture a normal verified backup before any subsequent mutating deployment;
do not reuse the original absence recovery to erase browser-created config.

Core acceptance uses managed/external TCP Docker and provisioned native TCP/
serial layouts. Dummy mode must stay visibly demo-only. Optional discovery never
changes network mode, serial ownership or the deployment lifecycle.

## 9. Implementation work packages and dependencies

Implement in small reviewable changes. Each gate must pass before enabling its
consumer; no live Pi cutover is implied.

| Stage | Deliverable / principal surfaces | Dependencies | Exit evidence |
|---|---|---|---|
| G0: contract and reproductions | Approve remaining decisions in section 3; inventory writers and supported layouts; reproduce stale save, partial save, first-run ambiguity and override mismatch. | None | Executable repros; chosen API transition and transaction protocol recorded; confirmed memory-only UI scope retained. |
| G1: shared resolution and save foundations | Shared effective endpoint metadata; pure candidate builder; config revisions, conditional commit and recoverable config/state persistence in controller/server. Update both existing editor and API docs. | G0 | Conflict/fault-injection tests; no silent external change loss; saved serial/unknown/private fields preserved. |
| G2: truthful first-install deployment | Explicit unconfigured intent, no new sample seeding, configured-vs-unconfigured receipts and later activation semantics. | G0; G1 before setup activation | Empty-mount Docker/native startup, strict configured-mode regression, first-install backup/absence recovery. |
| G3: guided UI | Responsive shared in-memory editor and simple step flow; optional playback mappings; preview/diff and accessible conflict handling. No persistent drafts. | G1 | Node/component/API tests; no writes/probes before consent; desktop/mobile save parity, Back/Next/cancel and conflict handling; no draft storage access on any device. |
| G4: bounded verification | Timestamped shared backend/agent evidence; explicit offline completion and post-save results. Draft reachability remains a separate opt-in extension. | G1, G3 | Silent gateway/controller, stale agent and serial ownership tests; evidence timestamps and no state-changing commands. |
| G5: optional mDNS | Advertisement ownership/network matrix, bounded browser service and suggestion UI. | G0 discovery decision; G3-G4 | Zero/one/multiple/untrusted/expired results; native and Docker capability tests with manual fallback. Not a G6 core-release blocker. |
| G6: acceptance and handover | Integrate G2-G4, docs/screens and phased compatibility migration; separately approved representative hardware rehearsal. | G1-G4 | Core test matrix below, real empty-image setup and explicit limitations. G5 accepted separately if shipped. |

Suggested implementation starting point: **G0 and G1**, not wizard markup.
The save and effective-value foundations fix existing editor correctness as
well as preventing the wizard from inheriting those risks.

## 10. Verification matrix

| Area | Required cases and invariant |
|---|---|
| First-run classification | Missing, valid empty-topology, malformed, unreadable, unwritable, seeded example and existing real config. Only truly missing config auto-enters setup; no classification by names. |
| Precedence | Saved TCP/serial with no/host/port/both overrides; blank, invalid and boundary ports; valid port-only serial-to-TCP case. UI, API and connector agree; unrelated saves retain inactive saved values. |
| Topology | Supported ID/count limits, duplicate addresses, zero enabled zones, renamed/hidden/deleted zones, input enable/disable, shortcut impacts and unknown fields. Save/diff uses one normalization path. |
| Playback | No agents, unavailable/stale agent, multiple agents, missing output, duplicate labels, preserved unresolved binding and skip. No credentials in API/draft/logs; stable IDs survive label changes. |
| Unsaved edit lifecycle | On all devices, step changes, preview, failed save/conflict, resize and orientation retain in-memory values; dirty Cancel/navigation confirms discard where supported. Fresh page load starts from saved server config; reload/close loss is explained. Assert no draft persistence reads/writes, retention/resume controls or cross-device sync. |
| Conditional save | Two tabs, missing precondition (`428`), changed/identical preconditions, missing-file creation race, process restart, effective-constraint change, private-field-only external edit, external cooperative writer, timeout/unknown outcome and conflict reapply. Playback polls do not invalidate revisions; no automatic overwrite/retry. |
| Persistence | Fail before/between/after staged writes, rename and sync; restart/reconciliation, permission/disk-full errors, newer unrelated edits and live polling. Exact consistent outcome or explicit blocked recovery; never two-renames-equals-transaction. |
| Verification | Reachability timeout/refusal/DNS failure, reachable silent controller, fresh/stale poll, absent serial ownership, peer TLS/auth failures and per-agent partial failure. Assert actual concurrency/time limits and no control/volume/power/playback calls. |
| Discovery | Disabled/unavailable multicast, zero/one/multiple/spoofed/oversized/expired records, duplicate IDs, isolated VLAN/bridge, manual input and explicit overrides. Never auto-select, connect or overwrite. |
| Deployment | Clean image empty volumes, managed/external/dummy scope, native provisioned serial/TCP, configured update rejects missing config, first-install recovery and post-save activation/rerun. No example seeding or native-service changes. |
| UX and disclosure | Full desktop/phone/tablet journey including preview, conditional save, conflict and results; keyboard/screen-reader/touch flow, 320 CSS-pixel reflow, 44 CSS-pixel touch controls, virtual keyboard/orientation/zoom, focus/error summaries, non-color status and unsaved navigation. All devices have the same loss notice and no draft controls. Existing mobile runtime pages unchanged; no secret/path disclosure; status wording remains truthful. |

Use focused unittest selectors and Node test files while iterating; extend
existing image/Compose/Ansible checks rather than install unrelated tooling.
Manual browser acceptance should use a disposable deployment with synthetic
configuration. Real hardware/network operations need their own approval, happen
after the current observation, and never convert unavailable checks into passes.

## 11. Remaining decisions and handoff

- [ ] Approve remaining section 3 recommendations, especially sample policy
  and initial supported layouts; memory-only responsive editing is confirmed.
- [x] Conditional-write compatibility: revision required immediately, no
  transition release (2026-11-20).
- [ ] Approve the G1 persistence protocol after writer inventory/failure
  reproductions.
- [ ] Set bounded diagnostic target/concurrency policy and whether native
  serial verification remains cached-only for the first release.
- [ ] Approve unconfigured deployment intent and its post-save activation
  transition without weakening configured adoption/update acceptance.
- [ ] Decide whether mDNS ships later (recommended); if included, approve
  advertisement ownership and proven network support before dependencies.
- [ ] Review G6 evidence and remaining hardware/access-control limitations
  before calling guided setup complete.

### Decisions versus implementation evidence

Product approval is needed for D1-D4, D6 and D8-D10 and supported first-release
deployment layouts. D5 is implemented without a transition release. No runtime work is
implied by refining this document. D7/D11 (full responsive setup, memory-only
edits on every device and no cross-device sync) are confirmed and supersede
all earlier desktop-only and local draft retention proposals.

G0 must still establish the complete writer inventory, single-process deployment
assumption, ownership/mount compatibility and recovery feasibility. Those are
engineering gates to demonstrate with repros/tests, not questions for an operator
to answer from intuition. If they invalidate this baseline, revise the design
before enabling the wizard. Optional mDNS/probe destination policy is deferred,
not silently approved.

## 12. Executable implementation sequence

### 12.1 Start gate: six-hour baseline (satisfied)

Implementation was gated on a six-hour Pi observation that began
2026-11-14 01:29:07 CET. The original run completed with four failures. Resolution and the
short diagnostic/repeat endurance gates are tracked separately in the
[audio diagnostic freshness incident plan](./general_done_audio-diagnostic-freshness.md).
Its corrected-runtime six-hour acceptance passed on 2026-11-14 at 15:19:18 CET
(350 samples, zero failures, 21,602.064 seconds; verified at approximately
17:54 CET). The observation prerequisite is satisfied; remaining contract and
implementation gates still apply. The original failed records remain evidence,
not a retrospectively converted pass. No automatic implementation or rollout
is started by this result.

### 12.2 Core release boundary

Implement the six-screen journey represented by the latest simplified mockups:
Welcome, Connection, Rooms & inputs, optional Playback, Review, Results.
Desktop, tablet and phone use one responsive implementation. Unsaved values live
only in the current page; no retention controls, stored drafts or resume feature.
Keep controls familiar: Back/Next, Skip playback, explicit final Save and Cancel.
Show deployment-managed connection values without requiring operators to
understand environment precedence. Keep protocol/agent details in expandable
status information rather than adding more mandatory onboarding questions.

The core release includes safe configuration persistence, offline save, honest
results and explicit first-install deployment handling. It excludes mDNS, draft
endpoint probing, pairing, credentials, hardware control commands and unrelated
deployment/logging modernization. G5 is a later optional project, not part of
the following delivery sequence.

### 12.3 Ordered implementation batches

Each row is a separately reviewable change, not an instruction to commit or push
without a request. Tests must accompany the producing batch; do not defer all
verification until the UI exists. File names below refer to existing surfaces;
introduce a small helper/module only where extraction demonstrably simplifies
shared behavior.

| Batch / gate | Concrete changes | Tests and completion evidence |
|---|---|---|
| I0 / G0: baseline and contracts | After section 12.1 passes, inspect the current worktree/branch and preserve unrelated changes. Inventory all config/state writers, readers, startup paths, locks and supported mount types. Resolve remaining product decisions and the unconditional-client migration release. Add reproductions for stale save, state-before-config failure, invalid-vs-missing classification and invalid override display. | Repros in [controller tests](../../tests/test_russound_controller.py), [server tests](../../tests/test_russound_server.py) and [connector tests](../../tests/test_russound_connector.py). Record the selected lock/recovery protocol and any design amendments before I2. No live Pi repros. |
| I1 / G1: preparation and metadata | Extract the pure candidate/impact builder from [controller updates](../../web/russound_controller.py). Add explicit load states and shared effective-field metadata in [config types](../../web/config_types.py); preserve existing saved/private/unknown values. Keep existing editor behavior until its migration. | Existing offline-save, topology, playback binding and reconnect tests pass; preview preparation has no file writes, backend reconnects or control calls. Resolver/UI metadata agrees for independent host/port overrides and serial mode. |
| I2 / G1: persistence and recovery | First introduce the [config and state store](#config-and-state-store-first-step-of-i2) as the single owner of both files. Then implement the chosen section 5 persistence protocol inside it; route every app-owned writer through the coordination boundary. Reconcile before startup polling or commands and block unsafe recovery explicitly. Preserve owner/mode, absence and latest surviving runtime state. | Fault injection at each staging, record, replace and sync boundary; concurrent cooperating state/config writers; restart recovery, private backup handling and unsupported mounts. Unexpected external edits block rather than disappear. Do not declare durability from a normal save test alone. |
| I3 / G1: API and existing-editor migration | **Partly done 2026-11-20:** required revision checks, `409`/`428` envelopes and editor migration. Remaining: add preview, structured field errors and save/activation separation to [server](../../web/russound_server.py). Migrate [configuration editor](../../web/static/config.js) and relevant [API helpers](../../web/static/api.js). Complete the documented compatibility transition before enabling the wizard. | Two tabs, concurrent first creation, private-field edit, process restart, missing header, changed constraints and timeout reconciliation. Existing editor remains usable; final writes recompute candidates under lock. Required preconditions cannot be bypassed by another supported HTTP writer. |
| I4 / G2: truthful first install | Update [Compose adapter](../../tool/deployment_compose.py) and existing deployment workflow surfaces with reviewed absent-config intent and configured activation. Sample seeding is already gone: the legacy Docker and native web playbooks were retired on 2026-11-16. Confirm in the inventory that no new path reintroduces it, and replace the Rooms page hint that tells operators to copy the example. Preserve existing files. | Extend deployment input/preflight/state/workflow/operator tests and [profile tests](../../tests/test_container_profiles.py). Prove empty config acceptance, first-install absence recovery, strict configured updates and reviewed post-save activation. Existing configuration removal never re-enters an exemption. |
| I5 / G3: responsive setup | Reuse/extract the editor's in-memory model, add guided mode to the existing config route/UI, and reuse [styles](../../web/static/styles.css). Missing config enters setup; valid config offers explicit review; invalid/unreadable shows guidance. Add steps, optional stable-ID playback selection, preview, discard guard and conflict review. No browser storage layer. | Add focused setup/editor Node tests following [rooms UI tests](../../tests/test_rooms_ui.js). API/UI parity for allowed fields and managed values; Back/Next/Skip/error preserve memory edits; reload starts from server; no draft persistence access or premature hardware/config writes. |
| I6 / G4: results and evidence | Integrate shared backend/agent observation source, age, generation and freshness into Results. Distinguish durable save, application activation and physical verification. Offline action skips deliberate verification, with normal polling disclosed. No per-tab poll loops or new arbitrary-target probes. | Fresh/stale/missing poll, reachable silent controller, authentication failure, partial agent inventory and late old-generation replies. Assert exact freshness behavior and absence of power/source/volume/playback commands. Saved config remains saved even if activation fails. |
| I7 / G6: disposable acceptance and docs | Exercise the complete setup and revisit flow with synthetic config in disposable native/container environments. Update operator/user guidance, API examples and coverage documentation. Check that the generated mockups are treated as illustrations, not specifications overriding contracts. | Desktop and mobile browser evidence; existing full Python/Node CI; clean amd64/ARM64 image smoke checks; deployment integration checks. Record skipped/environment-dependent checks honestly and retain evidence before proposing a Pi rollout. |

Dependency order is I0 -> I1 -> I2 -> I3. I4 follows I3 for the conservative
implementation sequence; I5 follows I4; I6 follows I5; I7 integrates all batches.
This intentionally serial order keeps the initial work easy to review rather than
introducing parallel branches or enabling a UI before its save/deployment gates.
If the compatibility transition needs a separate release, stop wizard enablement
at I3 until that release/migration is complete; do not disguise a bypass as a
temporary implementation shortcut.

### 12.4 Validation procedure

Use the repository's existing runners and dependencies. For each batch run the
smallest relevant selectors; expand after shared persistence/API changes and at
I7. Do not install dependencies preemptively or create a new frontend framework.

Examples from the repository root:

```sh
# Controller/server work; add connector tests when changing resolution.
.venv/bin/python -m unittest discover -s tests -p 'test_russound_controller.py'
.venv/bin/python -m unittest discover -s tests -p 'test_russound_server.py'

# Existing frontend runners; include the focused setup tests once created.
node --test tests/test_api_fetch.js tests/test_rooms_ui.js tests/test_input_playback.js

# Integration gate; CI also runs real architecture-specific image smoke checks.
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.js
```

These are planned validation commands, not results from this documentation task.
Match deployment integration selectors to the actual changed surfaces and their
existing fixture requirements; a skipped Docker/SSH test does not certify that
workflow.

Browser acceptance must cover the entire journey at desktop and 320 CSS-pixel
mobile width, not only a static screenshot. Verify touch target dimensions,
virtual keyboard/zoom/orientation, focus/error navigation and reflow. Exercise
Cancel, skip playback, offline save, two-tab conflict and uncertain save without
losing the in-page candidate. Instrument browser storage access to prove setup
does not persist drafts. Confirm existing rooms/playback/status behavior is
unchanged. No real playback or volume commands are needed for these checks.

### 12.5 Ready-to-implement checklist and rollout boundary

- [x] Simplified responsive journey and memory-only editing captured.
- [x] Mockup differences and excluded draft features identified.
- [x] Producing batches, dependencies and verification gates documented.
- [x] Six-hour completion verified against section 12.1; evidence recorded.
- [x] API migration decision: revision required, no transition release.
- [ ] Remaining product decisions resolved in I0.
- [ ] Writer/reader inventory and recovery protocol proven before I2.
- [ ] Local implementation and disposable acceptance completed through I7.
- [ ] A separate reviewed Pi rollout is requested and approved.

No timer, background implementation agent or automatic deployment is installed
by this plan. Continue with I0, then deliver the remaining shared foundations
before wizard markup. Passing the six-hour test certified only the observed
existing deployment, not the new setup feature.
Do not use the running configured Pi as an empty-config setup fixture.

Related: [safe configuration changes](./web_open_safe-configuration-changes.md),
[effective overrides](./web_done_effective-config-overrides.md),
[guided diagnostics](./general_idea_guided-diagnostics.md),
[deployment simplification](./general_done_simplify-deployment-configuration.md),
[deployment status](./general_open_deployment-status-overview.md),
[Wi-Fi bootstrap](./general_idea_wifi-bootstrap.md) (a Pi without a network
cannot show this wizard) and
[network relocation](./general_idea_network-relocation.md) (the access boundary
and setup page after a move).
