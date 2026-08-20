from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import html
import importlib
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import sys
import threading
import time
from typing import Any, cast
from urllib.parse import parse_qs

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.config_types import coerce_russound_config, env_port, env_str, resolve_backend_poll_interval_seconds
from web.russound_backend import RussoundBackend
from web.russound_controller import get_controller
from web.russound_state import RussoundState

WEB_ROOT = Path(__file__).resolve().parent
WEB_STATIC = WEB_ROOT / "static"

SESSION_COOKIE_NAME = "russound_session_id"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
BACKEND_CHANGE_POLL_INTERVAL_SECONDS = 2.0


class RussoundRequestHandler:
    """Compatibility helpers retained for unit tests and route validation."""

    headers: dict[str, str]

    def _read_bool_field(self, payload: dict[str, Any], field_name: str) -> bool | None:
        value = payload.get(field_name)
        if isinstance(value, bool):
            return value
        return None

    def read_bool_field(self, payload: dict[str, Any], field_name: str) -> bool | None:
        return self._read_bool_field(payload, field_name)

    def _read_int_field(self, payload: dict[str, Any], field_name: str) -> int | None:
        value = payload.get(field_name)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None

    def read_int_field(self, payload: dict[str, Any], field_name: str) -> int | None:
        return self._read_int_field(payload, field_name)

    def _read_zone_setting_value(self, payload: dict[str, Any], action: str) -> Any | None:
        if action in {"power", "loudness"}:
            return self._read_bool_field(payload, action)
        if action in {"source", "volume", "bass", "treble", "balance"}:
            return self._read_int_field(payload, action)
        return None

    def read_zone_setting_value(self, payload: dict[str, Any], action: str) -> Any | None:
        return self._read_zone_setting_value(payload, action)

    def _state_has_zone_address(self, state: dict[str, Any] | RussoundState, controller_id: int, zone_number: int) -> bool:
        if isinstance(state, RussoundState):
            return state.has_zone_address(controller_id, zone_number)
        return any(
            int(zone.get("controller", 0)) == controller_id and int(zone.get("zone", 0)) == zone_number
            for zone in state.get("zones", [])
        )

    def _state_has_zone_addresses(self, state: dict[str, Any] | RussoundState, zone_addresses: list[tuple[int, int]]) -> bool:
        if isinstance(state, RussoundState):
            return state.has_zone_addresses(zone_addresses)
        known_zone_addresses = {
            (int(zone.get("controller", 0)), int(zone.get("zone", 0)))
            for zone in state.get("zones", [])
        }
        return all(zone_address in known_zone_addresses for zone_address in zone_addresses)

    def _state_has_input(self, state: dict[str, Any] | RussoundState, source_id: Any) -> bool:
        if isinstance(state, RussoundState):
            return state.has_input(source_id)
        return any(input_item.get("id") == source_id for input_item in state.get("inputs", []))

    def _match_controller_zone_route(self, path: str) -> tuple[int, int, str] | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 6 or parts[0] != "api" or parts[1] != "controller" or parts[3] != "zone":
            return None
        try:
            controller_id = int(parts[2])
            zone_number = int(parts[4])
        except ValueError:
            return None
        action = parts[5]
        if action not in {"power", "source", "volume", "bass", "treble", "loudness", "balance"}:
            return None
        return controller_id, zone_number, action

    def _is_authorized(self, server: "RussoundHTTPServer", parsed: Any) -> bool:
        header_token = self.headers.get("X-Russound-Api-Token")
        if header_token == server.api_token:
            return True
        query_token = parse_qs(parsed.query).get("token", [None])[0]
        return query_token == server.api_token


class RussoundHTTPServer:
    def __init__(
        self,
        config_path: str | Path | None,
        state_path: str | Path | None,
    ) -> None:
        self.config_path = config_path
        self.state_path = state_path
        self.api_token = secrets.token_urlsafe(32)
        self.state_revision = 0
        self.state_lock = threading.Lock()
        self._event_clients: dict[int, dict[str, Any]] = {}
        self._event_client_index: dict[str, int] = {}
        self.controller = get_controller(config_path, state_path)
        self._event_clients_lock = threading.Lock()
        self._event_client_id = 0
        self._event_history: deque[dict[str, Any]] = deque(maxlen=50)
        self._event_history_lock = threading.Lock()
        self._backend_watcher_lock = threading.Lock()
        self._backend_watcher_started = False
        self._event_backend: RussoundBackend | None = None
        self._event_backend_lock = threading.Lock()
        self._backend_poll_interval_seconds = BACKEND_CHANGE_POLL_INTERVAL_SECONDS
        self._helpers = RussoundRequestHandler()
        self.app = self._build_flask_app()

    def _build_flask_app(self) -> Flask:
        app = Flask(__name__)

        @app.before_request
        def _authorize_api_routes():
            if not request.path.startswith("/api/"):
                return None
            if self._is_authorized_request():
                return None
            return jsonify({"error": "Unauthorized"}), 401

        @app.after_request
        def _set_session_cookie(response: Response) -> Response:
            return self._ensure_session_cookie(response)

        @app.get("/")
        @app.get("/index.html")
        def index():
            return self._serve_html_template(WEB_STATIC / "index.html")

        @app.get("/status")
        def status_page():
            return self._serve_html_template(WEB_STATIC / "status.html")

        @app.get("/config")
        def config_page():
            return self._serve_html_template(WEB_STATIC / "config.html")

        @app.get("/api/state")
        def api_state():
            with self.state_lock:
                payload = self.controller.build_view_payload(refresh_backend=False)
            return jsonify(payload)

        @app.get("/api/config")
        def api_config():
            with self.state_lock:
                payload = self.controller.build_config_editor_payload()
            return jsonify(payload)

        @app.get("/api/status")
        def api_status():
            return jsonify(self.build_status_payload())

        @app.get("/api/status/clients")
        def api_status_clients():
            return jsonify(self.build_client_status_payload())

        @app.get("/api/status/history")
        def api_status_history():
            return jsonify(self.build_history_status_payload())

        @app.get("/api/events")
        def api_events():
            return self._serve_events()

        @app.post("/api/config")
        def api_update_config():
            payload = self._read_json_body()
            try:
                with self.state_lock:
                    response_payload = self.controller.update_config_zones(payload=payload)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            self.record_frontend_event(self._request_ip(), request.path, payload)
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.post("/api/system/power")
        def api_system_power():
            payload = self._read_json_body()
            power = self._helpers.read_bool_field(payload, "power")
            if power is None:
                return jsonify({"error": "Invalid request body: 'power' must be a boolean"}), 400
            with self.state_lock:
                response_payload = self.controller.handle_system_power_change(power=power)
            self.record_frontend_event(self._request_ip(), request.path, payload)
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.post("/api/source")
        def api_source():
            payload = self._read_json_body()
            source_value = self._helpers.read_int_field(payload, "source")
            if source_value is None:
                return jsonify({"error": "Invalid request body: 'source' must be an integer"}), 400
            with self.state_lock:
                try:
                    response_payload = self.controller.handle_source_change(source=source_value)
                except LookupError:
                    return jsonify({"error": "Source not found"}), 404
            self.record_frontend_event(self._request_ip(), request.path, payload)
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.post("/api/shortcuts/<shortcut_id>/activate")
        def api_shortcut_activate(shortcut_id: str):
            payload = {"shortcut_id": shortcut_id}
            with self.state_lock:
                try:
                    response_payload = self.controller.handle_shortcut_activation(shortcut_id=shortcut_id)
                except LookupError as exc:
                    return jsonify({"error": str(exc)}), 404
                except RuntimeError as exc:
                    return jsonify({"error": str(exc)}), 500
            self.record_frontend_event(self._request_ip(), request.path, payload)
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.post("/api/controller/<int:controller_id>/zone/<int:zone_number>/<action>")
        def api_zone_action(controller_id: int, zone_number: int, action: str):
            if action not in {"power", "source", "volume", "bass", "treble", "loudness", "balance"}:
                return jsonify({"error": "Not found"}), 404

            payload = self._read_json_body()
            self.record_frontend_event(self._request_ip(), request.path, payload)
            value = self._helpers.read_zone_setting_value(payload, action)
            if value is None:
                expected_type = "boolean" if action in {"power", "loudness"} else "integer"
                return jsonify({"error": f"Invalid request body: '{action}' must be a {expected_type}"}), 400
            with self.state_lock:
                try:
                    response_payload = self.controller.handle_zone_setting_change(
                        controller_id=controller_id,
                        zone_number=zone_number,
                        setting=action,
                        value=value,
                    )
                except LookupError:
                    return jsonify({"error": "Zone not found"}), 404
                except RuntimeError as exc:
                    return jsonify({"error": str(exc)}), 502
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.get("/static/<path:filename>")
        def static_files(filename: str) -> Response:
            return send_from_directory(WEB_STATIC, filename)

        @app.errorhandler(404)
        def not_found(_: Any) -> tuple[Response, int]:
            return jsonify({"error": "Not found"}), 404

        return app

    def run(
        self,
        host: str,
        port: int,
        debug: bool = False,
        waitress_threads: int = 16,
    ) -> None:
        self._backend_poll_interval_seconds = self._resolve_backend_poll_interval_seconds()
        self._start_backend_change_watcher()
        if debug:
            self.app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
            return

        try:
            waitress_module = importlib.import_module("waitress")
        except ModuleNotFoundError as exc:
            raise RuntimeError("waitress is required when debug mode is disabled. Install it with 'pip install waitress'.") from exc

        serve = getattr(waitress_module, "serve")
        serve(self.app, host=host, port=port, threads=max(4, waitress_threads))

    def _start_backend_change_watcher(self) -> None:
        with self._backend_watcher_lock:
            if self._backend_watcher_started:
                return
            self._start_unsolicited_update_listener()
            watcher = threading.Thread(target=self._backend_change_watcher_loop, daemon=True, name="backend-change-watcher")
            watcher.start()
            self._backend_watcher_started = True

    def _start_unsolicited_update_listener(self) -> None:
        config = self.controller.load_config()
        if not isinstance(config, dict):
            return
        backend = RussoundBackend(config=config)
        if not backend.start_update_listener(self._handle_unsolicited_zone_update):
            backend.close()
            return
        with self._event_backend_lock:
            self._event_backend = backend

    def _handle_unsolicited_zone_update(self, update: dict[str, Any]) -> None:
        controller_id = update.get("controller")
        zone_number = update.get("zone")
        setting = update.get("setting")
        if not all(isinstance(value, int) for value in (controller_id, zone_number)) or not isinstance(setting, str):
            return
        with self.state_lock:
            state = self.controller.load_state(refresh_backend=False)
            changed = False
            for zone in state.zones:
                if zone.controller != controller_id or zone.zone_number != zone_number:
                    continue
                value = update.get("value")
                if setting == "power":
                    normalized_value = bool(value)
                elif setting == "volume" and isinstance(value, int):
                    normalized_value = max(0, min(100, value))
                else:
                    return
                if getattr(zone, setting) != normalized_value:
                    setattr(zone, setting, normalized_value)
                    changed = True
                break
            if not changed:
                return
            state.sync_system_power()
            self.controller.persist_state(state)
        logging.debug(
            "Applied unsolicited Russound %s update for controller %s zone %s",
            setting,
            controller_id,
            zone_number,
        )
        self.broadcast_state_change()

    def _resolve_backend_poll_interval_seconds(self) -> float:
        config = coerce_russound_config(self.controller.load_config())
        poll_interval_seconds = resolve_backend_poll_interval_seconds(config, BACKEND_CHANGE_POLL_INTERVAL_SECONDS)
        return max(1.0, float(poll_interval_seconds))

    def _has_active_event_clients(self) -> bool:
        with self._event_clients_lock:
            return any(client.get("active", True) for client in self._event_clients.values())

    def _backend_change_watcher_loop(self) -> None:
        while True:
            iteration_started_at = time.monotonic()
            try:
                if self._has_active_event_clients() and self._sync_backend_state_if_changed():
                    logging.debug("Detected out-of-band hardware state change; broadcasting SSE update")
                    self.broadcast_state_change()
            except Exception as exc:
                logging.debug("Backend change watcher iteration failed: %s", exc)
            elapsed_seconds = time.monotonic() - iteration_started_at
            sleep_seconds = max(0.0, self._backend_poll_interval_seconds - elapsed_seconds)
            time.sleep(sleep_seconds)

    def _sync_backend_state_if_changed(self) -> bool:
        with self.state_lock:
            current_state = self.controller.load_state(refresh_backend=False)
            refreshed_state = self.controller.load_state(refresh_backend=True)
            if current_state.to_payload() == refreshed_state.to_payload():
                return False
            self.controller.persist_state(refreshed_state)
            return True

    def _read_json_body(self) -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
        return {}

    def _is_authorized_request(self) -> bool:
        header_token = request.headers.get("X-Russound-Api-Token")
        if header_token == self.api_token:
            return True
        query_token = request.args.get("token")
        return query_token == self.api_token

    def _serve_html_template(self, file_path: Path) -> Response:
        template = file_path.read_text(encoding="utf-8")
        marker = '<meta name="russound-api-token" content="" />'
        content = template.replace(marker, f'<meta name="russound-api-token" content="{html.escape(self.api_token, quote=True)}" />')
        return Response(content, mimetype="text/html")

    def _request_ip(self) -> str:
        return request.remote_addr or ""

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        if not isinstance(session_id, str):
            return None
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        return normalized_session_id

    def _get_or_create_session_id(self) -> str:
        existing = self._normalize_session_id(request.cookies.get(SESSION_COOKIE_NAME))
        if existing is not None:
            return existing
        return secrets.token_urlsafe(24)

    def _ensure_session_cookie(self, response: Response) -> Response:
        session_id = self._normalize_session_id(request.cookies.get(SESSION_COOKIE_NAME))
        if session_id is not None:
            return response
        response.set_cookie(
            SESSION_COOKIE_NAME,
            self._get_or_create_session_id(),
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
        return response

    def _serve_events(self) -> Response:
        session_id = self._get_or_create_session_id()
        client_id, event_queue = self.register_event_client(
            self._request_ip(),
            request.headers.get("User-Agent"),
            session_id,
        )
        connection_id: str | None = None
        with self._event_clients_lock:
            client_entry = self._event_clients.get(client_id)
            if client_entry is not None:
                connection_id = client_entry.get("connection_id")

        def event_stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event_text = event_queue.get(timeout=15)
                    except Empty:
                        yield ": ping\n\n"
                        continue
                    yield f"event: state-change\ndata: {event_text}\n\n"
            except GeneratorExit:
                return
            finally:
                self.unregister_event_client(client_id, expected_connection_id=connection_id)

        response = Response(stream_with_context(event_stream()), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        self._ensure_session_cookie(response)
        return response

    def register_event_client(
        self,
        ip_address: str,
        user_agent: str | None,
        session_id: str | None = None,
    ) -> tuple[int, Queue[str]]:
        event_queue: Queue[str] = Queue()
        with self._event_clients_lock:
            connection_id = secrets.token_urlsafe(12)
            normalized_session_id = self._normalize_session_id(session_id)
            if normalized_session_id is not None:
                existing_client_id = self._event_client_index.get(normalized_session_id)
                existing_entry = self._event_clients.get(existing_client_id) if existing_client_id is not None else None
                if existing_entry is not None:
                    reused_client_id = existing_entry["id"]
                    was_active = bool(existing_entry.get("active", True))
                    existing_entry["active"] = True
                    existing_entry["queue"] = event_queue
                    existing_entry["ip"] = ip_address
                    existing_entry["user_agent"] = user_agent or existing_entry.get("user_agent", "")
                    existing_entry["connected_at"] = self._timestamp()
                    existing_entry["session_id"] = normalized_session_id
                    existing_entry["connection_id"] = connection_id
                    self._event_client_index[normalized_session_id] = reused_client_id
                    if not was_active:
                        logging.debug(
                            "Re-registered client id=%s session_id=%s ip=%s user_agent=%s",
                            reused_client_id,
                            normalized_session_id,
                            ip_address,
                            user_agent or existing_entry.get("user_agent", ""),
                        )
                    return reused_client_id, event_queue

            self._event_client_id += 1
            client_id = self._event_client_id
            self._event_clients[client_id] = {
                "id": client_id,
                "ip": ip_address,
                "user_agent": user_agent or "",
                "connected_at": self._timestamp(),
                "queue": event_queue,
                "session_id": normalized_session_id,
                "connection_id": connection_id,
                "active": True,
            }
            if normalized_session_id is not None:
                self._event_client_index[normalized_session_id] = client_id
            logging.debug(
                "Registered client id=%s session_id=%s ip=%s user_agent=%s",
                client_id,
                normalized_session_id,
                ip_address,
                user_agent or "",
            )
        return client_id, event_queue

    def unregister_event_client(self, client_id: int, expected_connection_id: str | None = None) -> None:
        with self._event_clients_lock:
            client_entry = self._event_clients.get(client_id)
            if client_entry is None:
                return
            if expected_connection_id is not None and client_entry.get("connection_id") != expected_connection_id:
                return
            client_entry["active"] = False
            logging.debug(
                "Unregistered client id=%s session_id=%s ip=%s",
                client_id,
                client_entry.get("session_id"),
                client_entry.get("ip"),
            )

    def broadcast_state_change(self) -> None:
        state_payload = self._build_state_payload_for_event()
        with self._event_clients_lock:
            self.state_revision += 1
            event_text = json.dumps(
                {
                    "revision": self.state_revision,
                    "payload": state_payload,
                }
            )
            for client in list(self._event_clients.values()):
                if client.get("active", True):
                    client["queue"].put(event_text)

    def _build_state_payload_for_event(self) -> dict[str, Any]:
        with self.state_lock:
            payload = self.controller.build_view_payload(refresh_backend=False)
        return payload

    def record_frontend_event(self, ip_address: str, path: str, payload: dict[str, Any]) -> None:
        entry: dict[str, Any] = {
            "timestamp": self._timestamp(),
            "ip": ip_address,
            "path": path,
            "payload": payload,
        }
        with self._event_history_lock:
            self._event_history.appendleft(entry)

    def build_status_payload(self) -> dict[str, Any]:
        return {
            "connected_clients": self.build_client_status_payload()["connected_clients"],
            "recent_events": self.build_history_status_payload()["recent_events"],
        }

    def build_client_status_payload(self) -> dict[str, Any]:
        with self._event_clients_lock:
            clients: list[dict[str, Any]] = []
            for client in self._event_clients.values():
                if not client.get("active", True):
                    continue
                clients.append(
                    {
                        "id": client["id"],
                        "ip": client["ip"],
                        "connected_at": client["connected_at"],
                        "user_agent": client["user_agent"],
                        "session_id": client["session_id"],
                    }
                )
        return {"connected_clients": clients}

    def build_history_status_payload(self) -> dict[str, Any]:
        with self._event_history_lock:
            events = list(self._event_history)
        return {"recent_events": events}

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _configure_logging(debug: bool) -> None:
    """Configure logging so debug output is emitted to stdout when requested."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    for logger_name in ("web", "web.russound_backend", "web.russound_controller", "web.russound_state", "web.zone", "web.russound_server"):
        logging.getLogger(logger_name).setLevel(level)
    logging.getLogger("russound.russound").setLevel(logging.DEBUG if debug else logging.WARNING)


def _daemonize_process() -> None:
    """Detach this process from the controlling terminal using double-fork."""
    if os.name != "posix":
        raise RuntimeError("--daemon is only supported on POSIX systems")

    first_pid = os.fork()
    if first_pid > 0:
        print(f"Started Russound daemon (pid {first_pid})")
        os._exit(0)

    os.setsid()

    second_pid = os.fork()
    if second_pid > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o027)

    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r", encoding="utf-8") as devnull_in, open(os.devnull, "a", encoding="utf-8") as devnull_out:
        os.dup2(devnull_in.fileno(), sys.stdin.fileno())
        os.dup2(devnull_out.fileno(), sys.stdout.fileno())
        os.dup2(devnull_out.fileno(), sys.stderr.fileno())


def _env_flag(name: str, default: bool = False) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logging.getLogger(__name__).warning("Ignoring %s=%r because it is not an integer", name, value)
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Russound web controller")
    parser.add_argument("--host", default=env_str("RUSSOUND_WEB_HOST") or "0.0.0.0")
    parser.add_argument("--port", type=int, default=env_port("RUSSOUND_WEB_PORT") or 8000)
    parser.add_argument("--config", default=env_str("RUSSOUND_CONFIG"))
    parser.add_argument("--state", default=env_str("RUSSOUND_STATE") or str(WEB_ROOT / "russound_state.json"))
    parser.add_argument("--daemon", action="store_true", help="Run the server in the background")
    parser.add_argument("--debug", action="store_true", default=_env_flag("RUSSOUND_DEBUG"), help="Enable debug logging to the terminal")
    parser.add_argument("--waitress-threads", type=int, default=_env_int("RUSSOUND_WAITRESS_THREADS", 16), help="Worker thread count for Waitress when not in debug mode (min 4, default 16)")
    args = parser.parse_args()

    if args.daemon:
        _daemonize_process()
    _configure_logging(args.debug)

    server = RussoundHTTPServer(args.config, args.state)
    if not args.daemon:
        print(f"Listening on http://{args.host}:{args.port}")
        print("Press Ctrl+C to stop the server.")
    try:
        server.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            waitress_threads=args.waitress_threads,
        )
    except KeyboardInterrupt:
        print("\nShutting down Russound server...")


if __name__ == "__main__":
    main()