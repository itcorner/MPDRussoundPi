# Manual playback operations (S07-S08)

S07 is implemented, tested locally, enabled on production and user-verified on
2026-10-17. S08 managed station selection and S09 TuneIn search/resolve are
implemented and tested locally; station selection has not been enabled or
accepted on production. Automation remains unimplemented.

## Activation and trust

The agent remains read-only by default. Supplying `--operations-db` with a
private persistent SQLite path enables play, pause, stop and calibration gain
commands, including TuneIn station selection. Pass
`--disable-tunein-selection` (Ansible:
`playback_agent_enable_tunein_selection=false`) to keep the other manual
operations but reject station selection with `station_selection_disabled`. Capabilities advertise only enabled actions in `write_operations`;
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

TuneIn station selection request:

```json
{
  "action": "set_tunein_station",
  "station_id": "s231065",
  "station_name": "Radio BBC News Haiti",
  "expected_config_revision": "<64 lowercase hexadecimal characters>",
  "expected_control_revision": "<64 lowercase hexadecimal characters>",
  "idempotency_key": "a-client-generated-unique-key"
}
```

The browser sends only the TuneIn station identity and display name; arbitrary
stream URLs are rejected by the operation API. The web gateway exposes
`GET /api/playback/catalogs/tunein/search?q=...` and returns normalized station
results without provider URLs. A query that is a TuneIn station ID (for
example `s25744`, case-insensitive) looks up only that station through
`Describe.ashx` and returns it only if `Tune.ashx` offers a supported
stream; otherwise the result list is empty. On selection, the Pi resolves the station ID
through TuneIn's fixed OPML endpoint and accepts only MP3/AAC/Ogg stream URLs
with public-literal hosts. Direct entries are preferred; otherwise the Pi reads
TuneIn-listed M3U/PLS playlists (at most 16 KB, no redirects) and uses the
first valid HTTP(S) entry that is not itself a playlist. A station without such
a stream fails with `station_unavailable` before any config, intention or MPD
change, and the previous station keeps playing. Resolved stream URLs are stored only in the private
MPD config and the mode-0600 operation ledger does not contain them. Status
shows the selected TuneIn station name and a redacted current host, never the
stream path or query.

A selection resolves the station once. Supervisor reconciliation re-resolves a
TuneIn station only when its output is not already playing the saved URL
without an MPD error, so healthy outputs do not query TuneIn while holding the
shared lock. After (re)starting a stream, routing waits up to 12 seconds for
MPD's PulseAudio client because internet streams may buffer for several
seconds; steady-state route checks keep the 5-second window. `audio_failed`
operation results log the underlying readiness/route/gain reason in the
agent journal.

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
    "output_id": "stereo_front",
    "action": "stop",
    "status": "queued",
    "phase": "accepted",
    "created_at": 1792230000.0,
    "deadline_at": 1792230030.0,
    "requested_intention_persisted": false,
    "requested_source_persisted": false,
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

## S08 source selection semantics

- Source selection saves the new desired URL, changes that output's persisted
  intention to `play`, and attempts playback. Selecting the same URL is safe
  and does not add another queue entry.
- The managed queue contains at most one selected source. Initial adoption is
  allowed only for an empty queue or one song matching the previously configured
  URL. Subsequent cleanup deletes only the persisted managed song ID; an
  unowned queue entry causes an explicit failure without clearing the queue.
  Queue inspection is limited to 64 entries.
- The selected URL and queue ownership flag/song ID are atomically written to
  the existing MPD config under the shared mutation lock. No MPD, PipeWire,
  systemd or audio config regeneration is performed.
- If the URL was saved but MPD or audio application fails, the result reports
  `requested_source_persisted: true`; the selected URL remains visible and the
  supervisor can retry through its normal reconciliation path. Restoring an
  earlier source is another explicit selection.
- Desired and current MPD source are reported separately. Display strings omit
  URL path, user-info, query and fragment. The submitted URL is never reflected
  in operation progress or errors.
- TuneIn constrains the selection interface, not the destinations its station
  URLs or later redirects reach. Selection is on by default with manual
  operations; set `playback_agent_enable_tunein_selection=false` where a
  Pi-side egress policy is required but not yet in place.

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
The dialog is headed by the output label, with agent and output id as a
subtitle, and groups its controls into **Playback** (current station/state and
transport), **Station** (TuneIn search row and scrollable results) and
**Calibration gain** (slider, dB readout and Apply). The draft hint appears
next to **Apply gain** only while the draft differs from the configured gain.
Command feedback appears below the sections only when there is something to
report and scrolls once it exceeds a compact maximum height. The dialog itself
remains capped to the viewport. Stream cards show the station, track, gain and
freshness; the raw MPD source host is no longer displayed there.
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
