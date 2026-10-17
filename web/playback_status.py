from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPException
import ipaddress
import json
import logging
import math
import re
import ssl
import threading
from typing import Any, Callable, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from mpdrussoundpi.russound.lib.tunein import TuneInCatalog
from web.config_types import env_str

POLL_INTERVAL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 2.0
AGENT_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
WRITE_ACTIONS = {"play", "pause", "stop", "set_gain", "set_tunein_station", "select_favorite"}
FAVORITE_ACTIONS = {"add_favorite", "remove_favorite", "move_favorite"}
AGENT_WRITE_ACTIONS = WRITE_ACTIONS | FAVORITE_ACTIONS
FAVORITE_KEY_PATTERN = re.compile(r"(tunein:s[0-9]{1,12}|[a-z0-9][a-z0-9_-]{0,63})\Z")
MAX_FAVORITES = 20


class PlaybackCommandError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def validate_operation_request(payload: Any, *, favorites: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlaybackCommandError("invalid_request", "A playback operation object is required.", 400)
    value = cast(dict[str, Any], payload)
    fields = {"action", "expected_config_revision", "expected_control_revision", "idempotency_key"}
    action = value.get("action")
    if not isinstance(action, str) or action not in (FAVORITE_ACTIONS if favorites else WRITE_ACTIONS):
        raise PlaybackCommandError("invalid_request", "Unsupported playback action.", 400)
    if action in ("select_favorite", "remove_favorite", "move_favorite"):
        fields.add("key")
        key = value.get("key")
        if not isinstance(key, str) or FAVORITE_KEY_PATTERN.match(key) is None:
            raise PlaybackCommandError("invalid_request", "A valid favorite is required.", 400)
    if action == "move_favorite":
        fields.add("index")
        index = value.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_FAVORITES:
            raise PlaybackCommandError("invalid_request", "A valid favorite position is required.", 400)
    if action == "set_gain":
        fields.add("gain_db")
        gain = value.get("gain_db")
        try:
            valid_gain = not isinstance(gain, bool) and isinstance(gain, (int, float)) and math.isfinite(gain) and gain <= 0
        except OverflowError:
            valid_gain = False
        if not valid_gain:
            raise PlaybackCommandError("invalid_request", "Gain must be finite and at or below 0 dB.", 400)
    if action in ("set_tunein_station", "add_favorite"):
        fields.update(("station_id", "station_name"))
        try:
            TuneInCatalog.validate_station_id(value.get("station_id"))
        except ValueError:
            raise PlaybackCommandError("invalid_request", "A valid TuneIn station selection is required.", 400) from None
        station_name = value.get("station_name")
        if (
            not isinstance(station_name, str)
            or not station_name
            or len(station_name) > 120
            or not station_name.isprintable()
        ):
            raise PlaybackCommandError("invalid_request", "A valid TuneIn station selection is required.", 400)
    if set(value) != fields:
        raise PlaybackCommandError("invalid_request", "Operation fields are missing or unsupported.", 400)
    for name in ("expected_config_revision", "expected_control_revision"):
        if not isinstance(value[name], str) or re.fullmatch(r"[a-f0-9]{64}", value[name]) is None:
            raise PlaybackCommandError("invalid_request", "Current playback revisions are required.", 400)
    if not isinstance(value["idempotency_key"], str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value["idempotency_key"]) is None:
        raise PlaybackCommandError("invalid_request", "Invalid operation idempotency key.", 400)
    return value


class PlaybackAgentConfig:
    def __init__(self, base_url: str, agent_id: str, token_file: str, tls_ca_file: str | None = None) -> None:
        parts = urlsplit(base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Playback agent URL must be HTTP(S) without credentials, query, or fragment")
        if parts.scheme == "http":
            try:
                is_loopback = parts.hostname.lower() == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                raise ValueError("Playback agent URL must use HTTPS unless it targets loopback")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("Playback agent URL contains an invalid port") from exc
        if AGENT_ID_PATTERN.fullmatch(agent_id) is None:
            raise ValueError("Playback agent identity is invalid")
        if tls_ca_file is not None and (not tls_ca_file or parts.scheme != "https"):
            raise ValueError("Playback TLS CA file requires an HTTPS agent URL")
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.token_file = token_file
        self.tls_ca_file = tls_ca_file

    @classmethod
    def from_environment(cls) -> tuple[PlaybackAgentConfig, ...] | None:
        raw_value = env_str("RUSSOUND_PLAYBACK_AGENTS")
        if raw_value is None:
            return None
        try:
            parsed_entries = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError("RUSSOUND_PLAYBACK_AGENTS must be a JSON array") from exc
        if not isinstance(parsed_entries, list) or not parsed_entries:
            raise ValueError("RUSSOUND_PLAYBACK_AGENTS must be a non-empty JSON array")
        entries: list[Any] = cast(list[Any], parsed_entries)
        configurations: list[PlaybackAgentConfig] = []
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise ValueError("Each playback agent requires exactly agent_id, url, and token_file")
            entry: dict[str, Any] = cast(dict[str, Any], raw_entry)
            if not {"agent_id", "url", "token_file"} <= set(entry) or set(entry) - {
                "agent_id", "url", "token_file", "tls_ca_file",
            }:
                raise ValueError("Each playback agent requires agent_id, url, and token_file; tls_ca_file is optional")
            agent_id = entry.get("agent_id")
            base_url = entry.get("url")
            token_file = entry.get("token_file")
            tls_ca_file = entry.get("tls_ca_file")
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError("Playback agent identity, URL, and token-file reference must be non-empty strings")
            if not isinstance(base_url, str) or not base_url:
                raise ValueError("Playback agent identity, URL, and token-file reference must be non-empty strings")
            if not isinstance(token_file, str) or not token_file:
                raise ValueError("Playback agent identity, URL, and token-file reference must be non-empty strings")
            if tls_ca_file is not None and (not isinstance(tls_ca_file, str) or not tls_ca_file):
                raise ValueError("Playback TLS CA file must be a non-empty path")
            configurations.append(cls(base_url, agent_id, token_file, tls_ca_file))
        agent_ids = [configuration.agent_id for configuration in configurations]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("Playback agent identities must be unique")
        return tuple(configurations)


class PlaybackStatusClient:
    """One read-only polling/cache worker for the configured playback agent."""

    def __init__(self, config: PlaybackAgentConfig, on_change: Callable[[], None] | None = None) -> None:
        with open(config.token_file, encoding="utf-8") as token_file:
            token = token_file.read(4097).strip()
        if len(token) > 4096:
            raise ValueError("Playback token file exceeds the supported size")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Playback token file must contain one non-empty token")
        self._config = config
        self._ssl_context = (
            ssl.create_default_context(cafile=config.tls_ca_file)
            if config.tls_ca_file is not None
            else ssl.create_default_context()
        )
        self._on_change_callback = on_change
        self._token = token
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._snapshot: dict[str, Any] | None = None
        self._status = "unavailable"
        self._error: dict[str, Any] | None = {
            "code": "snapshot_pending",
            "message": "Waiting for the playback agent.",
            "retryable": True,
        }
        self._revision = 0
        self._write_operations: list[str] = []

    @property
    def agent_id(self) -> str:
        return self._config.agent_id

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "api_version": 1,
                "configured": True,
                "revision": self._revision,
                "agents": [{
                    "agent_id": self._config.agent_id,
                    "status": self._status,
                    "last_attempt_at": self._last_attempt_at,
                    "last_success_at": self._last_success_at,
                    "error": self._error,
                    "snapshot": self._snapshot,
                    "write_operations": list(self._write_operations),
                }],
            }

    def poll_once(self) -> bool:
        attempted = _timestamp()
        with self._lock:
            self._last_attempt_at = attempted
        try:
            health = self._get_json("/v1/health")
            _validate_identity(health, self._config.agent_id)
            if health.get("status") != "alive":
                raise ValueError("agent_not_alive")
            capabilities = self._get_json("/v1/capabilities")
            _validate_identity(capabilities, self._config.agent_id)
            writes = _write_capabilities(capabilities.get("write_operations"))
            inventory = self._get_json("/v1/outputs")
            _validate_identity(inventory, self._config.agent_id)
            if not isinstance(inventory.get("outputs"), list):
                raise ValueError("invalid_agent_response")
            for output in inventory["outputs"]:
                _validate_output(output)
            snapshot = {
                key: inventory[key]
                for key in ("api_version", "agent_id", "epoch", "revision", "config_revision", "snapshot_at", "snapshot_age_ms", "stale", "outputs")
                if key in inventory
            }
            snapshot["favorites"] = self._favorites() if "select_favorite" in writes else None
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
            code = _error_code(exc)
            with self._lock:
                changed = self._status != "unavailable" or self._error is None or self._error["code"] != code
                self._status = "unavailable"
                self._error = {
                    "code": code,
                    "message": _error_message(code),
                    "retryable": True,
                }
                if changed:
                    self._revision += 1
            return False
        with self._lock:
            changed = (
                self._status != "ok"
                or self._write_operations != writes
                or _meaningful_snapshot(self._snapshot) != _meaningful_snapshot(snapshot)
                or self._error is not None
            )
            self._status = "ok"
            self._error = None
            self._snapshot = snapshot
            self._write_operations = list(writes)
            self._last_success_at = _timestamp()
            if changed:
                self._revision += 1
        return True

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Playback status client is already running")
        self._thread = threading.Thread(target=self._run, daemon=True, name="playback-status-poller")
        self._thread.start()

    def set_on_change_callback(self, callback: Callable[[], None]) -> None:
        self._on_change_callback = callback

    def _run(self) -> None:
        while not self._stop.is_set():
            before = self.snapshot()["revision"]
            self.poll_once()
            after = self.snapshot()["revision"]
            if after != before:
                self._on_change()
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _on_change(self) -> None:
        if self._on_change_callback is not None:
            self._on_change_callback()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS * 4)
            if self._thread.is_alive():
                logging.error("Playback status poller did not stop within its timeout")

    def _favorites(self) -> dict[str, Any] | None:
        """Favorites are optional: before the agent's first observation they are unavailable."""
        try:
            response = self._get_json("/v1/favorites")
        except HTTPError as exc:
            exc.close()
            if exc.code == 503:
                return None
            raise
        _validate_identity(response, self._config.agent_id)
        return _validate_favorites(response)

    def _get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self._config.base_url}{path}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        with build_opener(
            NoRedirectHandler(),
            HTTPSHandler(context=self._ssl_context),
        ).open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ValueError("invalid_agent_response")
            payload: Any = json.loads(response.read(1_000_001))
        if not isinstance(payload, dict):
            raise ValueError("invalid_agent_response")
        return cast(dict[str, Any], payload)

    def submit_operation(self, output_id: str, payload: Any) -> dict[str, Any]:
        if AGENT_ID_PATTERN.fullmatch(output_id) is None:
            raise PlaybackCommandError("invalid_request", "Invalid output identity.", 400)
        value = validate_operation_request(payload)
        with self._lock:
            if self._status != "ok" or value["action"] not in self._write_operations:
                raise PlaybackCommandError("writes_unavailable", "Playback commands are unavailable or not enabled on this agent.", 503)
        response = self._operation_request(f"/v1/outputs/{output_id}/operations", value)
        if response["operation"].get("output_id") != output_id or response["operation"].get("action") != value["action"]:
            raise PlaybackCommandError("invalid_agent_response", "Agent returned a mismatched operation; its outcome is unknown.", 502)
        return response

    def submit_favorites_operation(self, payload: Any) -> dict[str, Any]:
        value = validate_operation_request(payload, favorites=True)
        with self._lock:
            if self._status != "ok" or value["action"] not in self._write_operations:
                raise PlaybackCommandError("writes_unavailable", "Favorites editing is unavailable or not enabled on this agent.", 503)
        response = self._operation_request("/v1/favorites/operations", value)
        if response["operation"].get("output_id") is not None or response["operation"].get("action") != value["action"]:
            raise PlaybackCommandError("invalid_agent_response", "Agent returned a mismatched operation; its outcome is unknown.", 502)
        return response

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        if re.fullmatch(r"[a-f0-9]{32}", operation_id) is None:
            raise PlaybackCommandError("invalid_request", "Invalid operation identity.", 400)
        response = self._operation_request(f"/v1/operations/{operation_id}")
        if response["operation"]["id"] != operation_id:
            raise PlaybackCommandError("invalid_agent_response", "Agent returned a mismatched operation.", 502)
        return response

    def _operation_request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            f"{self._config.base_url}{path}",
            data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json", "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with build_opener(NoRedirectHandler(), HTTPSHandler(context=self._ssl_context)).open(
                request, timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status != (202 if payload is not None else 200):
                    raise ValueError("invalid_agent_response")
                result = json.loads(response.read(65537))
            if not isinstance(result, dict):
                raise ValueError("invalid_agent_response")
            result = cast(dict[str, Any], result)
            _validate_identity(result, self.agent_id)
            raw_operation = result.get("operation")
            if not isinstance(raw_operation, dict):
                raise ValueError("invalid_agent_response")
            operation = cast(dict[str, Any], raw_operation)
            if (
                not isinstance(operation.get("id"), str)
                or re.fullmatch(r"[a-f0-9]{32}", operation["id"]) is None
                or operation.get("status") not in {"queued", "running", "applied", "failed", "expired", "superseded", "outcome_unknown"}
            ):
                raise ValueError("invalid_agent_response")
            return result
        except HTTPError as exc:
            upstream_status = exc.code
            exc.close()
            messages = {
                400: "Playback operation was rejected as invalid.",
                401: "Playback agent authentication failed.",
                403: "Playback agent denied the operation.",
                404: "Playback output or operation was not found; a missing operation does not prove it never executed.",
                409: "Playback revisions changed, the backend is busy, or the idempotency key conflicts. Refresh before issuing a new command.",
                429: "Playback operation capacity is exhausted; retry the same operation later.",
            }
            status = upstream_status if upstream_status in messages else 503
            raise PlaybackCommandError("agent_operation_rejected", messages.get(status, "Playback agent could not accept the operation."), status) from None
        except (URLError, TimeoutError, OSError, HTTPException, ValueError, TypeError, AttributeError):
            raise PlaybackCommandError(
                "operation_outcome_unknown",
                "Playback agent response was unavailable or invalid. The command may have been accepted; retry only with the same fields and idempotency key.",
                503,
            ) from None


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        _ = (req, fp, code, msg, headers, newurl)
        return None


def _write_capabilities(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("invalid_agent_response")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or item not in AGENT_WRITE_ACTIONS:
            raise ValueError("invalid_agent_response")
        result.append(item)
    return result


def _validate_favorites(response: dict[str, Any]) -> dict[str, Any]:
    stations = response.get("favorites")
    revisions = [response.get("config_revision"), response.get("control_revision")]
    if not isinstance(stations, list) or len(cast(list[Any], stations)) > MAX_FAVORITES or not all(
        isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item) for item in revisions
    ):
        raise ValueError("invalid_agent_response")
    result: list[dict[str, Any]] = []
    for item in cast(list[Any], stations):
        if not isinstance(item, dict):
            raise ValueError("invalid_agent_response")
        station = cast(dict[str, Any], item)
        key, name, kind, station_id = (station.get(field) for field in ("key", "name", "type", "station_id"))
        if (
            set(station) != {"key", "name", "type", "station_id"}
            or not isinstance(key, str) or FAVORITE_KEY_PATTERN.match(key) is None
            or not isinstance(name, str) or not name or len(name) > 120
            or kind not in ("url", "tunein")
            or (kind == "tunein") != isinstance(station_id, str)
            or (station_id is not None and (not isinstance(station_id, str) or re.fullmatch(r"s[0-9]{1,12}", station_id) is None))
        ):
            raise ValueError("invalid_agent_response")
        result.append({"key": key, "name": name, "type": kind, "station_id": station_id})
    return {"config_revision": revisions[0], "control_revision": revisions[1], "stations": result}


def _validate_identity(response: dict[str, Any], agent_id: str) -> None:
    if response.get("api_version") != 1 or response.get("agent_id") != agent_id:
        raise ValueError("agent_identity_mismatch")


def _validate_output(output: Any) -> None:
    if not isinstance(output, dict):
        raise ValueError("invalid_agent_response")
    output_payload: dict[str, Any] = cast(dict[str, Any], output)
    if (
        not isinstance(output_payload.get("id"), str)
        or AGENT_ID_PATTERN.fullmatch(output_payload["id"]) is None
        or not isinstance(output_payload.get("label"), str)
        or not isinstance(output_payload.get("playback"), dict)
        or not isinstance(output_payload.get("gain"), dict)
    ):
        raise ValueError("invalid_agent_response")
    selection: object = output_payload.get("selection")
    if selection is not None:
        if not isinstance(selection, dict):
            raise ValueError("invalid_agent_response")
        selection_payload = cast(dict[str, Any], selection)
        source: object = selection_payload.get("source")
        if source is not None and not isinstance(source, str):
            raise ValueError("invalid_agent_response")
        for key in ("provider", "station_id", "station_name"):
            value = selection_payload.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError("invalid_agent_response")
    playback: object = output_payload["playback"]
    if isinstance(playback, dict):
        playback_payload = cast(dict[str, Any], playback)
        last_known: object = playback_payload.get("last_known")
        last_known_value: object = None
        if isinstance(last_known, dict):
            last_known_value = cast(dict[str, Any], last_known).get("value")
        for probe in (playback_payload.get("value"), last_known_value):
            if isinstance(probe, dict):
                source = cast(dict[str, Any], probe).get("current_source")
                if source is not None and not isinstance(source, str):
                    raise ValueError("invalid_agent_response")


def _error_code(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        if exc.code == 401:
            return "agent_unauthorized"
        return "agent_forbidden" if exc.code == 403 else "agent_http_error"
    if isinstance(exc, TimeoutError):
        return "agent_timeout"
    if isinstance(exc, URLError):
        return "agent_timeout" if isinstance(exc.reason, TimeoutError) else "agent_unreachable"
    return str(exc) if str(exc) in {"agent_identity_mismatch", "agent_not_alive", "invalid_agent_response"} else "agent_unreachable"


def _error_message(code: str) -> str:
    messages = {
        "agent_unauthorized": "Playback agent authentication failed.",
        "agent_forbidden": "Playback agent denied access (HTTP 403).",
        "agent_identity_mismatch": "Playback agent identity did not match configuration.",
        "agent_not_alive": "Playback agent is not alive.",
        "invalid_agent_response": "Playback agent returned an invalid response.",
        "agent_timeout": "Playback agent timed out.",
        "agent_http_error": "Playback agent returned an HTTP error.",
    }
    return messages.get(code, "Playback agent is unreachable.")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _meaningful_snapshot(snapshot: Any) -> Any:
    volatile_fields = {
        "age_ms",
        "attempted_at",
        "elapsed_seconds",
        "observed_at",
        "revision",
        "snapshot_age_ms",
        "snapshot_at",
    }
    if isinstance(snapshot, dict):
        return {
            key: _meaningful_snapshot(value)
            for key, value in cast(dict[str, Any], snapshot).items()
            if key not in volatile_fields
        }
    if isinstance(snapshot, list):
        return [_meaningful_snapshot(value) for value in cast(list[Any], snapshot)]
    return snapshot
