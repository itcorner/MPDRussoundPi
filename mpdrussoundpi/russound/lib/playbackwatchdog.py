"""Detect and restart MPD instances that hang or freeze while intended to play.

Design and thresholds: docs/plans/mpd_new_playback-stall-watchdog.md.
"""

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import subprocess
import time
from typing import TYPE_CHECKING, Protocol, cast

from lib.atomicjson import write_json
from lib.outputconfig import WatchdogConfig

if TYPE_CHECKING:
    from lib.mpdbackend import MPDBackend, OutputSettings


RESTART_START_MARGIN = 60.0
EVENT_HISTORY = 20
AUDIO_STACK_UNITS = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")


class WatchedOutput(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def intention(self) -> str: ...


@dataclass(frozen=True)
class ProbeResult:
    """One bounded MPD observation; unreachable results carry no fields."""
    reachable: bool
    state: str | None = None
    elapsed: float | None = None
    song_id: str | None = None


@dataclass
class _Tracking:
    probe_due: float = 0.0
    action_due: float = 0.0
    failures: int = 0
    first_failure: float | None = None
    baseline_song: str | None = None
    baseline_elapsed: float | None = None
    baseline_at: float | None = None
    stalled: str | None = None
    deferred: bool = False
    rate_limited: bool = False
    restarts: deque[float] = field(default_factory=deque)
    events: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=EVENT_HISTORY))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StallWatchdog:
    """Restart outputs whose MPD hangs or freezes; escalate when restarts do not help.

    Only evaluates outputs whose saved intention is ``play``; a stopped or
    paused player with a ``play`` intention remains the reconciler's job.
    """

    def __init__(
        self, config: WatchdogConfig, *,
        probe: Callable[[WatchedOutput], ProbeResult],
        restart: Callable[[str], None],
        escalate: Callable[[Sequence[str]], None],
        audio_gate: Callable[[], None],
        state_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._probe = probe
        self._restart = restart
        self._escalate = escalate
        self._audio_gate = audio_gate
        self._state_path = state_path
        self._clock = clock
        self._tracking: dict[str, _Tracking] = {}
        self._escalations: deque[dict[str, str]] = deque(maxlen=EVENT_HISTORY)
        self._last_escalation: float | None = None

    def tick(self, outputs: Iterable[WatchedOutput]) -> tuple[str, ...]:
        """One supervisor iteration; returns the output IDs restarted now."""
        if not self._config.enabled:
            return ()
        now = self._clock()
        known: dict[str, WatchedOutput] = {output.id: output for output in outputs}
        for identity in set(self._tracking) - set(known):
            del self._tracking[identity]
        restarted: list[str] = []
        for identity, output in known.items():
            tracking = self._tracking.setdefault(identity, _Tracking(probe_due=now))
            if output.intention != "play":
                self._reset(tracking)
                continue
            if tracking.stalled is None and now >= tracking.probe_due:
                tracking.probe_due = now + self._config.probe_interval_s
                self._evaluate(identity, tracking, self._probe(output), now)
            if tracking.stalled is not None:
                restarted.extend(self._act(identity, tracking, tuple(known), now))
        return tuple(restarted)

    def _reset(self, tracking: _Tracking) -> None:
        """Clear detection state; the restart window survives for rate limiting."""
        tracking.failures = 0
        tracking.first_failure = None
        tracking.baseline_song = None
        tracking.baseline_elapsed = None
        tracking.baseline_at = None
        tracking.stalled = None
        tracking.deferred = False

    def _evaluate(self, identity: str, tracking: _Tracking, result: ProbeResult, now: float) -> None:
        if not result.reachable:
            tracking.failures += 1
            if tracking.first_failure is None:
                tracking.first_failure = now
            tracking.baseline_at = None
            if (tracking.failures >= self._config.unresponsive_probes
                    and now - tracking.first_failure >= self._config.stall_threshold_s):
                tracking.stalled = "unresponsive"
                logging.warning("Watchdog: %s is unresponsive (%d failed probes over %.0fs)",
                                identity, tracking.failures, now - tracking.first_failure)
            return
        tracking.failures = 0
        tracking.first_failure = None
        if result.state != "play":
            tracking.baseline_at = None
            return
        moved = (tracking.baseline_at is None
                 or result.song_id != tracking.baseline_song
                 or result.elapsed != tracking.baseline_elapsed)
        if moved or tracking.baseline_at is None:
            tracking.baseline_song = result.song_id
            tracking.baseline_elapsed = result.elapsed
            tracking.baseline_at = now
        elif now - tracking.baseline_at >= self._config.stall_threshold_s:
            tracking.stalled = "frozen"
            logging.warning("Watchdog: %s reports play with frozen elapsed %s for %.0fs",
                            identity, result.elapsed, now - tracking.baseline_at)

    def _act(self, identity: str, tracking: _Tracking, known: tuple[str, ...], now: float) -> tuple[str, ...]:
        if now < tracking.action_due:
            return ()
        while tracking.restarts and now - tracking.restarts[0] > self._config.restart_window_s:
            tracking.restarts.popleft()
        if len(tracking.restarts) >= self._config.restart_limit:
            return self._try_escalation(identity, tracking, known, now)
        if not self._gate(identity, tracking):
            tracking.action_due = now + self._config.probe_interval_s
            return ()
        reason = tracking.stalled or "unknown"
        tracking.restarts.append(now)
        tracking.events.append({"at": _timestamp(), "reason": reason})
        try:
            self._restart(identity)
        except (OSError, subprocess.SubprocessError) as error:
            logging.error("Watchdog: restart of %s failed: %s", identity, error)
            tracking.action_due = now + self._config.restart_grace_s
            self._write_state()
            return ()
        logging.error("Watchdog: restarted %s (%s)", identity, reason)
        self._reset(tracking)
        tracking.probe_due = now + self._config.restart_grace_s
        self._write_state()
        return (identity,)

    def _try_escalation(self, identity: str, tracking: _Tracking, known: tuple[str, ...], now: float) -> tuple[str, ...]:
        if (self._last_escalation is not None
                and now - self._last_escalation < self._config.escalation_window_s):
            if not tracking.rate_limited:
                tracking.rate_limited = True
                logging.error("Watchdog: %s still stalled; restart and escalation limits exhausted, holding", identity)
                self._write_state()
            return ()
        if not self._gate(identity, tracking):
            tracking.action_due = now + self._config.probe_interval_s
            return ()
        self._last_escalation = now
        self._escalations.append({"at": _timestamp(), "output": identity})
        try:
            self._escalate(known)
        except (OSError, subprocess.SubprocessError) as error:
            logging.error("Watchdog: audio stack escalation for %s failed: %s", identity, error)
            tracking.action_due = now + self._config.restart_grace_s
            self._write_state()
            return ()
        logging.error("Watchdog: %s stalled despite restarts; restarted audio stack and all outputs", identity)
        for item in self._tracking.values():
            self._reset(item)
            item.restarts.clear()
            item.rate_limited = False
            item.probe_due = now + self._config.restart_grace_s
        self._write_state()
        return known

    def _gate(self, identity: str, tracking: _Tracking) -> bool:
        """Defer any restart while the shared audio path is not ready."""
        try:
            self._audio_gate()
        except (OSError, RuntimeError) as error:
            if not tracking.deferred:
                tracking.deferred = True
                logging.warning("Watchdog: %s restart deferred, audio not ready: %s", identity, error)
                self._write_state()
            return False
        if tracking.deferred:
            tracking.deferred = False
            logging.info("Watchdog: audio ready again, %s restart proceeds", identity)
        return True

    def _write_state(self) -> None:
        if self._state_path is None:
            return
        value = {
            "outputs": {
                identity: {
                    "restarts": list(tracking.events),
                    "deferred": "audio_not_ready" if tracking.deferred else None,
                    "rate_limited": tracking.rate_limited,
                }
                for identity, tracking in sorted(self._tracking.items())
            },
            "escalations": list(self._escalations),
        }
        try:
            write_json(self._state_path, value)
        except OSError as error:
            logging.error("Watchdog: cannot write state file: %s", error)


def default_state_path() -> Path:
    return (Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "mpdrussoundpi/watchdog.json")


def _parse_elapsed(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def create_watchdog(backend: "MPDBackend", config: WatchdogConfig) -> StallWatchdog:
    """Wire a watchdog to a live MPDBackend, systemctl and the audio readiness check."""
    from lib.audioready import audio_ready
    from lib.mpdbackend import BackendError

    restart_timeout = 120.0 + RESTART_START_MARGIN

    def probe(output: WatchedOutput) -> ProbeResult:
        # tick() passes the supervisor's OutputSettings, which carry the port.
        stream = cast("OutputSettings", output)
        try:
            with backend.observation_connection(stream) as client:
                status = client.status()
        except (BackendError, OSError):
            return ProbeResult(False)
        state = status.get("state")
        song_id = status.get("songid")
        return ProbeResult(True, state if isinstance(state, str) else None,
                           _parse_elapsed(status.get("elapsed")),
                           song_id if isinstance(song_id, str) else None)

    def restart(identity: str) -> None:
        subprocess.run(["systemctl", "--user", "restart", f"mpd_{identity}.service"],
                       check=True, capture_output=True, text=True, timeout=restart_timeout)

    def escalate(identities: Sequence[str]) -> None:
        subprocess.run(["systemctl", "--user", "restart", *AUDIO_STACK_UNITS],
                       check=True, capture_output=True, text=True, timeout=restart_timeout)
        subprocess.run(
            ["systemctl", "--user", "restart", *(f"mpd_{identity}.service" for identity in identities)],
            check=True, capture_output=True, text=True, timeout=restart_timeout)

    def gate() -> None:
        audio_ready(backend.config_path, enforce_profile=False)

    return StallWatchdog(config, probe=probe, restart=restart, escalate=escalate,
                         audio_gate=gate, state_path=default_state_path())
