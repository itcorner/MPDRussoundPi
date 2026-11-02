# API payload cleanup

Noted 2026-10-18. Baseline commit: 8294e73. Implemented and deployed
2026-11-02 (see the [implementation record](#implementation-record-2026-11-02));
the deprecated `/api/state` endpoint was removed the same day after the
deployment was verified.

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

## Implementation record (2026-11-02)

All four steps implemented; design decided with the maintainer: new endpoint
plus temporary deprecated `/api/state`, flat snapshot shape, mutation
responses return the Rooms snapshot.

1. **Rooms snapshot:** new `GET /api/rooms` returns the flat allowlisted
   shape (`config_required`, `message`, `backend_status`, `system_power`,
   visible `zones` with 6 fields, enabled `inputs` with resolved display
   data, `shortcuts` with id/name/source, `revision`, `epoch`). All mutation
   endpoints (`/api/system/power`, `/api/source`, shortcut activation, zone
   actions) return the same snapshot. The Rooms page fetches `/api/rooms`;
   its client-side view model consumes the server shape directly.
2. **Configuration snapshot:** new `GET /api/config/state` returns
   `config_required`, `backend_status`, saved `backend` settings, visible
   zones with the advanced sound values, and `resolved_inputs` (now always
   resolved, not only when bindings exist). The Configuration page polls it
   instead of `/api/state`.
3. **`notes`:** stripped from every API response (view payload and config
   editor payload); the key stays in the config file as admin documentation.
   Saves are unaffected because `update_config_zones` merges into the
   on-disk config.
4. **SSE:** `state-change` carries only `{revision, epoch}`; each page
   refetches its own snapshot. The Rooms page's in-place rendering (from the
   [live-update fix](./web_done_live-update-rerender.md)) makes this safe.

`GET /api/state` remained briefly as a deprecated compatibility endpoint
(full view minus `notes`) and was removed on 2026-11-02 after the maintainer
verified the deployed new endpoints. The CI smoke
test and all page code use the new endpoints. Tests migrated and extended
with allowlist shape checks (`tests/test_russound_server.py`,
`tests/test_input_playback.py`, `tests/test_rooms_ui.js`); full Python and
JavaScript suites pass. Documentation updated: `web/API.md`, `README.md`,
`software-architecture.md`, `mpd-web-integration-architecture.md`,
`mpd-playback-read-api.md`.
