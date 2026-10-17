# Russound Web API

This API powers the Russound control dashboard for managing a multi-room audio system.

## Base URL

- Local development: `http://127.0.0.1:8000`
- Local network by IP: `http://<controller-ip>:8000`
- Remote or DNS-based deployment: `http://<controller-dns-name>:8000`

Examples:

- `http://192.168.1.25:8000`
- `http://russound-controller.local:8000`
- `http://audio.example.internal:8000`

## HTTP behavior

- API responses are JSON (`application/json; charset=utf-8`).
- Successful JSON API requests return status `200`.
- Malformed request bodies return `400` with an error describing the expected field type.
- Unknown routes return `404` with `{ "error": "Not found" }`.
- Zone-specific routes return `404` with `{ "error": "Zone not found" }` when the controller/zone address in the URL does not exist.
- Source-changing routes return `404` with `{ "error": "Source not found" }` when the requested source id does not exist.
- Shortcut activation returns `404` with `{ "error": "Shortcut not found" }` when the shortcut id does not exist.
- Shortcut activation returns `500` when a configured shortcut references an unknown controller/zone address or source, because that indicates a server-side configuration error.

## Response categories

The server exposes three response types:

- JSON API responses from `/api/*`
- HTML pages from `/`, `/index.html`, `/config`, and `/status`
- Server-Sent Events from `/api/events`

Most mutating API routes return the latest full view payload after the change is applied, including the latest backend connectivity banner state.

Exceptions:

- `GET /api/events` returns an event stream, not JSON
- `GET /api/status` returns connected-client and event-history data, not the main controller view payload
- `GET /api/playback`, `/api/playback/health`, and
  `/api/playback/agents/{agent_id}/outputs/{output_id}` return playback-only
  status payloads, independently of the Russound view payload

The server currently expects these request body field types:

- `power`: boolean
- `source`: integer
- `volume`: integer
- `bass`: integer
- `treble`: integer
- `loudness`: boolean
- `balance`: integer

The dashboard also exposes `backend_status` in the main view payload so the frontend can display a warning banner whenever hardware communication is unavailable.

## Request protection

There is no API token. The server protects against cross-site request forgery
and DNS rebinding instead, so open pages keep working across server restarts.

- `GET`/`HEAD` requests (including `/api/events`) are always allowed; other
  sites cannot read the responses because the server sends no CORS headers.
- State-changing requests (`POST`) are rejected with `403` unless one of:
  - the `Origin` header matches the server origin or `RUSSOUND_ALLOWED_ORIGINS`,
  - `Sec-Fetch-Site` is `same-origin` or `none`,
  - neither header is present (non-browser clients such as `curl` or scripts).
- State-changing requests must use `Content-Type: application/json`, otherwise
  `415` is returned (prevents plain HTML form posts from other sites).
- If `RUSSOUND_ALLOWED_HOSTS` is set, requests with any other `Host` header are
  rejected with `400`.
- Behind a reverse proxy set `RUSSOUND_TRUSTED_PROXY_COUNT` so the server sees
  the original scheme/host via `X-Forwarded-*`.

## Session tracking

The backend tracks frontend sessions using an HTTP cookie (`russound_session_id`).

- The cookie is set by the server on normal page/API responses.
- SSE client grouping on the status page uses this cookie session id.
- Frontend code does not need to generate or send custom session headers.

Examples:

```http
GET /api/state
```

```http
POST /api/system/power
Content-Type: application/json

{"power": false}
```

## View payload

Most endpoints return the same payload shape:

```json
{
  "config": {
    "controllers": [
      {
        "id": 1,
        "ip": "192.168.1.50",
        "port": 9621
      }
    ],
    "inputs": [
      { "id": 1, "name": "Radio" },
      { "id": 2, "name": "TV" }
    ],
    "zones": [
      {
        "name": "Living Room",
        "controller": 1,
        "zone": 1
      }
    ],
    "shortcuts": [
      {
        "id": "party",
        "name": "Party",
        "source": 1,
        "zone_addresses": [
          { "controller": 1, "zone": 1 }
        ]
      }
    ]
  },
  "config_required": false,
  "state": {
    "system_power": false,
    "inputs": [
      { "id": 1, "name": "Radio" },
      { "id": 2, "name": "TV" }
    ],
    "zones": [
      {
        "name": "Living Room",
        "power": false,
        "source": 1,
        "volume": 20,
        "bass": 0,
        "treble": 0,
        "loudness": false,
        "balance": 0,
        "controller": 1,
        "zone": 1
      }
    ]
  }
}
```

If no valid config file is supplied to the server (`--config`), `GET /api/state` returns:

```json
{
  "config": null,
  "config_required": true,
  "message": "A config file is required. Complete setup on the Configuration page.",
  "state": {
    "system_power": false,
    "zones": [],
    "inputs": []
  }
}
```

## Endpoints

### Optional MPD playback status

Playback status is disabled unless the web-process environment variable
`RUSSOUND_PLAYBACK_AGENTS` contains a non-empty JSON array. Each entry has an
`agent_id`, `url`, and `token_file` path; HTTPS agents may also specify
`tls_ca_file` to trust a mounted private CA or self-signed server certificate.
For example:

```json
[
  {
    "agent_id": "musicpi",
    "url": "https://musicpi.example.internal",
    "token_file": "/run/secrets/musicpi-playback-token",
    "tls_ca_file": "/run/secrets/musicpi-proxy.crt"
  }
]
```

Plain HTTP is accepted only for loopback URLs; remote agents require HTTPS.
The token-file path is a secret reference and the token value is never included
in an API response or browser code. Invalid/partial entries, duplicate agent
identities, or unreadable/invalid token files are startup errors. The web
The TLS file is used as a trust anchor with normal certificate and hostname
validation; it is not an insecure verification bypass. The hostname in `url`
must resolve from Docker and match the certificate's subject alternative name.
`tls_ca_file` is rejected for HTTP URLs. For the selected Pi Caddy design, the agent remains at
`127.0.0.1:8081` and Caddy serves HTTPS using the matching certificate/key.
The certificate file can be mounted from `./docker/secrets` read-only; keep
the private key on the Pi and outside the web container.
Invalid/partial entries, duplicate agent
identities, or unreadable/invalid token or TLS trust files are startup errors.
The web process runs one background snapshot fetcher per configured agent and serves
cached snapshots; requests and browser tabs do not poll agents independently.
For Docker deployment, mount token and trust files into the web container
read-only and use their in-container paths in `token_file` and `tls_ca_file`.
Agent authentication, identity, and response-shape failures appear as playback
health errors and do not block Russound state or control routes.

#### GET /api/playback

Returns the cached playback inventory, or an explicit disabled response.

```json
{
  "api_version": 1,
  "configured": true,
  "revision": 8,
  "agents": [{
    "agent_id": "musicpi",
    "status": "ok",
    "last_attempt_at": "2026-10-14T18:50:00Z",
    "last_success_at": "2026-10-14T18:50:00Z",
    "error": null,
    "snapshot": {
      "agent_id": "musicpi",
      "revision": 42,
      "snapshot_age_ms": 500,
      "stale": false,
      "outputs": [],
      "favorites": {
        "config_revision": "<sha256>",
        "control_revision": "<sha256>",
        "stations": [
          {"key": "radio886", "name": "Radio 88.6", "type": "url", "station_id": null},
          {"key": "tunein:s8007", "name": "ORF Hitradio OE3", "type": "tunein", "station_id": "s8007"}
        ]
      }
    }
  }]
}
```

`snapshot.favorites` is polled only from agents that advertise
`select_favorite`; it is `null` until the agent's first observation. Favorites
never contain stream URLs. Each output carries `control.has_station`; when it is
`false` the output has no station and cannot play until one is selected.

When not configured, the response is
`{"api_version":1,"configured":false,"revision":0,"agents":[]}`. On an agent
failure, `status` is `unavailable`, `error` contains a stable code and
user-safe message, and the last successful snapshot is retained when available.

#### GET /api/playback/health

Returns playback integration configuration, aggregate connection status,
revision, and per-agent user-safe errors independently of Russound
`backend_status`.

#### GET /api/playback/agents/{agent_id}/outputs/{output_id}

Returns one output from the web process's cached inventories with its playback
integration status. Returns `404` when playback is disabled or no configured
agent has that output id.

The output's `selection.source` and `playback.value.current_source` fields are
redacted display values containing only the HTTP(S) scheme and host. They omit
path, user-info, query and fragment. These are the selected station's source
and MPD's observed current source, respectively; they can differ after a failed
source switch. `selection` also carries the station `key`, `provider` (`url` or
`tunein`), `station_id` and `station_name` (all `null` without a station).

#### POST /api/playback/agents/{agent_id}/outputs/{output_id}/operations

Submits a bounded, revision-checked operation to the configured playback agent.
Supported actions depend on its advertised `write_operations` capabilities.
S08/S09 provide TuneIn station selection; arbitrary browser-supplied stream
URLs are rejected. Search with
`GET /api/playback/catalogs/tunein/search?q=<station-name>`, or with a
station ID such as `q=s25744` to get only that station (empty if it has no
supported stream). Results contain
only station IDs and display metadata. Submit `set_tunein_station` with the
selected station ID/name plus current config/control revisions and an
idempotency key. The Pi resolves the stream URL from TuneIn (directly or via
a TuneIn-listed M3U/PLS playlist) and always starts playback. Stations without
a supported stream fail with `station_unavailable` and leave playback unchanged. Selection is enabled on agents with manual operations unless they run with
`--disable-tunein-selection`.
`select_favorite` with a favorite `key` plays any favorite (including URL
stations from the Pi configuration) on that output.
The durable result reports whether the desired station was saved separately
from whether MPD application succeeded; raw stream URLs are never returned.

#### POST /api/playback/agents/{agent_id}/favorites/operations

Submits a durable edit of the agent-wide favorites list (at most 20 stations),
with the same revision, idempotency, size and same-origin rules:

- `add_favorite` with `station_id` and `station_name` (TuneIn only);
- `remove_favorite` with `key`;
- `move_favorite` with `key` and `index` (0-19).

The returned operation has `output_id: null`; poll it with
`GET /api/playback/agents/{agent_id}/operations/{operation_id}`. Errors include
`favorites_full` and `favorite_not_found`.

### GET /api/state

Returns the current view payload (`config`, `config_required`, `state`).

- Response type: JSON.
- While a backend operation holds the state lock, returns the last published
  snapshot immediately rather than waiting for hardware I/O. Successful commands,
  configuration saves, and completed polls publish updated snapshots before
  releasing that lock. Hardware refreshes remain serialized with commands to
  prevent stale polls from overwriting newer changes; SSE announces refreshed state.

### GET /api/events

Server-Sent Events stream used for multi-client synchronization.

- Content type: `text/event-stream; charset=utf-8`
- Event names: `state-change` and `playback-change`.
- `state-change` carries the latest Russound view payload.
- `playback-change` is a coalesced invalidation; clients fetch `/api/playback`
  for the latest cached snapshot.
- Both event types share a bounded per-browser queue. When a slow client falls
  behind, queued notifications are replaced by the latest state notification.
- On connection/reconnection, the frontend fetches both `/api/state` and
  `/api/playback`.

```json
{ "event": "playback-change", "api_version": 1, "revision": 8 }
```

The server also sends keepalive comments (`: ping`) periodically.

- The stream starts with `retry: 2000` and is closed by the server after
  5 minutes; `EventSource` reconnects automatically and the frontend
  re-fetches both state snapshots on every (re)connect.
- Every stream is registered separately, so several tabs of the same browser
  all receive updates.
- Concurrent streams are limited (`RUSSOUND_MAX_EVENT_STREAMS`, at most
  worker threads − 4). Above the limit the server answers `503` with
  `Retry-After: 30`; the frontend keeps polling and retries the stream later.
- The frontend closes its stream while the page is hidden.

### GET /api/status

Returns the combined status payload for backwards compatibility.

- Response type: JSON.

Response shape:

```json
{
  "connected_clients": [
    {
      "id": 1,
      "ip": "127.0.0.1",
      "connected_at": "2026-08-08T14:55:12+00:00",
      "user_agent": "Mozilla/5.0"
    }
  ],
  "recent_events": [
    {
      "timestamp": "2026-08-08T14:56:03+00:00",
      "ip": "127.0.0.1",
      "path": "/api/controller/1/zone/1/volume",
      "payload": {"volume": 32}
    }
  ]
}
```

### GET /api/status/clients

Returns the currently connected frontend clients.

- Response type: JSON.

Response shape:

```json
{
  "connected_clients": [
    {
      "id": 1,
      "ip": "127.0.0.1",
      "connected_at": "2026-08-08T14:55:12+00:00",
      "user_agent": "Mozilla/5.0"
    }
  ]
}
```

### GET /api/status/history

Returns the last 50 frontend-triggered actions.

- Response type: JSON.

Response shape:

```json
{
  "recent_events": [
    {
      "timestamp": "2026-08-08T14:56:03+00:00",
      "ip": "127.0.0.1",
      "path": "/api/controller/1/zone/1/volume",
      "payload": {"volume": 32}
    }
  ]
}
```

`connected_clients` reflects active SSE connections, grouped per browser session (`streams` is the number of open streams, e.g. tabs). `recent_events` is an in-memory rolling history capped at 50 entries.
When available, each connected client includes its cookie-based `session_id` value.

### GET /api/config

Returns the editable zone-configuration payload used by the configuration page.

- Response type: JSON.
- Missing/invalid config returns `config_required: true`, a descriptive `message`,
  and editable defaults (one six-zone controller and six disabled source slots).
  These defaults are not persisted or used to connect to hardware until saved.
- `config_writable` indicates whether a destination path is configured, not an
  OS permission check; save errors describe filesystem/permission failures.
- `backend_overrides` lists non-empty `RUSSOUND_BACKEND_HOST` and
  `RUSSOUND_BACKEND_PORT` values. Environment overrides take precedence over saved settings.
- Read `/api/state.backend_status` for recorded backend connectivity independently of configuration validity.

Response shape:

```json
{
  "config": {
    "controllers": [{"id": 1, "zone_count": 6}],
    "zones": [{"name": "Living Room", "controller": 1, "zone": 1, "keypad_id": 1, "visible": true}],
    "inputs": [{"id": 1, "name": "Radio"}],
    "shortcuts": []
  },
  "config_required": false,
  "zone_slots": [
    {"controller": 1, "zone": 1, "keypad_id": 1, "enabled": true, "visible": true, "name": "Living Room"},
    {"controller": 1, "zone": 2, "keypad_id": 1, "enabled": false, "visible": true, "name": "Controller 1 Zone 2"}
  ],
  "source_slots": [
    {"id": 1, "name": "Radio"},
    {"id": 2, "name": "TV"}
  ]
}
```

`zone_slots` is derived from `controllers[].zone_count`, so the editor only exposes valid hardware slots.
`keypad_id` identifies the zone keypad (`1..6`) and defaults to `1` when omitted.
`source_slots` is derived from `inputs` and allows renaming source display names without changing source ids.

### POST /api/system/power

Sets system power for all zones.

- Returns the updated main view payload.
- System Off sends addressed power-off commands to every zone in each configured
  controller's scope, including zones hidden from the overview. Without controller
  scope it retains the broadcast fallback. Backend write failures return `502`
  rather than persisting or broadcasting a successful power change.

Request body:

```json
{ "power": true }
```

### POST /api/source

Sets one shared source for all zones.

- Returns the updated main view payload.

Request body:

```json
{ "source": 1 }
```

`source` must be a numeric input id (starting at 1) present in `state.inputs`.

### POST /api/shortcuts/{shortcutId}/activate

Activates a shortcut from `config.shortcuts` by id.

- Returns the updated main view payload.
- No request body is required, but the request must still be sent with `Content-Type: application/json`.
- If the shortcut id is unknown, state is unchanged and the current view payload is still returned.

Shortcut targets now use physical Russound addresses:

```json
{
  "id": "party",
  "name": "Party",
  "zone_addresses": [
    { "controller": 1, "zone": 1 },
    { "controller": 2, "zone": 1 }
  ],
  "source": 1
}
```

### POST /api/config

Creates or updates the backend, controller topology, zones, and sources, and persists the configuration.

The optional `shortcuts` array replaces the saved shortcuts (`[]` deletes all).
Each shortcut needs a unique non-empty string `id`, a non-empty `name`, an enabled
numeric `source`, and at least one unique enabled target in `zone_addresses`
(`{"controller": 1, "zone": 1}`). Invalid references return `400` before saving.
Omitting `shortcuts` preserves the existing shortcuts, pruning removed zone targets.

- Returns the updated config-editor payload.
- Rejects invalid controller/zone slots with `400 Bad Request`.
- Only slots within each controller's configured `zone_count` can be enabled, so the frontend cannot create more zones than the controller supports.
- Initial setup requires `backend` and a non-empty `controllers` list. They are
  optional when updating an existing valid configuration.
- `backend` accepts TCP `host`/`port` or serial `device`/`baud`. Controller ids and
  zone counts are integers from `1` to `6`; TCP ports are from `1` to `65535`.
- `backend.poll_interval_seconds` is a finite number of at least `1` second
  (default `2`). Fractional seconds are supported; saves apply it to the watcher immediately.
- Saves perform no hardware I/O. Endpoint or protocol-audit-log changes detach
  the old connection and reconnect through the watcher. Other edits keep the
  connection and recorded health, updating controller scope and poll interval in
  place; environment overrides still apply.
- Replacing an invalid config preserves a sibling `.invalid-*.bak` copy.
- Filesystem failures return `500` with an actionable JSON `error`; writable config/data directories are required.

Request body:

```json
{
  "backend": {"host": "russound-ser2net", "port": 6666},
  "controllers": [{"id": 1, "zone_count": 6}],
  "zone_slots": [
    {"controller": 1, "zone": 1, "enabled": true, "visible": true, "name": "Living Room"},
    {"controller": 1, "zone": 2, "enabled": false, "visible": true, "name": "Controller 1 Zone 2"}
  ],
  "source_slots": [
    {"id": 1, "name": "Radio", "enabled": true},
    {"id": 2, "name": "Television", "enabled": true}
  ]
}
```

`source_slots` is optional; each supplied entry needs an integer id from `1` to `6`,
a string name, and a boolean `enabled`. New source ids can be enabled during setup.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/power

Sets power for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "power": true }
```

`controllerId` and `zoneNumber` identify the physical Russound zone address.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/source

Sets source for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "source": 2 }
```

`source` must be a numeric input id present in `state.inputs`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/volume

Sets volume for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "volume": 45 }
```

Volume is clamped to the range `0..100`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/bass

Sets bass for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "bass": -2 }
```

Bass is normalized to the range `-10..10`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/treble

Sets treble for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "treble": 3 }
```

Treble is normalized to the range `-10..10`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/loudness

Sets loudness for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "loudness": true }
```

### POST /api/controller/{controllerId}/zone/{zoneNumber}/balance

Sets balance for a single zone.

- Returns the updated main view payload.

Request body:

```json
{ "balance": -2 }
```

Balance is normalized to the range `-10..10`.

## Pages and static files

- `GET /` and `GET /index.html` serve the dashboard page.
- `GET /config` serves the configuration editor.
- `GET /status` serves the status dashboard.
- `GET /static/*` serves frontend assets (JS/CSS).

## Server startup

The web server is started from `web/russound_server.py`.

Supported command-line options:

- `--host`: bind address, default `0.0.0.0`
- `--port`: TCP port, default `8000`
- `--config`: path to the Russound web configuration JSON file
- `--state`: path to the persisted state JSON file, default `web/russound_state.json`

Example:

```bash
python web/russound_server.py \
  --host 0.0.0.0 \
  --port 8000 \
  --config web/russound_config.json \
  --state web/russound_state.json
```

## Deployment and security notes

- The current server uses HTTP, not HTTPS. For non-local deployments, place it behind a TLS-terminating reverse proxy if transport encryption is required.
- There is no user authentication; state-changing requests are protected against cross-site requests (see "Request protection"). Anyone who can reach the web port can control the system.
- The status API and status page expose client IP addresses and recent frontend-triggered actions; restrict access accordingly when deploying outside a trusted network.

## Config reference

Use `web/config_example.json` as the canonical config template and field reference.
