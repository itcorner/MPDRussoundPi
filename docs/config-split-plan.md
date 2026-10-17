# Plan: split output configuration from station selection

Status: phases 1-3 implemented (loopback MPD, `id` keys, config/state split
and favorites backend); phase 4 (web favorites UI) and phase 5 (Pi rollout)
pending. Decisions C1-C6 are settled (see below).

Implementation deviations from this plan:

- Favorites have no `logo` field; the dashboard derives TuneIn logos from the
  station ID, so the API carries `{key, name, type, station_id}` only and
  `add_favorite` takes `station_id` + `station_name`.
- `set_label` was dropped entirely: labels are admin-edited in `config.json`.
- Station selection saves station, resolved URL, managed song ID and the play
  intention in one atomic `playback.json` write.
- The web gateway already accepts the new agent capabilities; its favorites
  routes and UI are phase 4.

## Problem

Each `streams` entry in `config.json` currently mixes three kinds of data:

| Kind | Fields | Who changes it |
| --- | --- | --- |
| Static MPD instance / output | `name`, `id`, `label`, `port`, `bind_to_address`, `audio_output` | Admin at install time |
| Selected station | `stream`, `source_provider`, `source_station_id`, `source_station_name` | User, via dashboard |
| Runtime bookkeeping | `queue_managed`, `managed_song_id`, resolved `stream` URL, `gain_db` | Agent / supervisor |

Consequences:

- The Ansible-seeded "install" file is rewritten by the agent on every station
  change, gain change and queue reconciliation. A redeployment can't update
  structure without merging by hand.
- `config_revision` (sha256 of the whole file) changes on every selection, so
  unrelated edits conflict.
- `name` and `label` duplicate each other. `name` is the internal key for
  intentions (`playback.json`), systemd/MPD file names, `mpdctrl --stream`,
  the audit and the supervisor. `id` is the API key. `label` is the display
  text. Three keys for one output.
- `bind_to_address` is ignored: `config2mpd` reads `bind_to_addr` and falls
  back to `0.0.0.0`. All three MPD instances listen on every interface
  without authentication (verified on the Pi: `0.0.0.0:6601-6603`). This is
  hardware-audit item O12.

## Is loopback-only MPD binding safe?

Yes, for this project's code:

- Every MPD client connects to `localhost`: `MPDBackend._connection`,
  `MPDInstance` and `playbackaudit.py`.
- The web dashboard never talks to MPD. It uses the agent HTTP API
  (agent on `127.0.0.1:8081`, optionally behind the Caddy proxy).
- When checked on the Pi, there were no established connections to ports
  6601-6603.

Caveat: any LAN MPD app (phone client, ncmpcpp from another machine) would
stop working. Confirm that none is in use.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| C1 | Is any LAN MPD client in use? | No (proposal accepted): bind `127.0.0.1` unconditionally |
| C2 | Is gain per output or per station? | Per output (proposal accepted), stored in runtime state |
| C3 | Merge `playback.json` intentions into runtime state? | Yes (proposal accepted): one atomic file, one revision |
| C4 | Station favorites list? | **Do it now**, as part of the split (see below) |
| C5 | Keep `mpdctrl --stream NAME`? | Replace with `--output ID`; no alias for the old flag |
| C6 | Versioning/backwards compatibility? | **None.** Rewrite `config.json` in place without a version field. There is no migration tool and no legacy parsing; the old `streams` key is rejected with a clear error. The Pi files are rewritten once by hand during rollout |

## Target layout

### 1. `~/.config/mpdrussoundpi/config.json`: static, admin-owned

```json
{
  "outputmappers": { "audio_devices": [ "...unchanged..." ] },
  "outputs": [
    { "id": "stereo_front",     "label": "Stereo Front",     "port": 6601, "sink": "stereo_front",
      "default_station": "radio886", "default_gain_db": -7.1 },
    { "id": "stereo_centerlfe", "label": "Stereo CenterLFE", "port": 6602, "sink": "stereo_centerlfe" },
    { "id": "stereo_rear",      "label": "Stereo Rear",      "port": 6603, "sink": "stereo_rear",
      "default_station": "tunein:s8007" }
  ],
  "stations": [
    { "key": "radio886", "name": "Radio 88.6", "type": "url",
      "url": "https://edge85.streamonkey.net/radio886-onair/stream/mp3?aggregator=886Website" },
    { "key": "tunein:s8235", "name": "ORF Radio FM4",    "type": "tunein", "station_id": "s8235" },
    { "key": "tunein:s8007", "name": "ORF Hitradio OE3", "type": "tunein", "station_id": "s8007" }
  ]
}
```

Outputs (MPD instances):

- `streams` → `outputs`. `name` is removed: `id` is the only key and `label`
  is only for display.
- `audio_output: {type: pulse, sink}` → `sink` (Pulse is the only supported
  type).
- `bind_to_address` is removed; MPD always binds `127.0.0.1`.
- `default_station` is optional. If present, it must name an entry of
  `stations`. It applies only while the output has no runtime selection
  yet. In that case the output starts with that station and intention
  `play`.
- Without `default_station` (CenterLFE in the example) nothing plays
  initially:
  - the output starts with no station and intention `stop`;
  - the supervisor keeps the MPD queue empty and silent;
  - the dashboard shows "No station". Play stays visible but disabled,
    with the tooltip "Select a station first";
  - the first selection sets the station and starts playback.
- `default_gain_db` is optional and defaults to `0.0`. It seeds the
  runtime gain only when the output has none yet.

Stations (the seed favorites list):

- `type: "url"` entries are admin-only direct streams. The dashboard
  can't create these (D09 unchanged), but it can select them from the
  favorites.
- For `type: "tunein"` entries the key is always `tunein:<station_id>`. That
  keeps keys stable and duplicate-free when the same station is added from
  search.
- The seed `config.json` shows both types. The agent never writes this
  file, so `config_revision` changes only on admin edits.

### 2. `~/.local/state/mpdrussoundpi/playback.json`: runtime, agent-owned

This file replaces today's intentions-only `playback.json` at the same path.
It is written only by the agent and supervisor (the backend), atomically
under the existing control lock.

```json
{
  "favorites": [
    { "key": "radio886", "name": "Radio 88.6", "type": "url", "url": "https://..." },
    { "key": "tunein:s25744", "name": "Radio 88.6 (TuneIn)", "type": "tunein", "station_id": "s25744",
      "logo": "https://cdn-profiles.tunein.com/s25744/images/logoq.png" }
  ],
  "outputs": {
    "stereo_front": {
      "station": { "key": "tunein:s25744", "name": "Radio 88.6", "type": "tunein", "station_id": "s25744" },
      "resolved_url": "https://frontend.streamonkey.net/radio886-onair?aggregator=TuneIn",
      "managed_song_id": "4",
      "gain_db": -7.2,
      "intention": "play"
    }
  }
}
```

`favorites`:

- If the key is absent, it is initialized from `config.json`
  `stations`. After that, the dashboard owns the list: an empty list
  stays empty. Add, remove and reorder are allowed.
- Bounded to 20 entries. A config `stations` list with more than 20 entries
  is rejected. `add_favorite` on a full list fails with `favorites_full`. `name` must be printable and at most 120
  characters. `logo` is optional and is either a TuneIn CDN URL or
  omitted.
- Adding is only possible from TuneIn search results, as `tunein`
  entries. `url` entries come only from the config seed.

`outputs.<id>.station`:

- This is a copy of the selected station, not a reference into
  `favorites`. Removing a favorite therefore never breaks a playing
  output.
- Selecting from search doesn't require the station to be a favorite.

Other fields:

- `queue_managed` disappears: every output is managed and owns one queue
  entry.
- `resolved_url` is only a cache. For `url` stations it equals `url`.
- Output IDs that are no longer in the config are ignored and pruned on
  the next write.

Revisions:

- `config_revision` is the hash of `config.json`.
- `control_revision` is file identity plus the hash of `playback.json`
  (same scheme as today).
- Favorites edits change `control_revision`. The web always sends both
  revisions, so the operation request shape is unchanged.

### 3. Favorites API and UI

Agent: new durable operations on the existing ledger and endpoint pattern,
which means idempotency keys, revision checks and the 403 for disabled TuneIn
selection still apply.

- `POST /v1/favorites/operations`, with action:
  - `add_favorite {station_id, name, logo?}`: TuneIn only, validated as for
    `set_tunein_station`; adding an existing key is a no-op;
  - `remove_favorite {key}`;
  - `move_favorite {key, index}`.
- `GET /v1/favorites` returns the keys, names, types, logos and
  `station_id`. It never returns URLs, for any type.
- `POST /v1/outputs/<id>/operations` gets a new action
  `select_favorite {key}`. It works for both types:
  - `url` favorites play the configured URL, which the client never sees
    or sends;
  - `tunein` favorites resolve as today.
- `set_tunein_station` stays, for selecting straight from search.
- Capabilities advertise the new actions. Favorite edits are gated by the
  same TuneIn opt-out as station selection, because they only add TuneIn
  entries. `select_favorite` on a `url` favorite is a manual operation only.

Web: proxy routes `GET /api/playback/favorites` and
`POST /api/playback/favorites/operations`, plus a new output action, using the
same CSRF/Host checks as the existing operation routes.

Playback dialog, Station section:

1. A **Favorites** list (logo, name, **Play** button) above the search. The
   current station is marked.
2. Each search result gets a ☆/★ toggle next to **Select and play**. The
   current station gets the same toggle in the Playback section.
3. Favorites can be managed with a remove (✕) button and ↑/↓ reorder
   buttons. There is no drag-and-drop, to keep the JS test harness simple.
4. The cards in the streams grid stay unchanged.

## Execution phases

Each phase ends with green tests (`unittest` + `node --test`), a docs update
and a commit. Phases 1 and 2 are small and reduce the risk of the split.

### Phase 1: loopback MPD binding

1. `lib/mpdinstance.py` `config2mpd`: drop `bind_to_addr`; always emit
   `bind_to_address "127.0.0.1"`.
2. Connect to `127.0.0.1` instead of `localhost`, so `::1` resolution
   can't miss the listener. This applies to `mpdbackend.py`,
   `mpdinstance.py` and `playbackaudit.py`.
3. Remove `bind_to_address` from the seed config. Test that the generated
   MPD conf always contains `127.0.0.1`.
4. Close O12 in the hardware audit.

### Phase 2: `id` replaces `name` as the internal key

1. `StreamSettings` (renamed `OutputSettings`): `name` → `id` + `label`.
2. Move to `id` everywhere code currently keys by name:
   - `_select`, intentions, `playbackaudit` conditions, supervisor
     backoff, `identities()`;
   - `mpdctrl --output ID` (C5).
3. `playbackstatus.selection_label` becomes the station name.
4. Unit and conf file names become `mpd_<id>.service` / `mpd_<id>.conf`.
   These match the current Pi names, so no unit rename is needed.
5. Delete `outputrename.py` and `playbackmigrate.py` plus their tests and
   doc sections. Their only purpose was legacy migration (C6).

### Phase 3: split config/state and favorites backend

1. `lib/outputconfig.py`:
   - parses `config.json` (`outputmappers`, `outputs`, `stations`);
   - validates unique IDs, ports, sinks and station keys;
   - checks that every `default_station`, when present, exists, and that
     `stations` has at most 20 entries;
   - applies `default_gain_db` = 0.0 when absent;
   - rejects the `streams` key with "old config format; see
     docs/mpd-headless-deployment.md".
2. `lib/playbackstate.py`:
   - loads, validates and atomically writes `playback.json`;
   - `output(id)` resolves defaults from the config;
   - `update_output(id, …)`, `favorites()`, `add/remove/move_favorite`;
   - enforces the bounds above.
3. `lib/stations.py`: a `Station` dataclass (`key`, `name`, `type`,
   `station_id` | `url`, `logo`) with validation and `resolve(tunein)` →
   URL. For `url` stations the URL is validated with `validate_source_url`.
4. `MPDBackend`:
   - reads both files;
   - every mutation writes only `playback.json`;
   - `set_label` becomes an admin config edit through `mpdctrl` only.
     Alternatively, drop it and let the admin edit `config.json`.
   - New `select_favorite`, sharing the queue-replacement path with
     `_set_tunein_station`.
   - `play` on an output without a station fails with `no_station`. The
     capabilities/inventory report `has_station`, so the web can disable
     Play.
5. `playbackoperations.py`: new actions and validation; favorites
   operations are queued on a global "favorites" lane.
6. Update consumers:
   - `playbackstatus.py` and `playbackagent.py` (inventory, capabilities,
     `GET /v1/favorites`);
   - `playbackaudit.py` (configured URL = `resolved_url`);
   - `streamlevels.py` (per-output proposal; resolves the current or
     default station);
   - `mpdsetup.py` / `mpdinstance.py`: MPD confs need no URL, so remove
     `stream.py` from conf generation.
7. Tests:
   - rewrite fixtures to the new format;
   - the agent never changes `config.json` (hash before/after selection,
     gain, reconcile and favorites);
   - default-station fallback, including an output without
     `default_station` (stays stopped and silent; Play is disabled in the UI and
     rejected by the agent until a station is selected; the first selection starts it) and without
     `default_gain_db` (gain 0.0);
   - favorites cap of 20 (`favorites_full`), idempotency and ordering;
   - removing a favorite leaves the playing output intact;
   - `select_favorite` for both types;
   - no URLs in any agent or web response;
   - the old `streams` key is rejected.

### Phase 4: web favorites UI

1. `web/russound_server.py` / `web/playback_status.py`: proxy routes and
   capability passthrough.
2. `playback.js`, `playback.html`, `styles.css`: favorites list, star
   toggles, reorder and remove, using the existing operation polling and
   feedback.
3. JS tests: render, toggle, reorder, select, disabled states (including Play
   disabled when `has_station` is false).
4. Docs:
   - `web/API.md`;
   - `docs/mpd-playback-operations-api.md`;
   - `docs/mpd-headless-deployment.md` (config format with both station
     types);
   - `docs/mpd-backend-api.md`, `docs/test-coverage.md`.

### Phase 5: Pi rollout (no migration tooling)

1. Record the current stations, gains and intentions from
   `GET /api/playback`. Stop the agent and supervisor. Back up
   `~/.config/mpdrussoundpi/`, `~/.local/state/mpdrussoundpi/`,
   `~/.local/state/mpdrussoundpi-agent/operations.sqlite3` and
   `~/.config/mpd/`.
2. Write the new `config.json` by hand from the repository seed, keeping
   the current gains as `default_gain_db` and a `default_station` for each
   output that should play right away. Delete the old `playback.json`;
   outputs then start on their default station or stay silent.
3. Delete the old operations DB, because its records refer to the old
   action set.
4. Deploy the agent and lib. Run `mpdsetup.py --install` and
   `mpdsetup.py --restart`; this regenerates the loopback MPD confs and
   briefly interrupts audio.
5. Start the supervisor and agent. Then verify:
   - `ss -ltn` shows only `127.0.0.1:6601-6603`;
   - the dashboard shows the outputs with a default station playing it at
     the expected gain, and outputs without one stopped with "No station";
   - add, remove and reorder a favorite;
   - select a favorite on each type;
   - `config.json` stays byte-identical throughout.
6. Remove the `90-tunein-test.conf` drop-in. Make `setup-musicpi.yml`
   generate the supervisor unit from the agent install, so the supervisor
   and agent always run the same code.

## Out of scope

- Per-station gain, MPD password auth (unneeded with loopback),
  user-entered URL favorites (D09).
- Changing the web/agent HTTP API shape. Only revision semantics change
  internally.
