from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import socket
import sys
import threading
import time
from typing import Any, cast

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

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
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Streams are closed periodically so a dead client cannot pin a worker thread indefinitely.
EVENT_STREAM_MAX_SECONDS = 300.0
EVENT_STREAM_PING_SECONDS = 15.0
EVENT_STREAM_RETRY_MS = 2000
# Worker threads always kept free for page loads and API calls.
RESERVED_REQUEST_THREADS = 4
DEFAULT_WAITRESS_THREADS = 24
DEFAULT_TCP_USER_TIMEOUT_SECONDS = 30
BACKEND_CHANGE_POLL_INTERVAL_SECONDS = 2.0
# Gives the controller time to apply a keypad action and coalesces bursts of key presses.
ZONE_REFRESH_DEBOUNCE_SECONDS = 0.5
# Upper bound for how long a dropped gateway connection stays unnoticed by the watcher.
UPDATE_LISTENER_CHECK_SECONDS = 5.0
MAX_RECONNECT_DELAY_SECONDS = 60.0


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
        if action in {"source", "volume", "turn_on_volume", "bass", "treble", "balance"}:
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
        if action not in {"power", "source", "volume", "bass", "treble", "loudness", "balance", "turn_on_volume"}:
            return None
        return controller_id, zone_number, action


def _hostname(host: str) -> str:
    """Strip the port from a Host header value (IPv6 literals keep their address)."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _split_env_list(name: str) -> list[str]:
    return [item.strip() for item in (env_str(name) or "").split(",") if item.strip()]


def _connection_socket_options(tcp_user_timeout_seconds: int) -> list[tuple[int, int, int]]:
    """Socket options waitress applies to each accepted client connection."""
    options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    tcp_user_timeout = getattr(socket, "TCP_USER_TIMEOUT", None)
    if tcp_user_timeout is not None and tcp_user_timeout_seconds > 0:
        # Drops connections whose sent data stays unacknowledged, e.g. sleeping phones holding an SSE stream.
        options.append((socket.IPPROTO_TCP, tcp_user_timeout, tcp_user_timeout_seconds * 1000))
    return options


class RussoundHTTPServer:
    def __init__(
        self,
        config_path: str | Path | None,
        state_path: str | Path | None,
        allowed_hosts: list[str] | None = None,
        allowed_origins: list[str] | None = None,
        trusted_proxy_count: int = 0,
        max_event_streams: int | None = None,
    ) -> None:
        self.config_path = config_path
        self.state_path = state_path
        # Empty means any Host is accepted; set it to block DNS-rebinding attacks.
        self._allowed_hosts = {_hostname(host) for host in allowed_hosts or []}
        self._allowed_origins = {origin.rstrip("/").lower() for origin in allowed_origins or []}
        self.state_revision = 0
        self.state_lock = threading.Lock()
        self._state_payload_lock = threading.Lock()
        self._event_clients: dict[int, dict[str, Any]] = {}
        # None means unlimited until run() derives a limit from the worker thread count.
        self._max_event_streams = max_event_streams
        self._event_stream_max_seconds = EVENT_STREAM_MAX_SECONDS
        self.controller = get_controller(config_path, state_path)
        self._state_payload = self.controller.build_view_payload(refresh_backend=False)
        self._event_clients_lock = threading.Lock()
        self._event_client_id = 0
        self._event_history: deque[dict[str, Any]] = deque(maxlen=50)
        self._event_history_lock = threading.Lock()
        self._backend_watcher_lock = threading.Lock()
        self._backend_watcher_started = False
        self._event_backend: RussoundBackend | None = None
        self._event_backend_lock = threading.Lock()
        self._backend_reconfiguring = threading.Event()
        self._backend_generation = 0
        self._backend_poll_interval_seconds = BACKEND_CHANGE_POLL_INTERVAL_SECONDS
        self._zone_refresh_requested = threading.Event()
        self._full_poll_requested = threading.Event()
        self._next_reconnect_at = 0.0
        self._reconnect_delay_seconds = UPDATE_LISTENER_CHECK_SECONDS
        self._zone_refresh_lock = threading.Lock()
        self._zone_refresh_addresses: set[tuple[int, int]] = set()
        self._helpers = RussoundRequestHandler()
        self.app = self._build_flask_app()
        if trusted_proxy_count > 0:
            self.app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
                self.app.wsgi_app,
                x_for=trusted_proxy_count,
                x_proto=trusted_proxy_count,
                x_host=trusted_proxy_count,
                x_port=trusted_proxy_count,
            )

    def _build_flask_app(self) -> Flask:
        app = Flask(__name__)

        @app.before_request
        def _guard_requests():
            if self._allowed_hosts and _hostname(request.host) not in self._allowed_hosts:
                logging.warning("Rejected request with untrusted Host header %r from %s", request.host, request.remote_addr)
                return jsonify({"error": "Host not allowed"}), 400
            if not request.path.startswith("/api/"):
                return None
            logging.getLogger(__name__).debug(
                "API request method=%s path=%s host=%s session_id=%s",
                request.method,
                request.path,
                request.remote_addr or "unknown",
                self._get_or_create_session_id(),
            )
            if request.method in SAFE_HTTP_METHODS:
                return None
            if not self._is_same_origin_request():
                logging.warning(
                    "Rejected cross-site API request path=%s origin=%s sec-fetch-site=%s host=%s",
                    request.path,
                    request.headers.get("Origin"),
                    request.headers.get("Sec-Fetch-Site"),
                    request.remote_addr,
                )
                return jsonify({"error": "Cross-site request rejected"}), 403
            if not request.is_json:
                return jsonify({"error": "Content-Type must be application/json"}), 415
            return None

        @app.after_request
        def _set_session_cookie(response: Response) -> Response:
            return self._ensure_session_cookie(response)

        @app.get("/")
        @app.get("/index.html")
        def index():
            return send_from_directory(WEB_STATIC, "index.html")

        @app.get("/status")
        def status_page():
            return send_from_directory(WEB_STATIC, "status.html")

        @app.get("/config")
        def config_page():
            return send_from_directory(WEB_STATIC, "config.html")

        @app.get("/api/state")
        def api_state():
            return jsonify(self._read_state_payload())

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
            old_backend = None
            try:
                with self.state_lock:
                    response_payload = self.controller.update_config_zones(payload=payload)
                    self._backend_reconfiguring.set()
                    with self._event_backend_lock:
                        old_backend = self._event_backend
                        self._event_backend = None
                        self._backend_generation += 1
                    self.controller.set_shared_backend(None)
                    self._backend_poll_interval_seconds = self._resolve_backend_poll_interval_seconds()
                    self._next_reconnect_at = 0.0
                    self._reconnect_delay_seconds = UPDATE_LISTENER_CHECK_SECONDS
                    self._publish_state_payload_locked()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            except OSError as exc:
                return jsonify({"error": f"Cannot save configuration or state. Check writable config/data volumes and permissions: {exc}"}), 500
            try:
                if old_backend is not None:
                    old_backend.close()
            finally:
                self._backend_reconfiguring.clear()
            self._full_poll_requested.set()
            self._zone_refresh_requested.set()
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
                self._publish_state_payload(response_payload)
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
                self._publish_state_payload(response_payload)
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
                self._publish_state_payload(response_payload)
            self.record_frontend_event(self._request_ip(), request.path, payload)
            self.broadcast_state_change()
            return jsonify(response_payload)

        @app.post("/api/controller/<int:controller_id>/zone/<int:zone_number>/<action>")
        def api_zone_action(controller_id: int, zone_number: int, action: str):
            if action not in {"power", "source", "volume", "bass", "treble", "loudness", "balance", "turn_on_volume"}:
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
                self._publish_state_payload(response_payload)
            logging.debug(
                "API zone update applied controller=%d zone=%d action=%s value=%s",
                controller_id,
                zone_number,
                action,
                value,
            )
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
        waitress_threads: int = DEFAULT_WAITRESS_THREADS,
        tcp_user_timeout_seconds: int = DEFAULT_TCP_USER_TIMEOUT_SECONDS,
    ) -> None:
        self._backend_poll_interval_seconds = self._resolve_backend_poll_interval_seconds()
        thread_count = max(RESERVED_REQUEST_THREADS + 1, waitress_threads)
        stream_limit = thread_count - RESERVED_REQUEST_THREADS
        self._max_event_streams = stream_limit if self._max_event_streams is None else min(self._max_event_streams, stream_limit)
        self._start_backend_change_watcher()
        if debug:
            self.app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
            return

        try:
            waitress_module = importlib.import_module("waitress")
        except ModuleNotFoundError as exc:
            raise RuntimeError("waitress is required when debug mode is disabled. Install it with 'pip install waitress'.") from exc

        create_server = getattr(waitress_module, "create_server")
        server = create_server(self.app, host=host, port=port, threads=thread_count)
        # Waitress has no serve() argument for this; it applies adj.socket_options to every accepted connection.
        server.adj.socket_options = _connection_socket_options(tcp_user_timeout_seconds)
        server.run()

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
        generation = self._backend_generation
        if not backend.start_update_listener(lambda update: self._handle_unsolicited_zone_update(update, generation)):
            logging.warning("Russound gateway not reachable yet; the update listener will start once it connects")
        with self._event_backend_lock:
            self._event_backend = backend
        self.controller.set_shared_backend(backend)

    def _ensure_update_listener(self) -> None:
        with self.state_lock:
            if self._backend_reconfiguring.is_set():
                return
            self._ensure_update_listener_locked()

    def _ensure_update_listener_locked(self) -> None:
        """Create the shared backend if missing and reconnect it (re-attaching the listener) when dropped."""
        with self._event_backend_lock:
            backend = self._event_backend
        if backend is None:
            self._start_unsolicited_update_listener()
            return
        client = backend.client
        if client is not None and client.is_connected():
            self._reconnect_delay_seconds = UPDATE_LISTENER_CHECK_SECONDS
            return
        now = time.monotonic()
        if now < self._next_reconnect_at:
            return
        try:
            reconnected = backend.connect()
        except Exception as exc:
            logging.debug("Russound gateway reconnect attempt failed: %s", exc)
            reconnected = False
        if reconnected:
            logging.info("Russound gateway connection re-established")
            self._reconnect_delay_seconds = UPDATE_LISTENER_CHECK_SECONDS
            return
        self._next_reconnect_at = now + self._reconnect_delay_seconds
        self._reconnect_delay_seconds = min(self._reconnect_delay_seconds * 2, MAX_RECONNECT_DELAY_SECONDS)

    def _handle_unsolicited_zone_update(self, update: dict[str, Any], generation: int | None = None) -> None:
        if generation is not None and generation != self._backend_generation:
            return
        controller_id = update.get("controller")
        zone_number = update.get("zone")
        setting = update.get("setting")
        if not all(isinstance(value, int) for value in (controller_id, zone_number)) or not isinstance(setting, str):
            return
        if setting == "refresh":
            self._request_zone_refresh(cast(int, controller_id), cast(int, zone_number))
            return
        with self.state_lock:
            if generation is not None and generation != self._backend_generation:
                return
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
                elif setting == "source" and isinstance(value, int) and state.has_input(value):
                    normalized_value = value
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
            self._publish_state_payload_locked()
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
            return bool(self._event_clients)

    def _request_zone_refresh(self, controller_id: int, zone_number: int) -> None:
        with self._zone_refresh_lock:
            self._zone_refresh_addresses.add((controller_id, zone_number))
        self._zone_refresh_requested.set()

    def _take_zone_refresh_addresses(self) -> set[tuple[int, int]]:
        self._zone_refresh_requested.clear()
        with self._zone_refresh_lock:
            addresses = set(self._zone_refresh_addresses)
            self._zone_refresh_addresses.clear()
        return addresses

    def _backend_change_watcher_loop(self) -> None:
        next_full_poll_at = time.monotonic()
        while True:
            self._ensure_update_listener()
            wait_seconds = min(max(0.0, next_full_poll_at - time.monotonic()), UPDATE_LISTENER_CHECK_SECONDS)
            woken = self._zone_refresh_requested.wait(wait_seconds)
            if not woken and time.monotonic() < next_full_poll_at:
                continue
            if woken and not self._full_poll_requested.is_set():
                time.sleep(ZONE_REFRESH_DEBOUNCE_SECONDS)
                try:
                    if self._refresh_zones_if_changed(self._take_zone_refresh_addresses()):
                        logging.debug("Keypad-triggered zone refresh detected a change; broadcasting SSE update")
                        self.broadcast_state_change()
                except Exception as exc:
                    logging.debug("Keypad-triggered zone refresh failed: %s", exc)
                continue
            self._full_poll_requested.clear()
            iteration_started_at = time.monotonic()
            try:
                # Without a recorded poll result the hardware health would be a guess, so poll even without UI clients.
                should_poll = self._has_active_event_clients() or self.controller.backend_health_snapshot() is None
                if should_poll and self._sync_backend_state_if_changed():
                    logging.debug("Detected out-of-band hardware state change; broadcasting SSE update")
                    self.broadcast_state_change()
            except Exception as exc:
                logging.debug("Backend change watcher iteration failed: %s", exc)
            next_full_poll_at = iteration_started_at + self._backend_poll_interval_seconds

    def _refresh_zones_if_changed(self, zone_addresses: set[tuple[int, int]]) -> bool:
        if not zone_addresses:
            return False
        with self.state_lock:
            current_state = self.controller.load_state(refresh_backend=False)
            with self._event_backend_lock:
                shared_backend = self._event_backend
            refreshed_state = self.controller.refresh_zones(zone_addresses, backend=shared_backend)
            if current_state.to_payload() == refreshed_state.to_payload():
                self._publish_state_payload_locked()
                return False
            self.controller.persist_state(refreshed_state)
            self._publish_state_payload_locked()
            return True

    def _sync_backend_state_if_changed(self) -> bool:
        with self.state_lock:
            current_state = self.controller.load_state(refresh_backend=False)
            health_before = self.controller.backend_health_snapshot()
            with self._event_backend_lock:
                shared_backend = self._event_backend
            if shared_backend is None:
                refreshed_state = self.controller.load_state(refresh_backend=True)
            else:
                logging.debug("Polling Russound state through the shared backend connector")
                refreshed_state = self.controller.load_state(refresh_backend=True, backend=shared_backend)
            # Silent hardware keeps its last known zone values, so health has to be compared as well.
            health_changed = self.controller.backend_health_snapshot() != health_before
            if current_state.to_payload() == refreshed_state.to_payload():
                self._publish_state_payload_locked()
                return health_changed
            self.controller.persist_state(refreshed_state)
            self._publish_state_payload_locked()
            return True

    def _read_json_body(self) -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            return cast(dict[str, Any], payload)
        return {}

    def _is_same_origin_request(self) -> bool:
        origin = request.headers.get("Origin")
        if origin is not None:
            normalized_origin = origin.rstrip("/").lower()
            if normalized_origin == request.host_url.rstrip("/").lower() or normalized_origin in self._allowed_origins:
                return True
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site in {"same-origin", "none"}:
            return True
        # Without browser provenance headers the caller is not a browser, so it cannot be a CSRF vector.
        return origin is None and fetch_site is None

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
        registration = self.register_event_client(
            self._request_ip(),
            request.headers.get("User-Agent"),
            session_id,
        )
        if registration is None:
            logging.warning(
                "SSE connection rejected: stream limit %s reached host=%s session_id=%s",
                self._max_event_streams,
                self._request_ip(),
                session_id,
            )
            response = jsonify({"error": "Too many live update connections"})
            response.status_code = 503
            response.headers["Retry-After"] = "30"
            return response
        client_id, event_queue = registration
        request_ip = self._request_ip()
        stream_deadline = time.monotonic() + self._event_stream_max_seconds

        def event_stream():
            try:
                yield f"retry: {EVENT_STREAM_RETRY_MS}\n: connected\n\n"
                while True:
                    remaining_seconds = stream_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        # The browser reconnects after the retry delay and resyncs on open.
                        return
                    try:
                        event_text = event_queue.get(timeout=min(EVENT_STREAM_PING_SECONDS, remaining_seconds))
                    except Empty:
                        yield ": ping\n\n"
                        continue
                    yield f"event: state-change\ndata: {event_text}\n\n"
            except GeneratorExit:
                return
            finally:
                self.unregister_event_client(client_id)
                logging.info("SSE connection closed client_id=%s host=%s session_id=%s", client_id, request_ip, session_id)

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
    ) -> tuple[int, Queue[str]] | None:
        """Register one SSE stream; returns None when the stream limit is reached."""
        event_queue: Queue[str] = Queue()
        with self._event_clients_lock:
            if self._max_event_streams is not None and len(self._event_clients) >= self._max_event_streams:
                return None
            self._event_client_id += 1
            client_id = self._event_client_id
            self._event_clients[client_id] = {
                "id": client_id,
                "ip": ip_address,
                "user_agent": user_agent or "",
                "connected_at": self._timestamp(),
                "queue": event_queue,
                "session_id": self._normalize_session_id(session_id),
            }
            stream_count = len(self._event_clients)
        # Health may be stale while nobody was watching; re-poll so the new client sees the real hardware state.
        self._full_poll_requested.set()
        self._zone_refresh_requested.set()
        logging.info(
            "SSE client connected client_id=%s session_id=%s host=%s streams=%d",
            client_id,
            session_id,
            ip_address,
            stream_count,
        )
        return client_id, event_queue

    def unregister_event_client(self, client_id: int) -> None:
        with self._event_clients_lock:
            self._event_clients.pop(client_id, None)

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
            for client in self._event_clients.values():
                client["queue"].put(event_text)
            recipients = len(self._event_clients)
        logging.info(
            "SSE state update broadcast revision=%d recipients=%d",
            self.state_revision,
            recipients,
        )

    def _build_state_payload_for_event(self) -> dict[str, Any]:
        return self._read_state_payload()

    def _publish_state_payload(self, payload: dict[str, Any]) -> None:
        with self._state_payload_lock:
            self._state_payload = payload

    def _publish_state_payload_locked(self) -> None:
        self._publish_state_payload(self.controller.build_view_payload(refresh_backend=False))

    def _read_state_payload(self) -> dict[str, Any]:
        if self.state_lock.acquire(blocking=False):
            try:
                self._publish_state_payload_locked()
            finally:
                self.state_lock.release()
        with self._state_payload_lock:
            return self._state_payload

    def record_frontend_event(self, ip_address: str, path: str, payload: dict[str, Any]) -> None:
        entry: dict[str, Any] = {
            "timestamp": self._timestamp(),
            "ip": ip_address,
            "path": path,
            "payload": payload,
        }
        with self._event_history_lock:
            self._event_history.appendleft(entry)
        logging.info(
            "UI interaction path=%s host=%s",
            path,
            ip_address,
        )

    def build_status_payload(self) -> dict[str, Any]:
        return {
            "connected_clients": self.build_client_status_payload()["connected_clients"],
            "recent_events": self.build_history_status_payload()["recent_events"],
        }

    def build_client_status_payload(self) -> dict[str, Any]:
        """List connected clients, one entry per browser session (tabs of one browser share a session)."""
        sessions: dict[str, dict[str, Any]] = {}
        with self._event_clients_lock:
            for client in sorted(self._event_clients.values(), key=lambda entry: entry["id"]):
                session_key = client["session_id"] or f"stream-{client['id']}"
                stream_count = sessions[session_key]["streams"] + 1 if session_key in sessions else 1
                sessions[session_key] = {
                    "id": client["id"],
                    "ip": client["ip"],
                    "connected_at": client["connected_at"],
                    "user_agent": client["user_agent"],
                    "session_id": client["session_id"],
                    "streams": stream_count,
                }
        return {"connected_clients": list(sessions.values())}

    def build_history_status_payload(self) -> dict[str, Any]:
        with self._event_history_lock:
            events = list(self._event_history)
        return {"recent_events": events}

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _configure_logging(debug: bool) -> None:
    """Configure logging so debug output is emitted to stdout when requested."""
    level = logging.DEBUG if debug else logging.INFO
    log_file = env_str("RUSSOUND_LOG_FILE")
    file_handler: logging.Handler | None = None
    if log_file:
        from logging.handlers import RotatingFileHandler

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    if file_handler is not None:
        logging.getLogger().addHandler(file_handler)
    for logger_name in ("web", "web.russound_backend", "web.russound_connector", "web.russound_controller", "web.russound_state", "web.zone", "web.russound_server"):
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
    parser.add_argument(
        "--waitress-threads",
        type=int,
        default=_env_int("RUSSOUND_WAITRESS_THREADS", DEFAULT_WAITRESS_THREADS),
        help=f"Worker thread count for Waitress (min {RESERVED_REQUEST_THREADS + 1}, default {DEFAULT_WAITRESS_THREADS})",
    )
    args = parser.parse_args()

    if args.daemon:
        _daemonize_process()
    _configure_logging(args.debug)

    server = RussoundHTTPServer(
        args.config,
        args.state,
        allowed_hosts=_split_env_list("RUSSOUND_ALLOWED_HOSTS"),
        allowed_origins=_split_env_list("RUSSOUND_ALLOWED_ORIGINS"),
        trusted_proxy_count=_env_int("RUSSOUND_TRUSTED_PROXY_COUNT", 0),
        max_event_streams=_env_int("RUSSOUND_MAX_EVENT_STREAMS", 0) or None,
    )
    if not args.daemon:
        print(f"Listening on http://{args.host}:{args.port}")
        print("Press Ctrl+C to stop the server.")
    try:
        server.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            waitress_threads=args.waitress_threads,
            tcp_user_timeout_seconds=_env_int("RUSSOUND_TCP_USER_TIMEOUT_SECONDS", DEFAULT_TCP_USER_TIMEOUT_SECONDS),
        )
    except KeyboardInterrupt:
        print("\nShutting down Russound server...")


if __name__ == "__main__":
    main()