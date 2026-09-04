# MPDRussoundPi

MPDRussoundPi is a Python-based control stack for Russound multi-room audio systems. It combines service setup automation, runtime control scripts, and a browser UI for day-to-day operation.

The repository currently includes:

- Russound + MPD setup automation for target hosts (Ansible playbooks).
- Runtime scripts for audio setup and MPD orchestration.
- A Flask-based, token-protected web control server with live synchronization.
- A controller dashboard with flip-card advanced sound controls and a visible hardware warning banner.
- A configuration editor for zones and sources.
- A status monitor for connected clients and recent frontend actions.

## UI Demonstration

### Controller Dashboard

![Controller Dashboard](docs/screenshots/controller-dashboard.png)

### Configuration Editor

![Configuration Editor](docs/screenshots/configuration-editor.png)

### Status Monitor

![Status Monitor](docs/screenshots/status-monitor.png)

## Key Features

- Multi-zone Russound control (power, source, volume).
- Physical zone addressing via controller/zone mapping.
- Advanced sound controls for bass, treble, balance, and loudness.
- A prominent hardware connectivity warning banner when the backend cannot reach Russound hardware.
- Global system power-off action.
- Shortcut presets targeting multiple zones.
- Config-driven UI visibility and naming.
- Source renaming support in the web configuration editor.
- Live UI updates using Server-Sent Events (SSE).
- API request token enforcement for `/api/*` endpoints.

## Repository Structure

- `web/`: Python web server, controller logic, JSON config/state, and frontend assets.
- `tests/`: unit tests for controller logic, backend abstraction, server routing, and zone model.
- `ansible/`: provisioning and setup playbooks/tasks for deployment targets.
- `mpdrussoundpi/`: legacy/runtime Russound and MPD setup/control scripts.
- `AI_USAGE_DISCLAIMER.md`: policy statement on AI-assisted development.

## Architecture Overview

### Backend

- `web/russound_server.py`
  - Flask HTTP server.
  - Serves UI pages and static assets.
  - Exposes `/api/*` endpoints and `/api/events` SSE stream.
  - Enforces API token authentication.
- `web/russound_controller.py`
  - Domain/state orchestration.
  - Config loading and persistence.
  - Zone and shortcut mutation logic.
  - View payload building for frontend.
- `web/russound_backend.py`
  - Russound hardware adapter/wrapper.
  - Reads zone state and applies power/source/volume commands.

### Frontend

- `web/static/index.html` + `web/static/app.js`
  - Main control dashboard with the flip-card advanced audio controls and backend warning banner.
- `web/static/config.html` + `web/static/config.js`
  - Configuration editor for zones and input names.
- `web/static/status.html` + `web/static/status.js`
  - Runtime status page for clients/events.
- `web/static/styles.css`
  - Shared visual styling across all pages.

## Requirements

- macOS/Linux environment.
- Python 3.11+ recommended.
- Virtual environment support (`venv`).
- Access to Russound gateway/network where applicable.

## Quick Start (Local Development)

1. Clone the repository.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Start the web server.

Example:

```bash
git clone <your-fork-or-origin-url>
cd MPDRussoundPi
python3 -m venv .venv
source .venv/bin/activate
pip install russound flask
python web/russound_server.py --config web/russound_config.json --state web/russound_state.json --port 8000
```

Then open:

- `http://127.0.0.1:8000/` (Controller)
- `http://127.0.0.1:8000/config` (Configuration)
- `http://127.0.0.1:8000/status` (Status)

## Configuration

The web server expects a JSON config file (for example `web/russound_config.json`).

Primary sections:

- `controllers`: hardware controllers with `id` and `zone_count`.
- `zones` (optional): user-facing zone definitions with `name`, `controller`, `zone`, and optional `visible`.
- `inputs`: available source list with numeric `id` and display `name`.
- `shortcuts` (optional): preset actions with `id`, `name`, `zone_addresses`, and optional `source`.

In `web/config_example.json`, `zones` and `shortcuts` are included as examples and can be omitted when not needed.

Use `web/config_example.json` as the base template for new environments.

## API Summary

The complete API reference is documented in `web/API.md`.

Common endpoints:

- `GET /api/state`
- `GET /api/config`
- `GET /api/status`
- `GET /api/events` (SSE)
- `POST /api/system/power`
- `POST /api/source`
- `POST /api/shortcuts/{shortcutId}/activate`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/power`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/source`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/volume`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/bass`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/treble`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/loudness`
- `POST /api/controller/{controllerId}/zone/{zoneNumber}/balance`

## Testing

Run the test suite with:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -q
```

Or run focused modules:

```bash
python -m unittest tests.test_zone tests.test_russound_backend tests.test_russound_controller tests.test_russound_server -q
```

See [the module-by-module coverage review](docs/test-coverage.md) for requirements, test links, and known blind spots.

## Docker

> **⚠️ Untested.** The container setup, the ser2net gateway container, and the `deploy-russound-docker.yml` playbook have not yet been run against real hardware. The images have never been built and serial device passthrough is unverified. Treat this as a starting point and validate before relying on it.

Each part runs in its own container:

| Image | Dockerfile | Purpose |
| --- | --- | --- |
| `mpdrussoundpi-web` | `Dockerfile` | Web application |
| `mpdrussoundpi-ser2net` | `Dockerfile.ser2net` | Serial gateway (RS-232 to TCP) |
| `mpdrussoundpi-dummy` | `Dockerfile.dummy` | Hardware-free [dummy backend](tool/dummy_backend/README.md) |

Two stacks combine them: `docker-compose.yml` runs the web application with the ser2net gateway (hardware), `docker-compose.dummy.yml` runs it with the dummy backend (no hardware).

```bash
cp web/config_example.json docker/config/russound_config.json
docker compose up -d
```

The UI is then available on `http://<host>:8000/`.

### Serial gateway container (ser2net)

The `russound-ser2net` container bridges the USB serial cable to TCP, so no gateway is needed on the host. The web container connects to it on `russound-ser2net:6666` over the compose network.

The USB device is changeable without rebuilding the image:

```bash
RUSSOUND_SERIAL_DEVICE=/dev/ttyUSB1 docker compose up -d
```

The same variable also controls the `devices:` passthrough, so the device is mapped into the container at the same path.

Set `RUSSOUND_SERIAL_GID` to the group owning the device on the host (`stat -c '%g' /dev/ttyUSB0`, usually `dialout`) so the non-root container user may open it.

To use a ser2net instance on the host or another machine instead, point the web container elsewhere and skip the gateway service:

```bash
RUSSOUND_BACKEND_HOST=host.docker.internal docker compose up -d russound-web
```

### Dummy backend container (no hardware)

`docker-compose.dummy.yml` starts the dummy backend together with the web container, which is pointed at `russound-dummy:6666`:

```bash
cp web/config_example.json docker/config/russound_config.json
docker compose -f docker-compose.dummy.yml up -d
```

The curses TUI runs inside a `tmux` session in the container and can be attached to at any time:

```bash
docker exec -it russound-dummy tmux attach -t dummy
```

Detach again with `Ctrl-b d`; pressing `Q` in the TUI quits the backend and stops the container. `S` saves the state to `docker/dummy-data/dummy_state.json`, which is seeded from `tool/dummy_backend/example_state.json` on first start. Set `RUSSOUND_DUMMY_TUI=false` to run the TCP server only.

### Volumes

Both mounts must be **directories**, because config and state are written atomically through a temporary file in the same directory. They belong to the web container.

- `/config`: holds `russound_config.json` (writable so the configuration editor can save).
- `/data`: holds `russound_state.json` and optional protocol audit logs.

### Endpoint configuration

Host and port are configurable through environment variables, which take precedence over the `backend` block of the config file.

| Variable | Purpose | Default |
| --- | --- | --- |
| `RUSSOUND_WEB_HOST` | Web server bind address | `0.0.0.0` |
| `RUSSOUND_WEB_PORT` | Web server port | `8000` |
| `RUSSOUND_BACKEND_HOST` | Russound/ser2net gateway host | `russound-ser2net` |
| `RUSSOUND_BACKEND_PORT` | Russound/ser2net gateway port | `6666` |
| `RUSSOUND_SERIAL_DEVICE` | USB serial device path (ser2net container) | `/dev/ttyUSB0` |
| `RUSSOUND_SERIAL_OPTIONS` | Serial line settings (ser2net container) | `19200n81` |
| `RUSSOUND_SER2NET_BIND` | ser2net bind address | `0.0.0.0` |
| `RUSSOUND_SER2NET_PORT` | ser2net listening port | `6666` |
| `RUSSOUND_SERIAL_GID` | Host GID owning the serial device | `20` |
| `RUSSOUND_CONFIG` | Config file path | `/config/russound_config.json` |
| `RUSSOUND_STATE` | State file path | `/data/russound_state.json` |
| `RUSSOUND_DEBUG` | Enable debug logging | `false` |
| `RUSSOUND_WAITRESS_THREADS` | Waitress worker threads | `16` |

Every web variable has an equivalent command-line flag (`--host`, `--port`, `--config`, `--state`, `--debug`), and the flag wins when both are provided.

On Linux hosts, set `RUSSOUND_UID`/`RUSSOUND_GID` to your `id -u`/`id -g` so the container may write the bind-mounted directories.

### Raspberry Pi provisioning

`ansible/playbooks/deploy-russound-docker.yml` prepares a Pi for the container deployment: it installs Docker Engine and the Compose plugin, clones the repository, seeds `docker/config/russound_config.json`, writes a `.env` file, and starts the `docker-compose.yml` stack (web application plus ser2net gateway).

```bash
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-russound-docker.yml
```

It disables the host `ser2net` and `russound-web` services when present, because the container now provides both.

Override the defaults per host or on the command line:

```bash
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-russound-docker.yml \
  -e russound_serial_device=/dev/ttyUSB1 -e russound_publish_port=8080
```

The playbook detects the serial device's group and the deployment user's UID/GID, so device access and the bind mounts work without manual tuning.

## Deployment Notes

- API access is token-protected, but this is a same-origin request token model, not full user authentication.
- For non-local deployments, place the service behind a TLS-terminating reverse proxy.
- Restrict network access to trusted clients.

## AI-Assisted Development Disclosure

This repository includes AI-assisted contributions. See `AI_USAGE_DISCLAIMER.md` for details.

## License

See `LICENSE`.
