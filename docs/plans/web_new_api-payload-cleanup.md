# API payload cleanup (planned, not implemented)

Noted 2026-10-18. Baseline commit: 8294e73.

## Findings
- `/api/state` is shared by Rooms (`web/static/app.js`) and Configuration (`web/static/config.js`) pages.
- Rooms uses only: zone name/controller/zone/power/source/volume, system_power, enabled inputs
  (resolved names), shortcuts, backend_status, config_required. It does NOT use bass, treble,
  balance, loudness, turn_on_volume, or most of `config`.
- Configuration needs advanced sound values, resolved_inputs, backend_status, config.backend.
- `config.notes` (see `web/config_example.json`) leaks to the frontend via
  `_filter_config_for_overview` (copies all config keys) and `build_config_editor_payload`
  (returns raw config). Frontend never renders it.
- SSE `state-change` (`broadcast_state_change` in `web/russound_server.py`) pushes the full state
  payload to every client.

## Proposed solution
1. Rooms snapshot: allowlisted fields only (system power, 5 zone fields, input display data,
   shortcuts, backend/config-required status).
2. Configuration snapshot: editor data plus a dedicated state view with advanced sound values,
   resolved inputs, backend status.
3. Config serialization: allowlist fields for API responses; omit `notes` everywhere in API
   output (keep in config file as docs). Stop returning raw config objects.
4. Live updates: SSE `state-change` becomes a small invalidation (revision); each page fetches
   its own snapshot.

## Related work
- [web_done_live-update-rerender.md](./web_done_live-update-rerender.md) (Rooms page re-render fix) prepares this cleanup:
  it adds a client-side Rooms view model matching the step-1 allowlist, an additive `revision`
  field on state payloads (prerequisite for step 4), and Rooms UI tests written against the
  reduced shape. Step 4 relies on that fix: invalidation + refetch is only safe once the Rooms
  page updates controls in place instead of rebuilding them.

## Before implementing
- Check for other `/api/state` consumers (status.js, playback.js, external tools/agents).
- Migrate pages + tests together (tests/test_russound_server.py, test_russound_controller.py,
  test_input_playback.py assert on `state["config"]["inputs"]`, full payload equality, etc.).
- Optionally keep old `/api/state` temporarily for compatibility.
