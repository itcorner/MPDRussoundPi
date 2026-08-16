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
- API routes require a per-process token issued by the server to the served frontend.
- Unauthorized API requests return `401` with `{ "error": "Unauthorized" }`.

## Response categories

The server exposes three response types:

- JSON API responses from `/api/*`
- HTML pages from `/`, `/index.html`, `/config`, and `/status`
- Server-Sent Events from `/api/events`

Most mutating API routes return the latest full view payload after the change is applied, including the latest backend connectivity banner state.

Exceptions:

- `GET /api/events` returns an event stream, not JSON
- `GET /api/status` returns connected-client and event-history data, not the main controller view payload

The server currently expects these request body field types:

- `power`: boolean
- `source`: integer
- `volume`: integer
- `bass`: integer
- `treble`: integer
- `loudness`: boolean
- `balance`: integer

The dashboard also exposes `backend_status` in the main view payload so the frontend can display a warning banner whenever hardware communication is unavailable.

## API authorization

The backend now protects `/api/*` routes with a server-generated token.

- The token is injected into the HTML served at `/` and `/index.html`.
- Frontend `fetch(...)` requests send it in the `X-Russound-Api-Token` header.
- The SSE endpoint accepts the same token through the `token` query parameter because `EventSource` cannot set custom headers.
- The token is tied to the currently running server process, not to a specific host name or IP.
- If the server is restarted, a new token is generated and the old token becomes invalid.

## Session tracking

The backend tracks frontend sessions using an HTTP cookie (`russound_session_id`).

- The cookie is set by the server on normal page/API responses.
- SSE client deduplication for status monitoring uses this cookie session id.
- Frontend code does not need to generate or send custom session headers.

Examples:

```http
GET /api/state
X-Russound-Api-Token: <server-issued-token>
```

```http
GET /api/events?token=<server-issued-token>
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
  "message": "A Russound config file is required. Copy web/config_example.json and start the server with --config.",
  "state": {
    "system_power": false,
    "zones": [],
    "inputs": []
  }
}
```

## Endpoints

### GET /api/state

Returns the current view payload (`config`, `config_required`, `state`).

- Requires API token authentication.
- Response type: JSON.

### GET /api/events

Server-Sent Events stream used for multi-client synchronization.

- Requires API token authentication.
- Content type: `text/event-stream; charset=utf-8`
- Event name: `state-change`
- Data payload:

```json
{ "revision": 12 }
```

The server also sends keepalive comments (`: ping`) periodically.

### GET /api/status

Returns the combined status payload for backwards compatibility.

- Requires API token authentication.
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

- Requires API token authentication.
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

- Requires API token authentication.
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

`connected_clients` reflects active SSE connections. `recent_events` is an in-memory rolling history capped at 50 entries.
When available, each connected client includes its cookie-based `session_id` value.

### GET /api/config

Returns the editable zone-configuration payload used by the configuration page.

- Requires API token authentication.
- Response type: JSON.

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

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "power": true }
```

### POST /api/source

Sets one shared source for all zones.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "source": 1 }
```

`source` must be a numeric input id (starting at 1) present in `state.inputs`.

### POST /api/shortcuts/{shortcutId}/activate

Activates a shortcut from `config.shortcuts` by id.

- Requires API token authentication.
- Returns the updated main view payload.
- No request body is required.
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

Updates the zone configuration used by the frontend overview and persists it to the config file.

- Requires API token authentication.
- Returns the updated config-editor payload.
- Rejects invalid controller/zone slots with `400 Bad Request`.
- Only slots within each controller's configured `zone_count` can be enabled, so the frontend cannot create more zones than the controller supports.

Request body:

```json
{
  "zone_slots": [
    {"controller": 1, "zone": 1, "enabled": true, "visible": true, "name": "Living Room"},
    {"controller": 1, "zone": 2, "enabled": false, "visible": true, "name": "Controller 1 Zone 2"}
  ],
  "source_slots": [
    {"id": 1, "name": "Radio"},
    {"id": 2, "name": "Television"}
  ]
}
```

`source_slots` is optional; if provided, each entry must reference an existing numeric source id.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/power

Sets power for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "power": true }
```

`controllerId` and `zoneNumber` identify the physical Russound zone address.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/source

Sets source for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "source": 2 }
```

`source` must be a numeric input id present in `state.inputs`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/volume

Sets volume for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "volume": 45 }
```

Volume is clamped to the range `0..100`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/bass

Sets bass for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "bass": -2 }
```

Bass is normalized to the range `-10..10`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/treble

Sets treble for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "treble": 3 }
```

Treble is normalized to the range `-10..10`.

### POST /api/controller/{controllerId}/zone/{zoneNumber}/loudness

Sets loudness for a single zone.

- Requires API token authentication.
- Returns the updated main view payload.

Request body:

```json
{ "loudness": true }
```

### POST /api/controller/{controllerId}/zone/{zoneNumber}/balance

Sets balance for a single zone.

- Requires API token authentication.
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
- The API token protects browser-to-server API calls, but it is delivered to the served frontend and is therefore a same-origin request token, not user authentication.
- Anyone who can load the controller page can also use its current token until the server process restarts.
- Restarting the server rotates the token automatically.
- The status API and status page expose client IP addresses and recent frontend-triggered actions; restrict access accordingly when deploying outside a trusted network.

## Config reference

Use `web/config_example.json` as the canonical config template and field reference.
