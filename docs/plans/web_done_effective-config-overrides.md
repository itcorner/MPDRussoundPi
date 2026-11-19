# Effective configuration and environment overrides inline

Status: **implemented and test-deployed; phone acceptance confirmed**
(2026-11-15). Release and screen-reader testing remain follow-ups.

**S3 runtime foundation implemented locally, 2026-11-14:** the shared resolver
now supplies additive `effective_backend` metadata to the editor, config-state
API and successful saves. Connectivity uses its `configured` value rather
than reimplementing environment precedence in JavaScript. The operator selected
the compact backend-only mockup: effective connection mode/host/port now appear
in their existing fields with inline source badges and accessible explanations.
Environment-controlled values are read-only; the existing API write contract
is unchanged. See the
[S3 record](./general_done_simplify-deployment-configuration.md#s3-runtime-configuration-and-logging-completion-2026-11-14)
and current implementation below.

## Goal

On the configuration page, show the values actually used by the running
application in their normal fields instead of a separate environment-override
banner above conflicting saved values. Identify each externally controlled
field clearly, make it read-only and explain how to change it.

Use an **Environment** badge alongside the field rather than prefixing the
input value with `env:`. The agreed presentation uses a small explanation
paragraph, associated with the controls, rather than a tooltip. The API
documentation names the relevant variables. Do not promise that editing a
generated `.env` is durable:
Ansible-managed deployments must change the corresponding playbook variable.
The UI must not expose secret values or arbitrary environment variables.

## Verified current behavior and precedence

- The editor renders effective backend mode/host/port in one field each, using
  the shared metadata instead of a separate conflicting override summary. The
  legacy `backend_overrides` API value remains for compatibility and is not
  used by the editor; see [`renderBackendFields`](../../web/static/config.js)
  and [`GET /api/config`](../../web/API.md#get-apiconfig).
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

## Implemented behavior and remaining scope

- For the selected compact design, backend mode, host and port use their
  normal fields. `Environment` and `Default` badges are visible; `Config file`
  badges are hidden by design. A paragraph below the fields is referenced by
  their `aria-describedby`. Fields controlled by environment are read-only;
  an unoverridden TCP field remains editable when the saved endpoint is TCP.
  CLI has no backend host/port override: `--config` selects the file only.
- When TCP is forced over saved serial configuration, mode is disabled and the
  hidden saved serial endpoint is preserved. The effective TCP fields are
  read-only in this case because the saved serial form has no independent
  dormant TCP fallback to edit. For first setup with no saved endpoint and an
  active environment override, the UI explains that the override must be
  removed before a file endpoint can be configured.
- An existing partial saved TCP endpoint is inactive in the resolver. The UI
  displays the effective defaults and retains the partial endpoint on unrelated
  saves. Changing the poll interval is blocked until that endpoint is completed.
  Backend drafts survive controller/zone editor rebuilds.
- The selected design labels only backend endpoint fields that have effective
  provenance; file-only controller/source/shortcut fields retain the compact
  existing appearance. Poll interval and serial device/baud have no CLI or
  environment override in the current runtime.
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
- Preserve existing API consumers and writes: saved `config`, additive
  `effective_backend`, and legacy `backend_overrides` semantics are unchanged.
  Read-only enforcement applies only to the browser editor; direct API clients
  retain their existing ability to update saved values. The shared resolver
  remains the sole source of effective backend values and provenance.

## Decisions and implementation stages

- [x] Inventory every field in the configuration editor and document whether
  environment variables or command-line options genuinely influence it,
  including defaults, validation and precedence. Only backend mode/host/port
  have effective source indicators; other editor fields are file-only.
- [x] Decide the read-only API contract and saved-serial behavior: read-only is
  UI-only; forced-TCP values over saved serial configuration cannot edit a
  dormant TCP endpoint. Environment-backed values point operators to the
  deployment environment and service restart/recreation.
- [x] Define additive allowlisted source metadata from the shared endpoint
  resolver, preserve saved/legacy API fields, and update
  [web API documentation](../../web/API.md) (S3). Invalid nonblank runtime
  ports reject startup.
- [x] Update the configuration UI, save/diff logic and help text; retain
  hidden saved serial endpoints and do not persist environment values.
- [x] Add automated coverage for effective source badges, editability and
  saved serial/TCP preservation under environment overrides.
- [x] Inspect the existing UI on the live Pi Docker deployment without changing
  connection settings; verify an isolated native web process using a fixture
  and loopback-only fake backend. The native check does not exercise a Pi
  systemd deployment.
- [x] Deploy the current UI candidate to the live Pi for mobile testing.
- [x] Confirm the live deployment works on the operator's phone.
- Follow-up: commit and release the changes through the normal workflow.
- Follow-up: verify keyboard and screen-reader behavior on actual devices.

## Verification and follow-ups

- Automated runtime and server tests cover TCP/serial resolution, independent
  overrides, whitespace, invalid ports and boundaries, metadata parity,
  offline saves and serial restoration. Editor tests cover source badges,
  partial overrides, saved-value preservation, incomplete endpoints, draft
  rebuilds and connection status. These do not cover every combination of
  browser save/reload and inaccessible config files.
- The live Pi Docker UI was visually inspected with environment-sourced mode,
  host and port, a connected backend and healthy containers. On 2026-11-15,
  the reviewed deployment applied the subsequent incomplete-endpoint and
  accessibility fixes; apply, container, HTTP, backend and playback checks
  passed. The served UI assets match the current working tree. This candidate
  has not been published as a release. The operator confirmed that the
  deployment looks good and works on their phone.
- An isolated native web process was exercised in a browser with a partial saved
  endpoint, both with a port override and after removing it: effective defaults
  were displayed, a controller edit preserved the backend draft, and unrelated
  saves preserved the partial file endpoint. Keyboard focus, computed
  accessibility-tree descriptions and a narrow viewport were checked. This is
  not a native Pi systemd deployment or a physical touch/screen-reader test.
- Follow up with physical keyboard and screen-reader verification, and any desired
  expanded end-to-end coverage. Tooltips were intentionally removed; the
  explanation paragraph is the intended replacement.
