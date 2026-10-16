from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPException
import ipaddress
import json
import logging
import re
import ssl
import threading
from typing import Any, Callable, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from web.config_types import env_str

POLL_INTERVAL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 2.0
AGENT_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


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
            if capabilities.get("write_operations") != []:
                raise ValueError("invalid_agent_response")
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
            changed = self._status != "ok" or self._snapshot != snapshot or self._error is not None
            self._status = "ok"
            self._error = None
            self._snapshot = snapshot
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


def _error_code(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return "agent_unauthorized" if exc.code == 401 else "agent_http_error"
    if isinstance(exc, TimeoutError):
        return "agent_timeout"
    if isinstance(exc, URLError):
        return "agent_timeout" if isinstance(exc.reason, TimeoutError) else "agent_unreachable"
    return str(exc) if str(exc) in {"agent_identity_mismatch", "agent_not_alive", "invalid_agent_response"} else "agent_unreachable"


def _error_message(code: str) -> str:
    messages = {
        "agent_unauthorized": "Playback agent authentication failed.",
        "agent_identity_mismatch": "Playback agent identity did not match configuration.",
        "agent_not_alive": "Playback agent is not alive.",
        "invalid_agent_response": "Playback agent returned an invalid response.",
        "agent_timeout": "Playback agent timed out.",
        "agent_http_error": "Playback agent returned an HTTP error.",
    }
    return messages.get(code, "Playback agent is unreachable.")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
