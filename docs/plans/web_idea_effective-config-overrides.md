# Idea: show effective configuration and environment overrides inline

Status: **idea, not scheduled or authorized for implementation** (2026-11-12).

**S3 runtime foundation implemented locally, 2026-11-14:** the shared resolver
now supplies additive `effective_backend` metadata to the editor, config-state
API and successful saves. Connectivity uses its `configured` value rather
than reimplementing environment precedence in JavaScript. The editor shows a
runtime/source summary but deliberately still edits saved fields. Full inline
badges/read-only fields, accessibility/help design and API write restrictions
below remain unimplemented; S3 does not authorize them. See the
[S3 record](./general_open_simplify-deployment-configuration.md#s3-runtime-configuration-and-logging-completion-2026-11-14).

## Goal

On the configuration page, show the values actually used by the running
application in their normal fields instead of a separate environment-override
banner above conflicting saved values. Identify each externally controlled
field clearly, make it read-only and explain how to change it.

Prefer a visible **Environment** badge alongside the field over prefixing the
input value with `env:`: a prefix would corrupt numeric inputs and complicate
validation and copying. Provide an accessible help control/description, not
only a hover-only tooltip, naming the relevant variable and explaining that it
must be changed or removed in the deployment environment and the service
restarted/recreated. Do not promise that editing a generated `.env` is durable:
Ansible-managed deployments must change the corresponding playbook variable.
The UI must not expose secret values or arbitrary environment variables.

## Verified current behavior and precedence

- The editor renders saved backend host/port from `config` and separately
  displays `backend_overrides`; see
  [`renderEditor`](../../web/static/config.js) and
  [`GET /api/config`](../../web/API.md#get-apiconfig). Since S3, the summary
  and status use the shared effective metadata, not the legacy override map.
- For the backend endpoint, non-empty `RUSSOUND_BACKEND_HOST` and valid
  `RUSSOUND_BACKEND_PORT` independently override the saved value. Blank or
  whitespace-only values do not override; a non-integer or out-of-range port
  now fails startup explicitly. The former logged-and-ignored behavior and its
  misleading override map were corrected in S3. Blank optional overrides remain
  unset; deployment errors are not disguised as effective settings.
- A valid host **or** port override selects TCP even when the saved endpoint is
  serial. A port-only override of a serial configuration currently resolves to
  TCP host `127.0.0.1` and the overridden port. This mode change is significant
  and must be displayed, not hidden behind the saved serial fields.
- Environment variables do **not** universally win over every configuration
  source. For example, explicit web-server command-line options such as
  `--host`, `--port`, `--config` and `--state` override their environment
  defaults. Some environment variables select paths or configure entirely
  separate services rather than overriding editor fields. Scope this feature
  to fields with an actual, verified relationship and reflect each field's
  real precedence; do not infer overrides from variable names.

These endpoint cases were checked against
[`resolve_backend_endpoint`](../../web/config_types.py) with saved TCP and
serial configurations, blank/valid/invalid environment values, and an
independent port-only override. S3 regression tests now also verify API source
parity, offline save preservation and shared browser status consumption.

## Proposed behavior

Coordinate the target precedence with
[deployment simplification](./general_open_simplify-deployment-configuration.md)
before changing it. The editor and
[deployment overview](./general_idea_deployment-status-overview.md) should
consume the same allowlisted effective-value/source metadata, not duplicate
precedence logic. Coordinate persistence and stale-editor handling with
[safe configuration changes](./web_idea_safe-configuration-changes.md).

- For each applicable field, display the effective value, a visible source
  badge and a keyboard/screen-reader-accessible explanation naming the source.
  Leave unaffected fields editable, including host and port independently when
  only one is overridden. Disable editing of the effective connection mode
  when the override forces TCP; explain why. The displayed source for the other
  TCP field may be saved configuration or a runtime default.
- Under the approved S3 contract an invalid nonblank runtime override rejects
  startup, rather than producing a running editor with a warning. A future
  deployment UI may explain that startup error without exposing its value;
  it must not pretend the override is active or silently ignore it.
- Preserve existing saved values independently of the displayed effective
  values. Saving a different field must not silently write an override into
  the config file, erase a hidden serial configuration, change mode, or create
  an unsaved-change indicator merely because the display differs. Handle
  initial setup, partial overrides, reloads and removal of an override
  explicitly. A later restart without the override must restore the saved
  configuration.
- Keep backend connectivity status based on the same effective endpoint as the
  actual connector; avoid a second browser-side implementation of precedence.
  Where it differs, distinguish a runtime default or inactive saved value from
  an environment value.
- Maintain existing API consumers: define additive typed effective-value and
  source metadata or a carefully versioned change; preserve the saved `config`
  and existing `backend_overrides` semantics until compatibility is decided.
  Resolve value, source and validity centrally using the same logic as the
  runtime. Only expose explicitly allowlisted non-secret fields.
- Existing configuration API writes must remain well-defined for other clients;
  determine whether read-only refers to this editor only or requires API-side
  enforcement. Do not silently discard submitted values or unintentionally
  change existing API behavior.

## Decisions and implementation stages

- [ ] Inventory every field in the configuration editor and document whether
  environment variables or command-line options genuinely influence it,
  including defaults, validation and precedence. Limit UI locking to effective
  overrides, not unrelated settings.
- [ ] Decide the read-only API contract, behavior for editing a saved serial
  configuration while TCP is forced, invalid override warning and where
  durable deployment-specific change instructions are maintained.
- [x] Define additive allowlisted source metadata from the shared endpoint
  resolver, preserve saved/legacy API fields, and update
  [web API documentation](../../web/API.md) (S3). Invalid nonblank runtime
  ports now reject startup; full inline editor policy remains open.
- [ ] Update the configuration UI, save/diff logic and help text; retain
  configuration persistence and non-overridden edits across all supported
  deployment layouts.
- [ ] Verify automated coverage and manually exercise Docker and native
  deployments without changing production connection settings.

## Acceptance tests

- TCP saved values with no override, host only, port only and both; equal vs.
  differing saved/effective values; whitespace, invalid, out-of-range and
  valid boundaries. Assert displayed values, source, editability and runtime
  endpoint agree.
- Serial saved values with host-only and port-only TCP overrides, including the
  default-host case; prove unrelated saves preserve the serial configuration
  and removal of the override restores it. Cover first-run/missing config and
  inaccessible config files.
- Verify API compatibility, config save/reload, connection change detection,
  unsaved state, external API clients and status display; verify no masked
  values are persisted accidentally and no arbitrary secrets are returned.
- Test browser keyboard and touch access, tooltips/help and screen-reader
  descriptions; confirm normal numeric values remain copyable and valid.
