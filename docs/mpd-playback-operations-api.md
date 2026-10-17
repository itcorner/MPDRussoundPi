# Manual playback operations (S07)

Status: implemented, tested locally and enabled on production, 2026-10-17.
S07 is done and production-verified: the user confirmed successful production
testing on 2026-10-17 after the contention and UI fixes.
Source changes and automation are not implemented by S07.

## Activation and trust

The agent remains read-only by default. Supplying `--operations-db` with a
private persistent SQLite path enables play, pause, stop and calibration gain
commands. Capabilities advertise only enabled actions in `write_operations`;
the web exposes controls only for advertised actions with usable revisions.
Old read-only agents remain compatible.

D07 accepts a trusted environment without browser login, user permissions or
roles. Anyone who can access the web interface can use enabled controls.
Existing same-origin/Host checks remain; web-to-agent TLS, certificate
validation and bearer authentication remain mandatory. The configured service
credential authorizes this one agent's enabled operations on configured output
IDs, not arbitrary MPD ports, shell commands, paths or systemd services.
Separate per-user/read-versus-write credentials are not introduced in S07.
Reassess access protection before exposing the deployment outside that trust
boundary. Lightweight admin authentication is deferred.

## Submission and lookup

Agent routes:

- `POST /v1/outputs/{output_id}/operations`
- `GET /v1/operations/{operation_id}`

Same-origin web routes:

- `POST /api/playback/agents/{agent_id}/outputs/{output_id}/operations`
- `GET /api/playback/agents/{agent_id}/operations/{operation_id}`

Only registered agents and validated IDs/actions are accepted; these are not
generic proxy routes. All agent routes require the service bearer token.
The JSON operation body is bounded to 2048 bytes:

```json
{
  "action": "set_gain",
  "gain_db": -7.1,
  "expected_config_revision": "<64 lowercase hexadecimal characters>",
  "expected_control_revision": "<64 lowercase hexadecimal characters>",
  "idempotency_key": "a-client-generated-unique-key"
}
```

For `play`, `pause` or `stop`, omit `gain_db`. Unknown/missing fields are errors.
An idempotency key is 16-128 ASCII letters, digits, underscores or hyphens.
Keys are scoped to this agent/output; reuse with different fields, including a
different action, is a conflict. Retry a lost response with the **identical**
fields/key, never a freshly generated command.

Inventory exposes the config content revision and, when writes are enabled,
each output's `control_revision` and `control: {mode: "manual", desired_state}`.
The control revision hashes the legacy intentions file's bytes, inode and
nanosecond modification time. It is host-wide: even a same-value CLI intention
write or another output's intention edit conservatively invalidates an old
revision. Existing CLI/supervisor/audit JSON remains unchanged; no automation
mode/schema migration occurs. Config revisions retain S04 content semantics.
Both revisions are checked on submission and again under the shared backend
mutation lock immediately before execution.
Submission validation is read-only and does not compete for that lock.
If the supervisor or CLI temporarily holds it, the durable worker reports
`waiting_for_backend` and retries acquisition until the original start deadline
(or shutdown). Revisions are rechecked on every execution attempt; no mutation
is retried after execution has begun. Persistent contention fails explicitly
with `backend_busy`.

Submission returns `202` only after the queued operation is committed to SQLite:

```json
{
  "api_version": 1,
  "agent_id": "musicpi",
  "epoch": "<agent process epoch>",
  "operation": {
    "id": "<32 lowercase hexadecimal characters>",
    "output_id": "front",
    "action": "stop",
    "status": "queued",
    "phase": "accepted",
    "created_at": 1792230000.0,
    "deadline_at": 1792230030.0,
    "requested_intention_persisted": false,
    "gain_persisted": false,
    "applied": false,
    "error": null
  }
}
```

Lookup returns the same envelope with current operation fields. HTTP acceptance
is not playback success. Status is `queued`, `running`, or one of:

| Terminal status | Meaning |
| --- | --- |
| `applied` | Backend returned after verification; for paused/stopped gain this means saved calibration, not live mixer application |
| `failed` | Explicit typed backend/revision/lock/persistence failure; inspect phase and persistence flags |
| `expired` | The operation did not begin before its deadline; no mutation attempted |
| `superseded` | A newer accepted command replaced this output's queued target |
| `outcome_unknown` | Execution was interrupted; no blind replay or claim of success |

For gain success, `gain_application` is `live` or `on_next_play`. Paused/stopped
outputs do not start or create audio routes just to set gain. Playing outputs
reuse the existing route/gain-save/rollback implementation. Playback commands
reuse existing startup readiness and mixer gating. All mutations target one
configured output; other queues/gains/intentions are preserved.

`requested_intention_persisted` is separate from `applied`. MPD/audio failure
after saving play/pause/stop can leave an intention that the existing supervisor
later fulfills. Failure is not cancellation. Gain-save and gain-rollback
failures have distinct codes. Error messages are sanitized; upstream URLs,
credentials and raw backend exceptions are not returned.

Errors use `{api_version: 1, error: {code, message, retryable}}`: malformed
requests `400`, authentication `401/403`, unknown IDs `404`, stale revisions,
key conflict or busy lock `409`, capacity `429`, backend/transport unavailable
`503`. The web reports ambiguous transport failures explicitly as
`operation_outcome_unknown`; it does not automatically resubmit with a new key.

## Durability and bounds

One worker owns the ledger using a process lock. SQLite commits acceptance,
running phase and persistence checkpoints before proceeding. No ledger
transaction can be atomic with MPD and the backend's JSON files.

- Maximum 32 queued/running operations and 1000 retained rows.
- Queued operations have a 30-second execution-start deadline. In-flight work
  uses existing bounded backend I/O; a client timeout does not cancel it.
- A newer queued command for an output supersedes older queued commands.
  Running work is not cancellable; revision checks reject subsequently stale
  commands rather than replaying an outdated target.
- Terminal rows/keys are retained for at least 24 hours from acceptance, then
  pruned during a later submission. The ledger rejects capacity rather than
  prematurely dropping fresh idempotency keys.
- Queued work survives restart, subject to deadline/revision checks. Running
  work becomes `outcome_unknown` and is not replayed. Startup attempts a fresh
  persisted-intention/config/MPD observation, exposing `recovery_observation`
  when possible, but does not equate saved intention with successful execution.
- A missing/expired operation lookup never proves that a command did not run.

Shutdown stops new work, finishes the active bounded backend call and leaves
unstarted queued work durable. Unexpected worker failure is logged and blocks
new acceptance. Keep the ledger and lock in a private directory; do not remove
or replace the ledger while running. Rollback to read-only preserves it.

## User interface and acceptance

Artist/title text stays on one line in stream cards. Overflowing text scrolls
back and forth, pausing at each end; fitting text stays still. Reduced-motion
preferences disable animation and allow manual horizontal scrolling.

Each stream card's **Config** button opens a dialog containing explicit
transport actions and calibration gain. Icon-and-label transport buttons show
Pause when observed playback is playing, otherwise Play, alongside Stop.
These still submit explicit play/pause operations, not a backend toggle.
The header keeps Close alongside the wrapping stream identity on mobile.
Opening the dialog clears automatic focus so no element is initially selected;
keyboard users can still tab to its controls. Stop is disabled when observed
playback is stopped.
Gain uses the existing `gain_db`:
a slider normally ranging from -60 to 0 dB in 0.1 dB steps, with its minimum
extended to include an existing value below -60. The current/draft gain is
shown as a number with the dB unit, not an editable text or numeric input.
Opening the dialog does not clamp or change existing gain.
Moving the slider edits a draft; **Apply gain** sends
one command, superseding unsent edits without a request per slider movement.
Pending commands lock that output's controls. Progress, terminal errors and
ambiguous outcomes appear only in the Config dialog, not the stream grid;
closing and reopening preserves this feedback. Status polling is bounded.
The dialog fits its controls with a compact, fixed-height feedback area that
shows the draft hint when idle and command feedback in its place.
Longer feedback scrolls within that area, so status changes do not resize or
reposition the dialog. The dialog itself remains capped to the viewport.
After a changed gain is saved, commands for that agent remain disabled until
the status cache supplies the updated configuration revision. The saved draft
is retained during this refresh so consecutive edits cannot reuse the old
revision or revert the slider. Drafts survive semantic playback refreshes.

Local tests cover idempotency/lost responses, conflicts, capacity, queue
supersession, restart ambiguity, expiry, shared locks, invalid input,
partial persistence, gain rollback/deferred application, existing CLI/backend/
supervisor behavior and a web-to-agent HTTP round trip. Frontend tests use
isolated fixtures, not production playback.

The user confirmed production testing worked well and accepted S07 as done on
2026-10-17. Deployment verification did not independently issue live commands.
The user did not enumerate individual test cases; do not interpret this sign-off
as a separately measured restart/endurance result. Additional disruptive tests
still require explicit approval.
