"""Durable, single-worker manual operations; playback state remains backend-owned."""

import fcntl
from collections.abc import Generator
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, cast
from uuid import uuid4

from lib.mpdbackend import (
    FAVORITE_ACTIONS, OUTPUT_ACTIONS, AudioOperationError, BackendBusy, BackendError,
    FavoriteNotFound, FavoritesFullError, GainPersistenceError, MPDBackend, MPDOperationError,
    NoStationError, RevisionConflict, StationUnavailableError,
)
from lib.playbackidentity import output_id
from lib.stations import MAX_STATIONS, Station


ACTIONS = OUTPUT_ACTIONS + FAVORITE_ACTIONS
TUNEIN_ACTIONS = ("set_tunein_station", *FAVORITE_ACTIONS)
FAVORITES_LANE = "@favorites"
FAVORITE_KEY_PATTERN = re.compile(r"(tunein:s[0-9]{1,12}|[a-z0-9][a-z0-9_-]{0,63})")
TERMINAL = ("applied", "failed", "expired", "superseded", "outcome_unknown")
RETENTION_SECONDS = 86400


class OperationError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def payload(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.status in (429, 503)}


def validate_request(value: object, *, favorites: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationError("invalid_request", "An operation object is required.", 400)
    payload = cast(dict[str, Any], value)
    allowed = {"action", "expected_config_revision", "expected_control_revision", "idempotency_key"}
    action = payload.get("action")
    if not isinstance(action, str) or action not in (FAVORITE_ACTIONS if favorites else OUTPUT_ACTIONS):
        raise OperationError("invalid_request", "Unsupported playback action.", 400)
    if action in ("select_favorite", "remove_favorite", "move_favorite"):
        allowed.add("key")
        key = payload.get("key")
        if not isinstance(key, str) or FAVORITE_KEY_PATTERN.fullmatch(key) is None:
            raise OperationError("invalid_request", "A valid favorite key is required.", 400)
    if action == "move_favorite":
        allowed.add("index")
        index = payload.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_STATIONS:
            raise OperationError("invalid_request", "A valid favorite index is required.", 400)
    if action == "set_gain":
        allowed.add("gain_db")
        gain = payload.get("gain_db")
        try:
            valid_gain = not isinstance(gain, bool) and isinstance(gain, (int, float)) and math.isfinite(gain) and gain <= 0
        except OverflowError:
            valid_gain = False
        if not valid_gain:
            raise OperationError("invalid_request", "Gain must be a finite number at or below 0 dB.", 400)
    if action in ("set_tunein_station", "add_favorite"):
        allowed.update(("station_id", "station_name"))
        try:
            Station.tunein(payload.get("station_id"), payload.get("station_name"))
        except ValueError:
            raise OperationError("invalid_request", "A valid TuneIn station selection is required.", 400) from None
    if set(payload) != allowed:
        raise OperationError("invalid_request", "Operation fields are missing or unsupported.", 400)
    for field in ("expected_config_revision", "expected_control_revision"):
        if not isinstance(payload[field], str) or re.fullmatch(r"[a-f0-9]{64}", payload[field]) is None:
            raise OperationError("invalid_request", "Current configuration and control revisions are required.", 400)
    key = payload["idempotency_key"]
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) is None:
        raise OperationError("invalid_request", "A 16-128 character idempotency key is required.", 400)
    return dict(payload)


class OperationManager:
    def __init__(
        self, backend: MPDBackend, path: str | Path, *,
        capacity: int = 32, ledger_limit: int = 1000, deadline_seconds: float = 30,
        tunein_selection_enabled: bool = True,
    ) -> None:
        self.backend = backend
        self.tunein_selection_enabled = tunein_selection_enabled
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self._ownership = self.path.with_suffix(".lock").open("a+b")
        os.chmod(self._ownership.name, 0o600)
        try:
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._ownership.close()
            raise RuntimeError("Another operation worker owns this ledger") from None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.capacity, self.ledger_limit, self.deadline_seconds = capacity, ledger_limit, deadline_seconds
        self._failed = False
        try:
            self._initialize()
        except BaseException:
            self._ownership.close()
            raise

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, output_id TEXT NOT NULL, key TEXT NOT NULL,
                request TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL, result TEXT NOT NULL, UNIQUE(output_id, key))""")
            rows = db.execute("SELECT id, result FROM operations WHERE status='running'").fetchall()
            for identity, raw in rows:
                result = json.loads(raw)
                result.update(status="outcome_unknown", applied=None, error={
                    "code": "interrupted", "message": "Agent restarted during execution. Read current status before issuing another command.",
                    "retryable": False,
                })
                try:
                    result["recovery_observation"] = (
                        None if result["output_id"] is None
                        else self.backend.operation_observation(result["output_id"])
                    )
                except (BackendError, OSError, ValueError, KeyError):
                    result["recovery_observation"] = None
                    logging.warning("Interrupted operation %s cannot yet be re-observed", identity)
                if not result["requested_intention_persisted"]:
                    result["requested_intention_persisted"] = None
                if not result["gain_persisted"]:
                    result["gain_persisted"] = None
                if not result.get("requested_source_persisted", False):
                    result["requested_source_persisted"] = None
                db.execute("UPDATE operations SET status=?, result=? WHERE id=?",
                           ("outcome_unknown", json.dumps(result), identity))
                logging.warning("Operation %s interrupted; outcome unknown", identity)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, identity: str, payload: object) -> dict[str, Any]:
        try:
            output_id(identity)
        except ValueError:
            raise OperationError("invalid_request", "Invalid output identity.", 400) from None
        return self._submit(identity, validate_request(payload))

    def submit_favorites(self, payload: object) -> dict[str, Any]:
        """Favorites edits share one global lane so a newer edit supersedes queued ones."""
        return self._submit(FAVORITES_LANE, validate_request(payload, favorites=True))

    def _submit(self, identity: str, value: dict[str, Any]) -> dict[str, Any]:
        favorites = identity == FAVORITES_LANE
        if value["action"] in TUNEIN_ACTIONS and not self.tunein_selection_enabled:
            raise OperationError("station_selection_disabled", "TuneIn station selection is not enabled on this agent.", 403)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            previous = db.execute("SELECT request,result FROM operations WHERE output_id=? AND key=?",
                                  (identity, value["idempotency_key"])).fetchone()
            if previous:
                if previous[0] != canonical:
                    raise OperationError("idempotency_conflict", "This key was already used with different operation fields.", 409)
                return json.loads(previous[1])
            if self._failed or self._stop.is_set():
                raise OperationError("worker_unavailable", "Playback operation worker is unavailable.", 503)
            now = time.time()
            db.execute("DELETE FROM operations WHERE created<? AND status NOT IN ('queued','running')",
                       (now - RETENTION_SECONDS,))
            count = db.execute("SELECT count(*) FROM operations").fetchone()[0]
            pending = db.execute("SELECT count(*) FROM operations WHERE status IN ('queued','running')").fetchone()[0]
            if count >= self.ledger_limit or pending >= self.capacity:
                raise OperationError("capacity_exceeded", "Playback operation capacity is exhausted; retry later.", 429)
            try:
                if favorites:
                    self.backend.validate_favorites_operation(
                        value["expected_config_revision"], value["expected_control_revision"],
                    )
                else:
                    self.backend.validate_operation(
                        identity, value["expected_config_revision"], value["expected_control_revision"],
                    )
            except BackendBusy:
                raise OperationError("backend_busy", "Playback backend is busy; refresh and retry.", 409) from None
            except RevisionConflict:
                raise OperationError("revision_conflict", "Playback configuration or intention changed; refresh and retry.", 409) from None
            except KeyError:
                raise OperationError("output_not_found", "Output is not configured.", 404) from None
            except (BackendError, OSError, ValueError):
                logging.warning("Cannot validate manual playback operation for %s", identity)
                raise OperationError("backend_unavailable", "Playback configuration or intentions are unavailable.", 503) from None
            # A newer accepted target must never let an older queued target run afterwards.
            for old_id, raw in db.execute("SELECT id,result FROM operations WHERE output_id=? AND status='queued'", (identity,)):
                old = json.loads(raw)
                old.update(status="superseded", error={
                    "code": "superseded", "message": "A newer manual command superseded this queued operation.", "retryable": False,
                })
                db.execute("UPDATE operations SET status='superseded', result=? WHERE id=?", (json.dumps(old), old_id))
            operation: dict[str, Any] = {
                "id": uuid4().hex, "output_id": None if favorites else identity, "action": value["action"],
                "status": "queued", "phase": "accepted", "created_at": now,
                "deadline_at": now + self.deadline_seconds, "requested_intention_persisted": False,
                "gain_persisted": False, "requested_source_persisted": False,
                "applied": False, "error": None,
            }
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                       (operation["id"], identity, value["idempotency_key"], canonical,
                        now, operation["deadline_at"], "queued", json.dumps(operation)))
        self._wake.set()
        return operation

    def get(self, identity: str) -> dict[str, Any]:
        if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
            raise OperationError("invalid_request", "Invalid operation identity.", 400)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT result FROM operations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise OperationError("operation_not_found", "Operation is unknown or its retention period expired; do not assume it was not executed.", 404)
        return json.loads(row[0])

    def _update(self, operation: dict[str, Any], **changes: Any) -> None:
        operation.update(changes)
        with self._lock, self._connect() as db:
            db.execute("UPDATE operations SET status=?,result=? WHERE id=?",
                       (operation["status"], json.dumps(operation), operation["id"]))

    def run_once(self) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT request,result FROM operations WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return False
            payload, operation = json.loads(row[0]), json.loads(row[1])
            operation.update(status="running", phase="executing")
            db.execute("UPDATE operations SET status='running',result=? WHERE id=?",
                       (json.dumps(operation), operation["id"]))
        if time.time() >= operation["deadline_at"]:
            self._update(operation, status="expired", error={
                "code": "expired", "message": "Operation expired before execution; no mutation was attempted.", "retryable": False,
            })
            return True

        def progress(phase: str) -> None:
            changes: dict[str, Any] = {"phase": phase}
            if phase == "intention_persisted":
                changes["requested_intention_persisted"] = True
            elif phase == "gain_saved":
                changes["gain_persisted"] = True
            elif phase in ("gain_live", "gain_deferred"):
                changes["gain_application"] = "live" if phase == "gain_live" else "on_next_play"
            elif phase == "source_persisted":
                changes["requested_source_persisted"] = True
            self._update(operation, **changes)

        try:
            while True:
                try:
                    if operation["output_id"] is None:
                        progress("favorites_saving")
                        self.backend.favorites_operation(
                            payload["action"], key=payload.get("key"), index=payload.get("index"),
                            station_id=payload.get("station_id"),
                            station_name=payload.get("station_name"),
                            expected_config_revision=payload["expected_config_revision"],
                            expected_control_revision=payload["expected_control_revision"],
                        )
                    else:
                        self.backend.manual_operation(
                            operation["output_id"], payload["action"], payload.get("gain_db"),
                            station_id=payload.get("station_id"),
                            station_name=payload.get("station_name"),
                            favorite_key=payload.get("key"),
                            expected_config_revision=payload["expected_config_revision"],
                            expected_control_revision=payload["expected_control_revision"], progress=progress,
                        )
                    break
                except BackendBusy:
                    if self._stop.is_set() or time.time() >= operation["deadline_at"]:
                        raise
                    progress("waiting_for_backend")
                    self._stop.wait(min(0.1, max(0, operation["deadline_at"] - time.time())))
                    if self._stop.is_set() or time.time() >= operation["deadline_at"]:
                        raise
        except (BackendError, OSError, ValueError, KeyError) as exc:
            code, text = "backend_failed", "Playback command failed. Read current playback status; a saved intention may still be reconciled by the supervisor."
            if isinstance(exc, BackendBusy):
                code, text = "backend_busy", "Playback backend is busy; no command was applied."
            elif isinstance(exc, RevisionConflict):
                code, text = "revision_conflict", "Playback configuration or intention changed before execution."
            elif isinstance(exc, GainPersistenceError):
                code = "gain_rollback_failed" if exc.rollback_failed else "gain_save_failed"
                text = ("Gain persistence and live rollback failed; inspect the output before retrying."
                        if exc.rollback_failed else "Gain could not be saved; the previous live gain was restored if changed.")
            elif isinstance(exc, NoStationError):
                code, text = "no_station", "No station is selected for this output; select a station first."
            elif isinstance(exc, FavoriteNotFound):
                code, text = "favorite_not_found", "The favorite is no longer in the list; refresh and retry."
            elif isinstance(exc, FavoritesFullError):
                code, text = "favorites_full", f"The favorites list is full ({MAX_STATIONS} stations); remove one first."
            elif isinstance(exc, StationUnavailableError):
                code, text = "station_unavailable", f"{exc} The previous station remains selected."
            elif isinstance(exc, MPDOperationError):
                code, text = "mpd_failed", "MPD command or readback failed; inspect current status and any saved intention."
            elif isinstance(exc, AudioOperationError):
                code, text = "audio_failed", "Audio readiness, routing or gain application failed; inspect current status and any saved intention."
            elif operation["phase"] == "source_saving":
                code, text = "source_save_failed", "Selected source could not be saved; the previous selection remains in effect."
            elif operation["phase"] == "intention_saving":
                code, text = "intention_save_failed", "Playback intention could not be saved; read current status before retrying."
            if isinstance(exc, (AudioOperationError, StationUnavailableError)):
                logging.warning("Operation %s failed: %s (%s)", operation["id"], code, exc)
            else:
                logging.warning("Operation %s failed: %s", operation["id"], code)
            self._update(operation, status="failed",
                         applied=None if operation["phase"] in (
                             "gain_applying", "source_persisted", "intention_persisted",
                         ) else False,
                         error={"code": code, "message": text, "retryable": False})
        else:
            self._update(operation, status="applied", phase="complete", applied=True)
        return True

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Operation worker cannot be restarted")
        self._thread = threading.Thread(target=self._run, name="playback-operations")
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                if not self.run_once():
                    self._wake.wait(0.5)
        except Exception:
            self._failed = True
            logging.exception("Playback operation worker stopped unexpectedly; running outcomes require inspection")

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
        self._ownership.close()
