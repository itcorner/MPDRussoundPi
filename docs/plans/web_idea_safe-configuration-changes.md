# Idea: safe configuration change handling

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

## Goal

Prevent lost edits and partial configuration saves, and make consequential
changes understandable before they take effect. Preserve the existing
configuration and runtime state unless a validated change is explicitly saved.

## Existing behavior and risks

The [configuration editor](../../web/static/config.js) submits backend,
controller, zone, input and shortcut data together. The
[`POST /api/config` handler](../../web/russound_server.py) processes it under
the server state lock, but the request has no configuration revision or
conditional-write precondition. Two editors can therefore submit stale
snapshots; a later save may overwrite the earlier edit despite atomic file
writes. A process-local lock does not coordinate external file writers.

[`update_config_zones`](../../web/russound_controller.py) persists adjusted
runtime state before persisting configuration. Each file is written
individually; a failure between writes may leave the pair inconsistent. The
existing invalid-config backup is useful but is not a multi-file transaction.
Investigate actual recovery paths and reproduce failure behavior before
selecting a persistence design.

## Candidate design

- Return a configuration-specific revision derived from the exact saved
  configuration, separate from ephemeral SSE and playback revisions. Require
  or negotiate a conditional save for updated clients; reject stale edits with
  an explicit conflict and preserve the user's draft. Offer reload, compare and
  deliberate reapply; do not silently merge conflicting topology changes.
- Protect against cooperating external writers as well as browser tabs, with a
  documented locking/revision strategy and a clear statement of which
  non-cooperating writers cannot be controlled. Define legacy-client behavior
  and migration rather than silently breaking the current API.
- Validate the complete candidate and provide a review of consequential
  changes: endpoint/mode, removed zones or bindings, shortcuts affected,
  reconnection and potential loss of control. Preserve overridden saved values
  and explicitly distinguish effective vs. saved values using
  [inline override metadata](./web_idea_effective-config-overrides.md).
- Design crash-safe, recoverable coordination of config and state writes,
  including partial failures and restarts. A pair of atomic renames alone is
  not an atomic transaction. Consider a journal or recovery protocol only
  after assessing compatibility with existing readers, permissions and storage.
  Avoid auto-rollback that can overwrite later valid edits.
- Backups, restoration and preview must be permission-aware and redact secrets;
  a restore is an explicit validated change with conflict handling, not a
  silent overwrite. Coordinate with the
  [deployment migration and rollback](./general_idea_simplify-deployment-configuration.md)
  design and [guided setup](./general_idea_guided-first-time-setup.md).

## Decisions and acceptance

- [ ] Reproduce stale-editor and interrupted-save cases; document all writers,
  locking and state ownership before designing transaction semantics.
- [ ] Decide revision/precondition requirements for existing API clients,
  conflict UX, backup scope and recovery after process/power failure.
- [ ] Test concurrent tabs and cooperating writers, stale/identical revisions,
  invalid submissions and retained drafts. Verify config overrides and serial
  mode are not accidentally persisted or removed.
- [ ] Inject failures before, between and after writes, during recovery and
  after an interrupted restart. Confirm a documented consistent outcome, no
  silent data loss and clear user-facing errors.
- [ ] Verify preview, accessible conflict/recovery flows and deployment
  migration without modifying a running production installation.
