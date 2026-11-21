"""Administrator login for privileged actions (stage 1 of the web-triggered update plan).

One shared administrator password, stored only as an scrypt hash in a file in the
secrets mount (``RUSSOUND_ADMIN_PASSWORD_FILE``). It is set, rotated or reset only
locally (``python -m web.admin_auth set`` or ``hash``), never over HTTP. Changing
the file revokes every session and clears the lockout, because both are bound to
the hash's generation.

- Lockout: five failed attempts in total lock the login for 15 minutes; attempts
  during the lock extend it; a success clears the counter. The state is persisted
  next to the application state and survives restarts and clock rollback.
- Sessions: opaque random ids kept hashed in memory, ``HttpOnly`` and
  ``SameSite=Strict`` cookies (``Secure`` over HTTPS), 15 minutes idle and 8 hours
  absolute, rotated on login, revoked on logout. A synchronizer CSRF token is
  required for every privileged request, and disruptive actions additionally need
  the password entered within the last 5 minutes.
- Audit: every login, failure, lock, logout and reauthentication is recorded with
  time, client address and a short session reference. No password, cookie or
  token is ever logged.

The UI runs over plain HTTP for now (plan decision 6): on that network the
password and the session cookie can be observed.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any, Callable

from mpdrussoundpi.common.atomicjson import write_json

DEFAULT_PASSWORD_FILE = "/run/secrets/russound-admin.scrypt"
COOKIE_NAME = "russound_admin"
CSRF_HEADER = "X-CSRF-Token"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
IDLE_SECONDS = 15 * 60
ABSOLUTE_SECONDS = 8 * 3600
REAUTH_SECONDS = 5 * 60
MAX_SESSIONS = 16
CLIENT_WINDOW_SECONDS = 60
CLIENT_MAX_ATTEMPTS = 10

audit_logger = logging.getLogger("russound.audit")


# ------------------------------------------------------------------ hashing

def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """``scrypt$n$r$p$salt$hash`` with base64 salt and hash; scrypt is memory-hard."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)
    encode = lambda value: base64.b64encode(value).decode()  # noqa: E731
    return f"scrypt${n}${r}${p}${encode(salt)}${encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.strip().split("$")
        if scheme != "scrypt" or not (2 ** 14 <= int(n) <= 2 ** 20) or not (1 <= int(r) <= 32) or not (1 <= int(p) <= 4):
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=SCRYPT_MAXMEM * 4, dklen=len(base64.b64decode(expected)))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


def _short(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


# ----------------------------------------------------------------- sessions

@dataclass
class Session:
    created: float
    last_seen: float
    authenticated_at: float
    generation: str
    csrf: str
    reference: str


class AdminAuth:
    """Password check, lockout, sessions and CSRF for the administrator."""

    def __init__(self, password_file: str | Path | None, lockout_file: str | Path | None = None, *,
                 clock: Callable[[], float] = time.time, audit_file: str | Path | None = None) -> None:
        self._password_file = Path(password_file) if password_file else None
        self._lockout_file = Path(lockout_file) if lockout_file else None
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._client_attempts: dict[str, list[float]] = {}
        self._hash_cache: tuple[tuple[int, int, int], str | None] | None = None
        self._audit_file = Path(audit_file) if audit_file else None
        self._lockout = self._load_lockout()

    # ------------------------------------------------------------ credential

    def _encoded_hash(self) -> str | None:
        """The stored hash, re-read when the file changes (a reset needs no restart)."""
        if self._password_file is None:
            return None
        try:
            stat = self._password_file.stat()
        except OSError:
            self._hash_cache = None
            return None
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if self._hash_cache is None or self._hash_cache[0] != signature:
            try:
                text = self._password_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                text = ""
            self._hash_cache = (signature, text if text.startswith("scrypt$") else None)
        return self._hash_cache[1]

    def configured(self) -> bool:
        return self._encoded_hash() is not None

    def _generation(self) -> str | None:
        encoded = self._encoded_hash()
        return None if encoded is None else _short(encoded)

    # --------------------------------------------------------------- lockout

    def _load_lockout(self) -> dict[str, Any]:
        state = {"generation": None, "failures": 0, "locked_until": 0.0, "last_failure_at": 0.0}
        if self._lockout_file is not None:
            try:
                stored = json.loads(self._lockout_file.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    state.update({key: stored[key] for key in state if key in stored})
            except (OSError, ValueError):
                pass
        return state

    def _save_lockout(self) -> None:
        if self._lockout_file is not None:
            try:
                write_json(self._lockout_file, self._lockout)
            except OSError:
                logging.getLogger(__name__).warning("Could not persist the administrator lockout state")

    def _current_lockout(self, now: float) -> dict[str, Any]:
        generation = self._generation()
        if self._lockout.get("generation") != generation:
            # A new password (local reset) clears the lockout.
            self._lockout = {"generation": generation, "failures": 0, "locked_until": 0.0, "last_failure_at": 0.0}
            self._save_lockout()
        if now < float(self._lockout.get("last_failure_at") or 0):
            # The clock went backwards: stay locked for a full period from now.
            self._lockout["locked_until"] = now + LOCK_SECONDS if self._lockout["failures"] >= MAX_FAILURES else 0.0
            self._lockout["last_failure_at"] = now
            self._save_lockout()
        return self._lockout

    def locked_for(self) -> int:
        with self._lock:
            now = self._clock()
            return max(0, int(float(self._current_lockout(now)["locked_until"]) - now + 0.999))

    # ----------------------------------------------------------------- audit

    def _audit(self, event: str, client: str, reference: str | None = None) -> None:
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())), "event": event,
                  "client": client[:64], "session": reference}
        audit_logger.warning("admin %s client=%s session=%s", event, record["client"], reference or "-")
        if self._audit_file is not None:
            try:
                with self._audit_file.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record) + "\n")
            except OSError:
                pass

    # ----------------------------------------------------------------- login

    def _verify(self, password: object, client: str, purpose: str) -> tuple[str, int]:
        """Throttle, lockout and password check. Results: ok, invalid, locked, throttled, not_configured."""
        now = self._clock()
        with self._lock:
            encoded = self._encoded_hash()
            if encoded is None:
                return "not_configured", 0
            recent = [moment for moment in self._client_attempts.get(client, []) if now - moment < CLIENT_WINDOW_SECONDS]
            if len(recent) >= CLIENT_MAX_ATTEMPTS:
                self._client_attempts[client] = recent
                return "throttled", int(recent[0] + CLIENT_WINDOW_SECONDS - now) + 1
            self._client_attempts[client] = recent + [now]
            lockout = self._current_lockout(now)
            if float(lockout["locked_until"]) > now:
                lockout["locked_until"] = now + LOCK_SECONDS  # attempts during the lock extend it
                lockout["last_failure_at"] = now
                self._save_lockout()
                self._audit(f"{purpose}_refused_locked", client)
                return "locked", LOCK_SECONDS
        valid = isinstance(password, str) and 0 < len(password) <= MAX_PASSWORD_LENGTH and verify_password(password, encoded)
        with self._lock:
            lockout = self._current_lockout(now)
            if not valid:
                lockout["failures"] = int(lockout["failures"]) + 1
                lockout["last_failure_at"] = now
                locked = lockout["failures"] >= MAX_FAILURES
                if locked:
                    lockout["locked_until"] = now + LOCK_SECONDS
                self._save_lockout()
                self._audit(f"{purpose}_failed" + ("_locked" if locked else ""), client)
                return ("locked", LOCK_SECONDS) if locked else ("invalid", 0)
            lockout.update(failures=0, locked_until=0.0)
            self._save_lockout()
            return "ok", 0

    def login(self, password: object, client: str) -> tuple[str, str | None, int]:
        """Returns (result, session id, retry-after)."""
        result, retry = self._verify(password, client, "login")
        if result != "ok":
            return result, None, retry
        now = self._clock()
        with self._lock:
            session_id = secrets.token_urlsafe(32)
            reference = _short(session_id)
            self._sessions[_short_key(session_id)] = Session(now, now, now, self._generation() or "",
                                                             secrets.token_urlsafe(32), reference)
            self._prune(now)
        self._audit("login", client, reference)
        return "ok", session_id, 0

    def _prune(self, now: float) -> None:
        generation = self._generation()
        for key, session in list(self._sessions.items()):
            if not self._alive(session, now, generation):
                del self._sessions[key]
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].created)
            del self._sessions[oldest]

    @staticmethod
    def _alive(session: Session, now: float, generation: str | None) -> bool:
        return (generation is not None and session.generation == generation
                and now - session.last_seen <= IDLE_SECONDS and now - session.created <= ABSOLUTE_SECONDS)

    def session(self, session_id: str | None, *, touch: bool = True) -> Session | None:
        if not session_id or len(session_id) > 128:
            return None
        now = self._clock()
        with self._lock:
            key = _short_key(session_id)
            current = self._sessions.get(key)
            if current is None:
                return None
            if not self._alive(current, now, self._generation()):
                del self._sessions[key]
                return None
            if touch:
                current.last_seen = now
            return current

    def logout(self, session_id: str | None, client: str) -> None:
        with self._lock:
            removed = self._sessions.pop(_short_key(session_id), None) if session_id else None
        if removed is not None:
            self._audit("logout", client, removed.reference)

    def reauthenticate(self, session_id: str | None, password: object, client: str) -> tuple[str, int]:
        current = self.session(session_id)
        if current is None:
            return "unauthenticated", 0
        result, retry = self._verify(password, client, "reauthenticate")
        if result != "ok":
            return result, retry
        with self._lock:
            current.authenticated_at = self._clock()
        self._audit("reauthenticated", client, current.reference)
        return "ok", 0

    # ------------------------------------------------------------ authorization

    def authorize(self, session_id: str | None, csrf: str | None, *, recent: bool = False) -> str | None:
        """None when allowed; otherwise unauthenticated, csrf or reauthenticate."""
        current = self.session(session_id)
        if current is None:
            return "unauthenticated"
        if not csrf or not hmac.compare_digest(csrf, current.csrf):
            return "csrf"
        if recent and self._clock() - current.authenticated_at > REAUTH_SECONDS:
            return "reauthenticate"
        return None

    def describe(self, session_id: str | None) -> dict[str, Any]:
        current = self.session(session_id, touch=False)
        now = self._clock()
        body: dict[str, Any] = {"api_version": 1, "configured": self.configured(), "authenticated": current is not None,
                                "locked_for_seconds": self.locked_for()}
        if current is not None:
            body.update({
                "csrf_token": current.csrf,
                "idle_expires_in_seconds": max(0, int(current.last_seen + IDLE_SECONDS - now)),
                "expires_in_seconds": max(0, int(current.created + ABSOLUTE_SECONDS - now)),
                "recently_authenticated": now - current.authenticated_at <= REAUTH_SECONDS,
            })
        return body


def _short_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


# ------------------------------------------------------------- local command

def _read_new_password() -> str:
    first = getpass.getpass("New administrator password: ")
    if not MIN_PASSWORD_LENGTH <= len(first) <= MAX_PASSWORD_LENGTH:
        raise SystemExit(f"Use between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Repeat the password: ") != first:
        raise SystemExit("The passwords differ; nothing was changed.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set or reset the administrator password locally. Writing a new hash file revokes "
                    "every administrator session and clears the login lockout.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("set", help="write the hash file (run on the host that holds the secrets)")
    write.add_argument("--file", required=True, help="hash file in the deployment's secrets directory")
    write.add_argument("--uid", type=int, help="numeric owner (the web service user, e.g. 10001)")
    write.add_argument("--gid", type=int, help="numeric group")
    commands.add_parser("hash", help="print a hash line to standard output, e.g. to pipe into sudo tee")
    args = parser.parse_args(argv)
    encoded = hash_password(_read_new_password())
    if args.command == "hash":
        print(encoded)
        return 0
    target = Path(args.file)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded + "\n")
            output.flush()
            os.fsync(output.fileno())
        if args.uid is not None or args.gid is not None:
            os.chown(temporary, -1 if args.uid is None else args.uid, -1 if args.gid is None else args.gid)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Administrator password written to {target}; existing sessions are revoked.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
