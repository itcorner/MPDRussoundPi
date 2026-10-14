from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import logging
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from lib.mpdbackend import BackendError, MPDBackend
from lib.playbackstatus import OutputObservation, PlaybackCollector


@dataclass(frozen=True)
class CachedInventory:
    revision: int = 0
    config_revision: str | None = None
    published_at: str | None = None
    monotonic: float = 0
    outputs: tuple[OutputObservation, ...] | None = None
    error_code: str = "snapshot_unavailable"


class AgentObserver:
    """Background reads only; HTTP readers take one immutable publication."""

    def __init__(self, config_path: str | Path, *, backend: MPDBackend | None = None) -> None:
        self.config_path = config_path
        self.backend = backend
        self._lock = threading.Lock()
        self._cache = CachedInventory()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._collector: PlaybackCollector | None = None
        self._config_due = 0.0
        self._config_delay = 2.0
        self._audio_due = 0.0
        self._due: dict[str, float] = {}
        self._delays: dict[str, float] = {}
        self._failed = False

    def snapshot(self) -> CachedInventory:
        with self._lock:
            return self._cache

    def _publish(
        self, outputs: tuple[OutputObservation, ...] | None,
        config_revision: str | None = None, error_code: str = "snapshot_unavailable",
    ) -> None:
        with self._lock:
            previous = self._cache
            if (outputs, config_revision, error_code) == (
                previous.outputs, previous.config_revision, previous.error_code,
            ):
                return
            self._cache = CachedInventory(
                previous.revision + 1, config_revision,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                time.monotonic(), outputs, error_code,
            )
        if previous.error_code != error_code or previous.config_revision != config_revision:
            logging.info("Agent inventory transition: %s", error_code)
        if outputs is not None:
            old = {output.id: output for output in previous.outputs or ()}
            for output in outputs:
                prior = old.get(output.id)
                if prior is None or prior.playback.status != output.playback.status:
                    logging.info("Output %s playback observation: %s", output.id, output.playback.status)

    def tick(self) -> None:
        """One scheduler iteration; never called by a request handler."""
        now = time.monotonic()
        if now >= self._config_due:
            replacement = None
            try:
                if self.backend is None:
                    self.backend = MPDBackend(self.config_path)
                _, _, revision = self.backend.configuration_snapshot()
                if self._collector is None or revision != self._collector.config_revision:
                    replacement = PlaybackCollector(self.backend)
            except (BackendError, OSError, ValueError):
                self._publish(None, error_code="configuration_invalid")
                old, self._collector = self._collector, None
                if old is not None:
                    old.close()
                self._config_due = time.monotonic() + self._config_delay
                self._config_delay = min(30.0, self._config_delay * 2)
            else:
                if replacement is not None:
                    old = self._collector
                    self._collector = replacement
                    self._due.clear()
                    self._delays.clear()
                    self._audio_due = 0
                    self._publish(replacement.snapshot(), replacement.config_revision, "ok")
                    if old is not None:
                        old.close()
                self._config_delay = 2
                self._config_due = time.monotonic() + 2
        collector = self._collector
        if collector is None:
            return
        outputs = collector.snapshot()
        previous = {output.id: output for output in self.snapshot().outputs or ()}
        for output in outputs:
            prior = previous.get(output.id)
            if prior is None or prior.playback != output.playback:
                delay = self._delays.get(output.id, 2.0)
                if output.playback.status == "error":
                    self._due[output.id] = now + delay
                    self._delays[output.id] = min(delay * 2, 30)
                else:
                    self._due[output.id] = now + 2
                    self._delays[output.id] = 2
        self._publish(outputs, collector.config_revision, "ok")
        audio = now >= self._audio_due
        eligible = tuple(output.id for output in outputs if now >= self._due.get(output.id, 0))
        if eligible:
            collector.refresh(output_ids=eligible)
        if audio:
            # Audio work uses its own pool even when an MPD is backing off.
            collector.refresh(audio=True, playback=False)
            self._audio_due = now + 30

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("Observer cannot be restarted")
        self._thread = threading.Thread(target=self._run, name="agent-observer")
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(0.1)
        except Exception:
            logging.error("Agent observer stopped unexpectedly", exc_info=False)
            self._failed = True
            self._publish(None, error_code="snapshot_unavailable")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._collector is not None:
            self._collector.close()
            self._collector = None
        if self._failed:
            raise RuntimeError("Agent observer failed unexpectedly")


def create_app(observer: AgentObserver, agent_id: str, token: str) -> Flask:
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent_id) is None:
        raise ValueError("Invalid agent identity")
    if len(token) < 32 or not token.isascii() or any(character.isspace() for character in token):
        raise ValueError("Service token must be at least 32 ASCII characters without whitespace")
    app = Flask(__name__)
    epoch = uuid4().hex
    identity: dict[str, object] = {"api_version": 1, "agent_id": agent_id, "epoch": epoch}
    messages = {
        "unauthorized": "Service authentication required.",
        "invalid_request": "Request is malformed.",
        "route_not_found": "Route does not exist.",
        "output_not_found": "Output is not configured.",
        "method_not_allowed": "Method is not supported.",
        "configuration_invalid": "Playback configuration is invalid or unavailable.",
        "snapshot_unavailable": "Playback snapshot is unavailable.",
        "internal_error": "Internal agent error.",
    }

    def error(code: str, status: int) -> tuple[Response, int]:
        response = jsonify(api_version=1, error={
            "code": code, "message": messages[code], "retryable": status >= 500,
        })
        if status == 405:
            response.headers["Allow"] = "GET, HEAD"
        return response, status

    @app.before_request
    def authenticate() -> tuple[Response, int] | None:
        supplied = request.headers.get("Authorization", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, "Bearer " + token):
            response, status = error("unauthorized", 401)
            response.headers["WWW-Authenticate"] = "Bearer"
            return response, status
        if request.query_string or request.content_length or request.headers.get("Transfer-Encoding"):
            return error("invalid_request", 400)
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exception: HTTPException) -> tuple[Response, int]:
        code = exception.code or 500
        if code >= 500:
            logging.error("Agent HTTP protocol error")
            return error("internal_error", code)
        return error({404: "route_not_found", 405: "method_not_allowed"}.get(code, "invalid_request"), code)

    @app.errorhandler(Exception)
    def unexpected_error(exception: Exception) -> tuple[Response, int]:
        logging.error("Agent HTTP request failed unexpectedly: %s", type(exception).__name__)
        return error("internal_error", 500)

    @app.get("/v1/health", provide_automatic_options=False)
    def health() -> Response:
        return jsonify(**identity, status="alive")

    @app.get("/v1/capabilities", provide_automatic_options=False)
    def capabilities() -> Response:
        return jsonify(**identity, read_features=[
            "inventory", "playback", "now_playing", "audio_diagnostics",
        ], write_operations=[])

    def inventory(output_id: str | None = None) -> Response | tuple[Response, int]:
        if output_id is not None and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", output_id) is None:
            return error("invalid_request", 400)
        cached = observer.snapshot()
        if cached.outputs is None:
            return error(cached.error_code, 503)
        now = time.monotonic()
        outputs = [output.to_dict(now) for output in cached.outputs]
        age = max(0, int((now - cached.monotonic) * 1000))
        envelope: dict[str, object] = {
            **identity, "revision": cached.revision, "config_revision": cached.config_revision,
            "snapshot_at": cached.published_at, "snapshot_age_ms": age, "stale": age >= 10000,
        }
        if output_id is None:
            return jsonify(**envelope, outputs=outputs)
        for output in outputs:
            if output["id"] == output_id:
                return jsonify(**envelope, output=output)
        return error("output_not_found", 404)

    app.add_url_rule("/v1/outputs", view_func=inventory, methods=["GET"], provide_automatic_options=False)
    app.add_url_rule("/v1/outputs/<output_id>", view_func=inventory, methods=["GET"],
                     provide_automatic_options=False)
    return app
