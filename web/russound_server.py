import argparse
from collections import deque
from datetime import datetime, timezone
import html
import json
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
    apply_shortcut,
    build_view_payload,
    load_config,
    load_state,
    persist_state,
    set_shared_source,
    update_system_power,
    update_zone_setting,
)

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
        if parsed.path == "/api/state":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            with server.state_lock:
                payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            self._send_json(payload)
            return
        if parsed.path == "/api/status":
            if not self._is_authorized(server, parsed):
                self._send_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            self._send_json(server.build_status_payload())
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
        if parsed.path == "/api/system/power":
            payload = self._read_json_body()
            power = self._read_bool_field(payload, "power")
            if power is None:
                self._send_json({"error": "Invalid request body: 'power' must be a boolean"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                update_system_power(state, power)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
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
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                if not self._state_has_input(state, source_value):
                    self._send_json({"error": "Source not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                set_shared_source(state, source_value)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/shortcuts/") and parsed.path.endswith("/activate"):
            shortcut_id = parsed.path.split("/")[3]
            payload = {"shortcut_id": shortcut_id}
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                config = load_config(server.config_path)
                shortcut_applied = False
                shortcut = None
                if config is not None:
                    shortcut = next((item for item in config.get("shortcuts", []) if item.get("id") == shortcut_id), None)
                if shortcut is None:
                    self._send_json({"error": "Shortcut not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                if not self._state_has_zones(state, shortcut.get("zone_ids", [])):
                    self._send_json({"error": "Shortcut configuration error: shortcut references unknown zone"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                shortcut_source = shortcut.get("source")
                if shortcut_source is not None and not self._state_has_input(state, shortcut_source):
                    self._send_json({"error": "Shortcut configuration error: shortcut references unknown source"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                apply_shortcut(state, shortcut)
                persist_state(server.state_path, state)
                shortcut_applied = True
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            if shortcut_applied:
                server.record_frontend_event(self.client_address[0], parsed.path, payload)
                server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/zones/") and parsed.path.endswith("/power"):
            zone_id = parsed.path.split("/")[3]
            payload = self._read_json_body()
            power = self._read_bool_field(payload, "power")
            if power is None:
                self._send_json({"error": "Invalid request body: 'power' must be a boolean"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                if not self._state_has_zone(state, zone_id):
                    self._send_json({"error": "Zone not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                update_zone_setting(state, zone_id, "power", power)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/zones/") and parsed.path.endswith("/source"):
            zone_id = parsed.path.split("/")[3]
            payload = self._read_json_body()
            source_value = self._read_int_field(payload, "source")
            if source_value is None:
                self._send_json({"error": "Invalid request body: 'source' must be an integer"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                if not self._state_has_zone(state, zone_id):
                    self._send_json({"error": "Zone not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                if not self._state_has_input(state, source_value):
                    self._send_json({"error": "Source not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                update_zone_setting(state, zone_id, "source", source_value)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/zones/") and parsed.path.endswith("/volume"):
            zone_id = parsed.path.split("/")[3]
            payload = self._read_json_body()
            volume = self._read_int_field(payload, "volume")
            if volume is None:
                self._send_json({"error": "Invalid request body: 'volume' must be an integer"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                if not self._state_has_zone(state, zone_id):
                    self._send_json({"error": "Zone not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                update_zone_setting(state, zone_id, "volume", volume)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
            server.broadcast_state_change()
            self._send_json(response_payload)
            return

        if parsed.path.startswith("/api/zones/") and parsed.path.endswith("/mute"):
            zone_id = parsed.path.split("/")[3]
            payload = self._read_json_body()
            mute = self._read_bool_field(payload, "mute")
            if mute is None:
                self._send_json({"error": "Invalid request body: 'mute' must be a boolean"}, status=HTTPStatus.BAD_REQUEST)
                return
            with server.state_lock:
                state = load_state(server.config_path, server.state_path, refresh_backend=False)
                if not self._state_has_zone(state, zone_id):
                    self._send_json({"error": "Zone not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                update_zone_setting(state, zone_id, "mute", mute)
                persist_state(server.state_path, state)
                response_payload = build_view_payload(server.config_path, server.state_path, refresh_backend=False)
            server.record_frontend_event(self.client_address[0], parsed.path, payload)
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

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state_has_zone(self, state: dict[str, Any], zone_id: str) -> bool:
        return any(zone.get("id") == zone_id for zone in state.get("zones", []))

    def _state_has_zones(self, state: dict[str, Any], zone_ids: list[Any]) -> bool:
        known_zone_ids = {zone.get("id") for zone in state.get("zones", [])}
        return all(zone_id in known_zone_ids for zone_id in zone_ids)

    def _state_has_input(self, state: dict[str, Any], source_id: Any) -> bool:
        return any(input_item.get("id") == source_id for input_item in state.get("inputs", []))

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
        entry = {
            "timestamp": self._timestamp(),
            "ip": ip_address,
            "path": path,
            "payload": payload,
        }
        with self._event_history_lock:
            self._event_history.appendleft(entry)

    def build_status_payload(self) -> dict[str, Any]:
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
        with self._event_history_lock:
            events = list(self._event_history)
        return {
            "connected_clients": clients,
            "recent_events": events,
        }

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Russound web controller")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config")
    parser.add_argument("--state", default=str(WEB_ROOT / "russound_state.json"))
    args = parser.parse_args()

    server = RussoundHTTPServer((args.host, args.port), RussoundRequestHandler, args.config, args.state)
    print(f"Listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
