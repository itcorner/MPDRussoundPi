# Web UI: live updates destroy open/active controls — analysis and fix plan

**Status: implemented.** Rooms (`web/static/app.js`), Playback (`web/static/playback.js`),
and the server-side `revision`/`epoch` (`web/russound_server.py`) follow this plan;
see `tests/test_rooms_ui.js` and `tests/test_playback_controls.js`. Known, accepted limits:
a Source select dismissed with Escape stays "in use" until it loses focus, and a zone
reorder still moves (and thereby closes) an open dropdown.

## Symptom

On the Rooms page (`/`), opening the **Source** dropdown of a zone and waiting for
a backend update (SSE `state-change` or the 5 s poll) makes the dropdown flicker
and eventually close.

## Root cause

`render()` in `web/static/app.js` is a full "throw away and rebuild" renderer:

- `zonesContainer.innerHTML = ""` followed by a fresh `<article class="zone-card">`
  per zone, including a brand-new `<select>` and `<input type="range">`.
- `shortcutsContainer.innerHTML = ""` followed by new shortcut buttons.

`render()` runs on **every** `applyPayload()`, which is triggered by:

1. every SSE `state-change` event (any zone, any field — also changes made by
   other clients or keypads),
2. the unconditional 5 s `setInterval(fetchState)` poll — even if nothing changed,
3. `eventSource.onopen`, and the response of every POST.

The open native dropdown belongs to the old `<select>` element. When that element
is detached the browser closes the popup (desktop: flicker/close; mobile: the
picker sheet collapses). Because the poll fires every 5 s regardless of changes,
the dropdown cannot stay open longer than ~5 s even on an idle system.

There is no "skip if unchanged" check and no "user is interacting" guard in
`app.js` (contrast with `config.js`'s `renderSoundControls`, which reuses its
elements and skips `document.activeElement`).

## Affected controls

### Rooms page — `web/static/app.js` (all rebuilt on every update)

| Control | Effect of rebuild |
| --- | --- |
| Zone **Source** `<select>` | Open popup closes; keyboard focus lost (reported bug). |
| Zone **Volume** range slider | Drag in progress is aborted (pointer capture lost), thumb jumps back to the backend value, the `change` event never fires, so the new volume is not sent. Keyboard focus lost. |
| Zone **On/Off** button | A tap whose `pointerdown` hits the old element and `pointerup` the new one produces no `click` → taps are silently dropped. Focus lost. |
| **Shortcut** buttons | Same dropped-click / focus-loss problem as power buttons. |
| "System off" button | Element is reused (re-appended), but moving it in the DOM still drops focus. |
| Backend warning banner | `innerHTML` rewritten each update: text selection lost (also renders the backend message as HTML — should be `textContent`). |

### Playback page — `web/static/playback.js`

`fetchPlayback()` already skips re-rendering when `playbackSignature()` is
unchanged, so the problem only shows when playback data really changes
(track/now-playing changes, freshness, favorites…). Then:

| Control | Effect |
| --- | --- |
| Output card **Config** button (`renderPlayback` → `outputsContainer.replaceChildren()`) | Dropped taps / focus loss on changing metadata (e.g. every track change on a stream). |
| Config dialog **favorites list** (Play / ↑ / ↓ / ✕ buttons) and **current favorite ☆** (`updateOutputDialog` → `favoritesList.replaceChildren()`, `currentFavorite.replaceChildren()`) | Rebuilt on every playback update while the dialog is open → dropped taps, focus jumps out of the list (bad for keyboard/screen-reader users). |
| Config dialog **station search results** (`stationResults.replaceChildren()`) | Same: "Select and play" / ☆ buttons rebuilt on every update. |
| Gain slider, station search input, transport buttons | **Not affected** – elements are created once and only updated. Dialog itself stays open. |

### Configuration page — `web/static/config.js`

**Not affected** for the periodic refresh: `renderSoundControls` creates the
sound sliders once and skips updating the focused element. Good reference
pattern. (`renderEditor` rebuilds everything, but only on load/save.)

### Status page — `web/static/status.js`

Display-only lists/tables are rebuilt; no form controls. Only side effect: text
selection is lost on update. Out of scope.

## Fix plan

### 1. Rooms page: switch `render()` to keyed, in-place updates (main fix)

1. Keep a `Map` `zoneCards` keyed by `zoneAddressKey(controller, zone)` holding
   references to `{ card, name, power, select, volume, volumeLabel }`.
2. Split `render()` into:
   - `createZoneCard(zone)` – builds the DOM once (as today, via
     `createElement`/`textContent`; drop the `innerHTML` template).
   - `updateZoneCard(refs, zone)` – mutates only what changed:
     power button text/class, slider `disabled`, zone name, and values.
   - Reconcile: create cards for new keys, remove cards for vanished keys,
     reorder only if the order differs. Clear the map when switching to the
     "config required" empty state.
3. Source options: rebuild a `<select>`'s options **only** when the input list
   signature (`id`, display name, title) changed; otherwise just set
   `select.value`. Reuse the same helper from `refreshInputNames()` (and fix its
   misplaced nested shortcut loop while there).
4. Interaction guard: do not write `value` into a control the user is using:
   - `select`: skip while `document.activeElement === select` (focus stays on the
     select while the popup is open, on desktop and mobile).
   - `range`: track `pointerdown` → `pointerup/pointercancel` plus focus, and skip
     value/label updates while active (mirrors `config.js`).
   - When interaction ends, the next update (or a re-apply of the last
     `state.zones`) brings the control in sync.
5. Shortcuts: rebuild buttons only when the `state.shortcuts` signature changes;
   otherwise update `title` in place. Keep `systemButton` in place instead of
   re-appending it.
6. Banner: create once, set `textContent` on two child spans, only when changed.
7. Cheap early-out: compute a signature of the render-relevant state
   (zones, system power, inputs, shortcuts, backend status) and skip `render()`
   when unchanged — stops the 5 s poll from touching the DOM at all.
   This is an optimisation on top of 1–6, not a replacement (real changes from
   other zones must still not close an open dropdown).

### 2. Playback page: stop rebuilding interactive lists on unrelated updates

1. `renderPlayback`: reconcile cards by `data-output-key` (keep the card and its
   Config button, update texts/logo/track in place); remove only vanished outputs.
2. `updateOutputDialog`:
   - Rebuild `favoritesList` only when the favorites signature
     (keys, names, order, current key) changed; otherwise only toggle
     `disabled` on existing buttons (store button refs per row).
   - Rebuild `stationResults` only when `state.stationResults` changed
     (identity check is enough – it is replaced on each search); otherwise only
     update `disabled`.
   - Same for `currentFavorite` (rebuild only when `selection.key`/favorite
     state changed).
   - If a rebuild is unavoidable, restore focus to the equivalent button
     (match by `aria-label`).

### 3. Tests

- Add `tests/test_rooms_ui.js` (node:test + `vm`, same fake-DOM approach as
  `tests/test_playback_controls.js`) covering:
  - applying the same payload twice keeps the same `<select>`/slider/button
    element instances;
  - a payload changing another zone keeps this zone's elements;
  - a focused `<select>` / active slider is not overwritten; it syncs after blur;
  - zones added/removed are reconciled;
  - backend message is rendered as text, not HTML.
- Extend `tests/test_playback_controls.js`: favorites/results button instances
  survive an `updateOutputDialog` call with unchanged favorites; card Config
  button survives a now-playing change.

### 4. Manual verification

Run the dummy backend (`docker-compose.dummy.yml`), open `/` in two browsers,
open a Source dropdown / drag a volume slider in one and change other zones in
the second: the dropdown must stay open, the drag must complete and send its
value; on `/playback`, keep the config dialog open across track changes and
check that favorites buttons keep focus and accept taps.

## Relation to the API payload cleanup ([web_new_api-payload-cleanup.md](./web_new_api-payload-cleanup.md))

The two plans are independent in cause (this bug is purely client-side DOM
rebuilding; slimmer payloads alone would not fix it) but touch the same code
paths: `applyPayload()`/`render()` in `app.js`, the SSE `state-change` handling,
and `/api/state`. Cleanup step 4 (SSE becomes a revision-only invalidation and
each page refetches its snapshot) does **not** change the number of renders, so
the keyed in-place rendering is required either way — and it is what makes the
cleanup's "fetch on every invalidation" model safe for open controls.

### Synergies to take care of now (additive, no breaking API change)

1. **Client-side Rooms view model = future Rooms snapshot.** Add one function in
   `app.js`, `toRoomsView(payload)`, that projects the payload onto exactly the
   allowlist from cleanup step 1: `system_power`, zones
   `{controller, zone, name, power, source, volume}`, enabled inputs
   `{id, display_name/name, playback_status, playback_message}`, shortcuts
   `{id, name, source}`, `backend_status`, `config_required`/`message`.
   - `render()` and `state` only consume this view (no more `payload.config.*`,
     `payload.state.*` lookups scattered around, and the three-way input
     fallback in `applyInputNames` lives in one place).
   - The render signature from fix step 1.7 is computed on this view, so
     changes to bass/treble/balance/loudness/turn-on volume, `config.notes` or
     other config fields no longer cause any DOM work on the Rooms page.
   - When the server later serves the Rooms snapshot, `toRoomsView` becomes
     (almost) identity and the server change is a drop-in.
2. **Expose the existing state revision.** The server already counts
   `state_revision` but only sends it inside the SSE event; `/api/state` and the
   POST responses don't carry it, and the client ignores it. Add `revision` to
   the state payload (additive field) and let the client drop payloads whose
   revision is older than the last applied one. This:
   - gives a cheap "unchanged" early-out for the 5 s poll and SSE duplicates,
   - is the prerequisite for cleanup step 4 (revision-only invalidation),
   - can later replace/complement the fragile client-only `zoneUpdateRevision`
     check in `fetchState()` (keep `pendingZoneUpdates` for optimistic values).
   Caveat to resolve while doing it: `state_revision` is currently incremented
   only in `broadcast_state_change`, not on every published payload; the
   revision must be bumped wherever `_state_payload` changes.
3. **Rooms tests use the reduced shape.** Write the fixtures of the new
   `tests/test_rooms_ui.js` with only the allowlisted fields. They then stay
   valid after the server-side cleanup and document the contract the Rooms
   page actually needs.
4. **Render backend messages as text.** The banner change (fix step 1.6) is also
   part of hardening the `backend_status` field that the cleanup keeps in both
   snapshots.
5. **`playback-change` on the Rooms page** currently refetches the whole
   `/api/state` only to update input names (`refreshInputNames`). Route it
   through the same `fetchState()` → `toRoomsView` → keyed update path instead of
   a separate DOM-patching function. After the cleanup this naturally becomes
   "refetch the Rooms snapshot".

### Deliberately left for the cleanup itself

- Removing fields from `/api/state`, splitting into Rooms/Configuration
  snapshots, dropping `config.notes`, and switching SSE to invalidation only:
  these need the coordinated server + test migration described in the cleanup
  plan (`test_russound_server.py`, `test_russound_controller.py`,
  `test_input_playback.py`) and are not needed to fix the bug.
- Playback page: unrelated to the cleanup (`/api/playback` is not in its
  scope); fix step 2 stands alone.

## Order of work

Synergy 2 (server `revision` field) → 1 together with synergies 1, 4, 5
(Rooms page, fixes the reported bug) → 3 (Rooms tests, synergy 3) →
2 (Playback) → 3 (Playback tests) → 4. The API payload cleanup can follow
afterwards without touching the Rooms rendering again.
