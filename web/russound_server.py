from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import html
import json
import logging
import os
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, urlparse
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.russound_controller import (
    get_controller,
)
from web.russound_state import RussoundState

WEB_ROOT = Path(__file__).resolve().parent
WEB_STATIC = WEB_ROOT / "static"


class RussoundRequestHandler(BaseHTTPRequestHandler):
    server_version = "RussoundWeb/1.0"

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Clients may close SSE or HTTP connections abruptly; treat as normal disconnects.
            return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        server = cast(RussoundHTTPServer, self.server)
        if parsed.path in {"/", "/index.html"}:
            self._serve_html_template(WEB_STATIC / "index.html", server.api_token)
            return
        if parsed.path == "/status":
            self._serve_html_template(WEB_STATIC / "status.html", server.api_token)
            return
        if parsed.path == "/config":
            self._serve_html_template(WEB_STATIC / "config.html", server.api_token)
            return
        if parsed.path == "/api/state":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            with server.state_lock:
                payload = server.controller.build_view_payload(refresh_backend=False)
            self._send_json(payload)
            return
        if parsed.path == "/api/config":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            with server.state_lock:
                payload = server.controller.build_config_editor_payload()
            self._send_json(payload)
            return
        if parsed.path == "/api/status":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self._send_json(server.build_status_payload())
            return
        if parsed.path == "/api/status/clients":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self._send_json(server.build_client_status_payload())
            return
        if parsed.path == "/api/status/history":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self._send_json(server.build_history_status_payload())
            return
        if parsed.path == "/api/events":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self._serve_events(server)
            return
        if parsed.path.startswith("/static/"):
            static_file = WEB_STATIC / parsed.path.removeprefix("/static/")
            if static_file.exists():
                self._serve_file(static_file)
                return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        server = cast(RussoundHTTPServer, self.server)
        if parsed.path.startswith("/api/") and not self._is_authorized(server, parsed):
            self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return
        if parsed.path == "/api/config":
            payload = self._read_json_body()
            try:
                with server.state_lock:
                    response_payload = server.controller.update_config_zones(payload=payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return
        if parsed.path == "/api/system/power":
            payload = self._read_json_body()
            power = self._read_bool_field(payload, "power")
            if power is None:
                self._send_json({"error": "Invalid request body: 'power' must be a boolean"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                response_payload = server.controller.handle_system_power_change(power=power)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path == "/api/source":
            payload = self._read_json_body()
            source_value = self._read_int_field(payload, "source")
            if source_value is None:
                self._send_json({"error": "Invalid request body: 'source' must be an integer"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                try:
                    response_payload = server.controller.handle_source_change(source=source_value)
                except LookupError:
                    self._send_json({"error": "Source not found"}, status=HTTPStatus.NOT_FOUND)
                    return
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/shortcuts/") and parsed.path.endswith("/activate"):
            shortcut_id = parsed.path.split("/")[3]
            payload = {"shortcut_id": shortcut_id}
            with server.state_lock:
                try:
                    response_payload = server.controller.handle_shortcut_activation(shortcut_id=shortcut_id)
                except LookupError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                    return
                except RuntimeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        route_match = self._match_controller_zone_route(parsed.path)
        if route_match is not None:
            controller_id, zone_number, action = route_match
            payload = self._read_json_body()
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            value = self._read_zone_setting_value(payload, action)
            if value is None:
                expected_type = "boolean" if action in {"power", "loudness"} else "integer"
                self._send_json(
                    {"error": f"Invalid request body: '{action}' must be a {expected_type}"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            with server.state_lock:
                try:
                    response_payload = server.controller.handle_zone_setting_change(
                        controller_id=controller_id,
                        zone_number=zone_number,
                        setting=action,
                        value=value,
                    )
                except LookupError:
                    self._send_json({"error": "Zone not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                except RuntimeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                    return
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    def _read_bool_field(self, payload: dict[str, Any], field_name: str) -> bool | None:
        value = payload.get(field_name)
        if isinstance(value, bool):
            return value
        return None

    def _read_int_field(self, payload: dict[str, Any], field_name: str) -> int | None:
        value = payload.get(field_name)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None

    def _read_zone_setting_value(self, payload: dict[str, Any], action: str) -> Any | None:
        if action in {"power", "loudness"}:
            return self._read_bool_field(payload, action)
        if action in {"source", "volume", "bass", "treble", "balance"}:
            return self._read_int_field(payload, action)
        return None

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _serve_html_template(self, file_path: Path, api_token: str) -> None:
        template = file_path.read_text(encoding="utf-8")
        marker = '<meta name="russound-api-token" content="" />'
        content = template.replace(marker, f'<meta name="russound-api-token" content="{html.escape(api_token, quote=True)}" />')
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self, server: "RussoundHTTPServer") -> None:
        client_id, event_queue = server.register_event_client(
            self.client_address[0],
            self.headers.get("User-Agent"),
        )
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            while True:
                try:
                    event_text = event_queue.get(timeout=15)
                except Empty:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        break
                    continue
                try:
                    self.wfile.write(f"event: state-change\ndata: {event_text}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    break
        finally:
            server.unregister_event_client(client_id)

    def _serve_file(self, file_path: Path, content_type: str | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or self._mime_type(file_path))
        data = file_path.read_bytes()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _mime_type(self, file_path: Path) -> str:
        if file_path.suffix == ".css":
            return "text/css; charset=utf-8"
        if file_path.suffix == ".js":
            return "application/javascript; charset=utf-8"
        return "application/octet-stream"


class RussoundHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        config_path: str | Path | None,
        state_path: str | Path | None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.config_path = config_path
        self.state_path = state_path
        self.api_token = secrets.token_urlsafe(32)
        self.state_revision = 0
        self.state_lock = threading.Lock()
        self._event_clients: dict[int, dict[str, Any]] = {}
        self.controller = get_controller(config_path, state_path)
        self._event_clients_lock = threading.Lock()
        self._event_client_id = 0
        self._event_history: deque[dict[str, Any]] = deque(maxlen=50)
        self._event_history_lock = threading.Lock()

    def register_event_client(self, ip_address: str, user_agent: str | None) -> tuple[int, Queue[str]]:
        event_queue: Queue[str] = Queue()
        with self._event_clients_lock:
            self._event_client_id += 1
            client_id = self._event_client_id
            self._event_clients[client_id] = {
                "id": client_id,
                "ip": ip_address,
                "user_agent": user_agent or "",
                "connected_at": self._timestamp(),
                "queue": event_queue,
            }
        return client_id, event_queue

    def unregister_event_client(self, client_id: int) -> None:
        with self._event_clients_lock:
            self._event_clients.pop(client_id, None)

    def broadcast_state_change(self) -> None:
        with self._event_clients_lock:
            self.state_revision += 1
            event_text = json.dumps({"revision": self.state_revision})
            for client in list(self._event_clients.values()):
                client["queue"].put(event_text)

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
            clients = [
                {
                    "id": client["id"],
                    "ip": client["ip"],
                    "connected_at": client["connected_at"],
                    "user_agent": client["user_agent"],
                }
                for client in self._event_clients.values()
            ]
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
    logging.getLogger("russound.russound").setLevel(logging.DEBUG if debug else logging.CRITICAL)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Russound web controller")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config")
    parser.add_argument("--state", default=str(WEB_ROOT / "russound_state.json"))
    parser.add_argument("--daemon", action="store_true", help="Run the server in the background")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to the terminal")
    args = parser.parse_args()

    if args.daemon:
        _daemonize_process()
    _configure_logging(args.debug)

    server = RussoundHTTPServer((args.host, args.port), RussoundRequestHandler, args.config, args.state)
    if not args.daemon:
        print(f"Listening on http://{args.host}:{args.port}")
        print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Russound server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
