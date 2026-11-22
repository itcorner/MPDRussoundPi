"""Read-only check whether a newer published web image exists.

The image knows what it was published as (``RUSSOUND_BUILD_IMAGE``,
``RUSSOUND_BUILD_VERSION``, baked in by CI). A background thread compares that
with the channel's moving tag on Docker Hub, through the shared
``mpdrussoundpi.common.imageregistry`` client:

- a development image (``dev-<commit>``) with the ``dev`` tag, which CI moves to
  every new ``develop`` build;
- a release or release candidate (``X.Y.Z``, ``X.Y.Z-rc.N``) with ``latest``,
  the newest stable release.

The result is cached in memory and served to every browser; browsers never
contact the registry. Nothing is pulled, installed or restarted. A failed lookup
keeps the last result, marked stale, and is never reported as up to date.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import platform
import re
import threading
import time
from typing import Any, Callable, Mapping

from mpdrussoundpi.common.imageregistry import REPOSITORIES, SOURCE, Registry, RegistryError, platform_config

CHECK_INTERVAL_SECONDS = 6 * 3600
FIRST_CHECK_DELAY_SECONDS = 60
MANUAL_MIN_INTERVAL_SECONDS = 60
STALE_AFTER_SECONDS = 24 * 3600
DEV_VERSION = re.compile(r"dev-([a-f0-9]{40})\Z")
RELEASE_VERSION = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-rc\.(\d{1,4}))?\Z")
BUILD_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
FAILED_MESSAGE = "Docker Hub could not be reached or returned unexpected data."
ARCHITECTURES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}

logger = logging.getLogger(__name__)


def _timestamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.utcoffset() is not None else None


def _release_key(version: str) -> tuple[int, int, int, int, int] | None:
    match = RELEASE_VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, candidate = match.groups()
    # A release sorts after every candidate of the same version.
    return int(major), int(minor), int(patch), 0 if candidate is not None else 1, int(candidate or 0)


def hub_link(repository: str, version: str) -> str:
    return f"https://hub.docker.com/r/{repository}/tags?name={version}"


class UpdateChecker:
    """Cached, rate-limited comparison of the running web image with its channel tag."""

    def __init__(self, environ: Mapping[str, str] | None = None, *,
                 registry_factory: Callable[[], Any] = Registry,
                 clock: Callable[[], float] = time.time,
                 machine: str | None = None) -> None:
        env = os.environ if environ is None else environ
        self._registry_factory = registry_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = clock()
        self._source = env.get("RUSSOUND_BUILD_SOURCE", "")
        self._repository = env.get("RUSSOUND_BUILD_IMAGE", "")
        self._version = env.get("RUSSOUND_BUILD_VERSION", "")
        self._revision = env.get("RUSSOUND_BUILD_REVISION", "")
        build_time = env.get("RUSSOUND_BUILD_TIME", "")
        self._built_at = _parse_time(build_time) if BUILD_TIME.fullmatch(build_time) else None
        self._build_time = build_time if BUILD_TIME.fullmatch(build_time) else None
        architecture = ARCHITECTURES.get((machine or platform.machine()).lower())
        self._platform = f"linux/{architecture}" if architecture else None
        self._state = "pending"
        self._reason: str | None = None
        self._enabled = env.get("RUSSOUND_UPDATE_CHECK", "on").strip().lower() not in ("off", "false", "0", "no")
        self._result: dict[str, Any] | None = None  # last successful comparison
        self._checked_at: float | None = None       # time of that comparison
        self._attempted_at: float | None = None
        self._failed = False
        self._next_at = self._started_at + FIRST_CHECK_DELAY_SECONDS
        self._channel: str | None = None
        self._unsupported = self._classify()

    # ------------------------------------------------------------ identity

    def _classify(self) -> str | None:
        """Why this image cannot be checked, or None when it can."""
        if not self._enabled:
            return "Update checks are turned off for this installation (RUSSOUND_UPDATE_CHECK=off)."
        if self._source == "local":
            return "This is a local build; it is not published, so there is no newer image to compare with."
        if self._source != "ci" or self._repository != REPOSITORIES["web"]:
            return "This build does not report a published image to compare with."
        if DEV_VERSION.fullmatch(self._version):
            self._channel = "dev"
        elif _release_key(self._version) is not None:
            self._channel = "latest"
        else:
            return "This build does not report a published image to compare with."
        if self._platform is None:
            return "The platform of this host is not one the project publishes."
        return None

    # -------------------------------------------------------------- public

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            state = ("disabled" if not self._enabled else "unsupported" if self._unsupported else self._state)
            result = dict(self._result) if self._result else None
            checked_at, attempted_at, failed, next_at = self._checked_at, self._attempted_at, self._failed, self._next_at
        stale = checked_at is not None and (failed or now - checked_at > STALE_AFTER_SECONDS)
        manual_wait = 0 if attempted_at is None else max(0, int(attempted_at + MANUAL_MIN_INTERVAL_SECONDS - now))
        return {
            "api_version": 1,
            "state": state,
            "reason": self._unsupported,
            "installed": {"image": self._repository, "version": self._version,
                          "revision": self._revision[:7] or None} if self._channel else None,
            "channel": self._channel,
            # Identity of the running build, also for local builds: an open page
            # reloads itself when this changes after an update.
            "build": {"revision": self._revision or None, "built_at": self._build_time},
            "result": result,
            "checked_at": None if checked_at is None else _timestamp(checked_at),
            "attempted_at": None if attempted_at is None else _timestamp(attempted_at),
            "check_failed": failed,
            "error": FAILED_MESSAGE if failed else None,
            "stale": stale,
            "next_check_at": None if self._unsupported or state == "checking" else _timestamp(next_at),
            "manual_check": {"available": not self._unsupported and state != "checking" and manual_wait == 0,
                             "retry_after_seconds": manual_wait},
        }

    def request_check(self) -> tuple[bool, int]:
        """Ask for a check now; returns (accepted, seconds to wait). Concurrent requests coalesce."""
        if self._unsupported:
            return False, 0
        now = self._clock()
        with self._lock:
            if self._state == "checking":
                return True, 0
            if self._attempted_at is not None and now - self._attempted_at < MANUAL_MIN_INTERVAL_SECONDS:
                return False, int(self._attempted_at + MANUAL_MIN_INTERVAL_SECONDS - now) + 1
            self._next_at = now
        self._wake.set()
        return True, 0

    def start(self) -> None:
        if self._unsupported or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="update-check")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------ checking

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                delay = self._next_at - self._clock()
            if delay > 0:
                self._wake.wait(timeout=min(delay, 300))
                self._wake.clear()
                continue
            self.check_once()

    def check_once(self) -> None:
        if self._unsupported:
            return
        with self._lock:
            self._state = "checking"
            self._attempted_at = self._clock()
        try:
            result = self._compare(self._registry_factory())
        except (RegistryError, OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("Update check failed: %s", error)
            with self._lock:
                self._failed = True
                self._state = "check_failed" if self._result is None else self._result["state"]
                self._next_at = self._clock() + CHECK_INTERVAL_SECONDS
            return
        with self._lock:
            self._result = result
            self._failed = False
            self._checked_at = self._clock()
            self._state = result["state"]
            self._next_at = self._checked_at + CHECK_INTERVAL_SECONDS

    def _compare(self, registry: Any) -> dict[str, Any]:
        assert self._channel is not None and self._platform is not None
        found = platform_config(registry, self._repository, self._channel, self._platform)
        labels = found["labels"]
        version, revision = labels.get("org.opencontainers.image.version"), labels.get("org.opencontainers.image.revision")
        if (labels.get("org.opencontainers.image.source") != SOURCE or not isinstance(version, str)
                or not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)):
            raise RegistryError("Channel tag has unexpected labels")
        created = _parse_time(found["created"])
        available = {"version": version, "revision": revision[:7], "published_at": None if created is None else _timestamp(created),
                     "link": hub_link(self._repository, version)}
        if self._channel == "dev":
            if DEV_VERSION.fullmatch(version) is None or version != "dev-" + revision:
                raise RegistryError("Development tag has unexpected labels")
            if version == self._version:
                state = "up_to_date"
            elif created is not None and self._built_at is not None and created <= self._built_at:
                state = "up_to_date"  # the moving tag lags behind this build; it is not newer
            else:
                state = "update_available"
        else:
            theirs, ours = _release_key(version), _release_key(self._version)
            if theirs is None or ours is None:
                raise RegistryError("Release tag has an unexpected version")
            state = "update_available" if theirs > ours else "up_to_date"
        return {"state": state, "available": available}
