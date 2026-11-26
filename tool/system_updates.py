"""Pi system updates from the Status page: package check, install and restart.

See docs/plans/general_open_pi-system-updates.md. Part of the updater service
(tool/deployment_updater.py), which routes the ``system_*`` requests here; the
web container never runs anything on the host itself.

- The **check** simulates ``apt-get full-upgrade`` against the local package
  lists and asks ``apt-cache policy`` which candidate versions come from a
  ``*-security`` suite (the simulation names only one origin per package). It
  also reports when the lists were refreshed, whether a restart is needed and
  the last run of ``unattended-upgrades``. "Check now" refreshes the lists first.
- The **install** is reviewed (the planned package list is bound to the
  confirmation digest) and runs ``apt-get full-upgrade`` as its own transient
  systemd unit under the operator lock, so it survives a restart of the updater
  and excludes the SSH operator. The unit writes its exit code to a file; the updater only
  reads that file and the log.
- The **restart** is a separate reviewed action, offered only when a restart is
  needed. Success is reported only after the boot ID changed and the
  containers and services that ran before are back.

Nothing is retried or rolled back automatically: a package upgrade has no undo.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from mpdrussoundpi.common.atomicjson import write_json

logger = logging.getLogger("mpdrussoundpi.system")

Run = Callable[[list[str], float], tuple[int, str]]

UNIT_PREFIX = "mpdrussoundpi-system-update-"
REVIEW_SECONDS = 120
# The confirmation window starts when an administrator's page first receives the
# review (computing it takes about a minute for a few hundred packages, and a
# page in a background tab polls late). A review nobody fetches ends after this.
UNSEEN_REVIEW_SECONDS = 600
MANUAL_CHECK_SECONDS = 600       # "Check now" refreshes the package lists at most every 10 minutes
RECHECK_SECONDS = 6 * 3600       # the cached result is recomputed at least this often
STALE_INDEX_SECONDS = 2 * 86400  # package lists older than this are reported as stale
VERIFY_SECONDS = 300             # containers and services must be back within this time
REBOOT_SECONDS = 600             # the boot ID must change within this time
CONFLICT_EXIT = 75               # flock: another operator holds the lock
MAX_PACKAGES = 600
RESTART_NAMES = frozenset({"raspi-firmware", "libc6", "systemd", "dbus"})
# firmware-*: device firmware (the Pi's onboard Wi-Fi and Bluetooth use firmware-brcm80211)
# is loaded when the driver starts, so a new version takes effect only after a restart.
RESTART_PREFIXES = ("linux-image-", "firmware-")
DOCKER_NAMES = frozenset({"docker-ce", "docker-ce-cli", "containerd.io"})
ACTIVE = ("preparing", "awaiting_approval", "installing", "verifying", "rebooting", "verifying_restart")

INSTALL_INTERRUPTION = ("Takes several minutes. If Docker is updated, the web app and gateway restart once and "
                        "this page reconnects.")
NO_UNDO = "There is no undo: installed packages cannot be restored to their old versions. Your data is not changed."
CONFIG_NOTE = "Configuration files you changed are kept; new versions are saved next to them."
RESTART_INTERRUPTION = "Playback and this page stop for about one to two minutes while the Pi restarts."

DEFAULT_PATHS = {
    "lists": Path("/var/lib/apt/lists"),
    "dpkg_status": Path("/var/lib/dpkg/status"),
    "dpkg_logs": (Path("/var/log/dpkg.log.1"), Path("/var/log/dpkg.log")),
    "reboot_flag": Path("/run/reboot-required"),
    "unattended_log": Path("/var/log/unattended-upgrades/unattended-upgrades.log"),
    "boot_id": Path("/proc/sys/kernel/random/boot_id"),
    "proc_stat": Path("/proc/stat"),
    "dpkg_locks": (Path("/var/lib/dpkg/lock-frontend"), Path("/var/lib/dpkg/lock")),
    "operator_lock": Path("/var/lib/mpdrussoundpi-operations/evidence/operator.lock"),
    "cgroups": Path("/sys/fs/cgroup/system.slice"),
}
# The watcher decides that the install unit has ended only after this many
# agreeing observations, ENDED_INTERVAL seconds apart: dpkg replaces libc6,
# libssl and systemd's own libraries during an upgrade, and a single
# ``systemctl`` call can fail meanwhile.
ENDED_CONFIRMATIONS = 3
ENDED_INTERVAL = 10

INST = re.compile(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+) ")
REMV = re.compile(r"^Remv (\S+)")
POLICY_VERSION = re.compile(r"^ (?:\*\*\*|   ) (\S+) -?\d+$")
POLICY_SOURCE = re.compile(r"^\s+-?\d+ (\S+) (\S+) \S+ Packages$")
DPKG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (install|upgrade) (\S+) ")
UU_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (.*)$")
CONFFILE = re.compile(r"^Configuration file '(/[^']{1,200})'")


class SystemRefused(ValueError):
    """A request the system part refuses; the message is safe to show."""


def _now_text(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def _local_epoch(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def default_run(args: list[str], timeout: float = 120) -> tuple[int, str]:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    return result.returncode, result.stdout + ("\n" + result.stderr if result.stderr else "")


# ------------------------------------------------------------------ parsing

def parse_simulation(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """``apt-get -s full-upgrade``: (installs with name, from, to) and removed package names."""
    installs, removals = [], []
    for line in text.splitlines():
        match = INST.match(line)
        if match:
            installs.append({"name": match.group(1), "from": match.group(2), "to": match.group(3)})
            continue
        match = REMV.match(line)
        if match:
            removals.append(match.group(1))
    return installs, removals


def security_versions(text: str) -> dict[str, set[str]]:
    """``apt-cache policy``: per package, the versions offered by a ``<codename>-security`` suite."""
    found: dict[str, set[str]] = {}
    package, version, in_table = None, None, False
    for line in text.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            package, version, in_table = line[:-1], None, False
            continue
        if package is None:
            continue
        if line.strip() == "Version table:":
            in_table = True
            continue
        if not in_table:
            continue
        match = POLICY_VERSION.match(line)
        if match:
            version = match.group(1)
            continue
        match = POLICY_SOURCE.match(line)
        if match and version and match.group(2).split("/")[0].endswith("-security"):
            found.setdefault(package, set()).add(version)
    return found


def needs_restart(name: str) -> bool:
    base = name.split(":")[0]
    return base in RESTART_NAMES or base.startswith(RESTART_PREFIXES)


def restart_reasons(paths: dict[str, Any]) -> list[str]:
    """Why a restart is needed: the reboot flag, or a listed package installed after the current boot."""
    reasons: list[str] = []
    if Path(paths["reboot_flag"]).exists():
        reasons.append("a package asked for a restart")
    boot = boot_time(paths)
    if boot is None:
        return reasons
    names: set[str] = set()
    for log in paths["dpkg_logs"]:
        try:
            text = Path(log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = DPKG_LINE.match(line)
            if not match or not needs_restart(match.group(3)):
                continue
            moment = _local_epoch(match.group(1))
            if moment is not None and moment >= boot:
                names.add(match.group(3).split(":")[0])
    if names:
        listed = sorted(names)
        reasons.append("updated since the last start: " + ", ".join(listed[:5]) + (" …" if len(listed) > 5 else ""))
    return reasons


def boot_time(paths: dict[str, Any]) -> float | None:
    try:
        for line in Path(paths["proc_stat"]).read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def boot_id(paths: dict[str, Any]) -> str | None:
    try:
        value = Path(paths["boot_id"]).read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f-]{36}", value) else None


def last_automatic_run(paths: dict[str, Any]) -> dict[str, Any] | None:
    """The last run of unattended-upgrades from its log: when, what it installed and how it ended."""
    try:
        lines = Path(paths["unattended_log"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = None
    for index, line in enumerate(lines):
        if "Starting unattended upgrades script" in line:
            start = index
    if start is None:
        return None
    run = {"at": None, "result": "running", "packages": []}
    for line in lines[start:]:
        match = UU_LINE.match(line)
        if not match:
            continue
        stamp, level, message = match.groups()
        if run["at"] is None:
            moment = _local_epoch(stamp)
            run["at"] = _now_text(moment) if moment is not None else None
        if message.startswith("Packages that will be upgraded:"):
            run["packages"] = message.split(":", 1)[1].split()[:50]
        if level in ("ERROR", "CRITICAL"):
            run["result"] = "failed"
        elif run["result"] != "failed" and message.startswith("All upgrades installed"):
            run["result"] = "installed"
        elif run["result"] != "failed" and message.startswith("No packages found that can be upgraded"):
            run["result"] = "nothing"
    return run


def index_time(paths: dict[str, Any]) -> float | None:
    """When the package lists were last refreshed: the newest Release file."""
    try:
        times = [entry.stat().st_mtime for entry in Path(paths["lists"]).iterdir()
                 if entry.name.endswith(("_InRelease", "_Release"))]
    except OSError:
        return None
    return max(times) if times else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def plan_digest(installs: list[dict[str, Any]], removals: list[str]) -> str:
    planned = {"install": sorted((item["name"], item["to"]) for item in installs), "remove": sorted(removals)}
    return "sha256:" + hashlib.sha256(json.dumps(planned, sort_keys=True).encode()).hexdigest()


def compute_check(run: Run, paths: dict[str, Any], clock: Callable[[], float]) -> dict[str, Any]:
    """What is pending, against the local package lists (no refresh)."""
    code, output = run(["apt-get", "-s", "-q", "-o", "Debug::NoLocking=1", "full-upgrade"], 300)
    if code:
        raise SystemRefused("The pending updates could not be computed (apt-get simulation failed).")
    installs, removals = parse_simulation(output)
    security: dict[str, set[str]] = {}
    names = [item["name"] for item in installs]
    for start in range(0, len(names), 200):
        code, output = run(["apt-cache", "policy", *names[start:start + 200]], 120)
        if code:
            raise SystemRefused("The security origin of the pending updates could not be read (apt-cache failed).")
        security.update(security_versions(output))
    packages = [{**item, "security": item["to"] in security.get(item["name"], set())} for item in installs]
    indexed = index_time(paths)
    return {
        "checked_at": _now_text(clock()),
        "index_updated_at": _now_text(indexed) if indexed else None,
        "index_stale": indexed is None or clock() - indexed > STALE_INDEX_SECONDS,
        "upgrades": sum(1 for item in packages if item["from"]),
        "new": sum(1 for item in packages if not item["from"]),
        "removals": len(removals),
        "security": sum(1 for item in packages if item["security"]),
        "packages": packages[:MAX_PACKAGES],
        "removed": removals[:50],
        "digest": plan_digest(installs, removals),
        "restart": {"reasons": restart_reasons(paths)},
        "automatic": last_automatic_run(paths),
    }


# ---------------------------------------------------------- service health

def services_snapshot(run: Run) -> dict[str, Any]:
    """The containers and project services running now, to compare after an install or a restart."""
    containers: dict[str, bool] = {}
    code, output = run(["docker", "ps", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], 60)
    if code:
        raise SystemRefused("Docker did not list the containers.")
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            healthy = parts[1] == "running" and "(health: starting)" not in parts[2] and "(unhealthy)" not in parts[2]
            containers[parts[0]] = healthy
    code, output = run(["systemctl", "list-units", "--type=service", "--state=active", "--no-legend", "--plain",
                        "mpdrussoundpi-*", "docker.service"], 60)
    units = sorted({line.split()[0] for line in output.splitlines() if line.strip()
                    and not line.split()[0].startswith(UNIT_PREFIX)}) if code == 0 else []
    return {"containers": sorted(containers), "healthy": sorted(name for name, ok in containers.items() if ok),
            "units": units}


def missing_services(before: dict[str, Any], run: Run) -> list[str]:
    """What ran before and is not running (and healthy) now; empty when everything is back."""
    try:
        now = services_snapshot(run)
    except SystemRefused:
        return ["Docker"]
    missing = [f"container {name}" for name in before.get("containers", []) if name not in now["healthy"]]
    missing += [f"service {name}" for name in before.get("units", []) if name not in now["units"]]
    return missing


# --------------------------------------------------------------- locks

def dpkg_busy(paths: dict[str, Any]) -> bool:
    """Whether apt or dpkg (for example unattended-upgrades) holds its lock right now."""
    for path in paths["dpkg_locks"]:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(fd, fcntl.LOCK_UN)
        except OSError:
            return True
        finally:
            os.close(fd)
    return False


def operator_busy(path: Path) -> bool:
    """Whether the SSH operator (or a running install) holds the operator lock.

    The file is created with the mode the operator expects, so the install's flock never creates it.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# ------------------------------------------------------------- the service

class SystemUpdates:
    """One system operation at a time, with a durable record that survives restarts of the updater."""

    def __init__(self, evidence: Path, state_path: Path, *, run: Run = default_run,
                 paths: dict[str, Any] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, deployment_clear: Callable[[], bool] = lambda: True,
                 other_busy: Callable[[], bool] = lambda: False, gate: Any = None,
                 reboot: Callable[[], tuple[int, str]] | None = None) -> None:
        self.evidence = evidence
        self.state_path = state_path
        self.paths = {**DEFAULT_PATHS, **(paths or {})}
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._deployment_clear = deployment_clear
        self.other_busy = other_busy
        self.gate = gate or threading.Lock()
        self._reboot = reboot or (lambda: run(["systemctl", "reboot"], 60))
        self._lock = threading.Condition()
        self._decision: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._restart_cache: tuple[Any, list[str]] | None = None
        self.check: dict[str, Any] = {"state": "pending", "result": None, "error": None,
                                      "manual_at": None, "key": None, "computed_at": None}
        self.state = self._load()

    # ------------------------------------------------------------ durable state

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("phase", "idle")
        phase = state["phase"]
        if phase in ("preparing", "awaiting_approval"):
            state.update(phase="cancelled", review=None, error="The updater restarted before approval.")
        elif (phase == "outcome_unknown" and state.get("kind") == "install" and state.get("result") is None
              and state.get("unit") and (self._exit_path(state).exists() or self._unit_state(state) == "running")):
            # Declared unknown too early while the install still ran: watch it again.
            logger.warning("Install %s is still running or has finished; watching it again", state.get("operation"))
            state.update(phase="installing", error=None)
        elif phase == "installing" and not self._exit_path(state).exists() and self._unit_ended(state):
            state.update(phase="outcome_unknown", error=(
                "The updater restarted during the installation and its end was not recorded. Check the Pi with "
                "'sudo dpkg --audit' over SSH before installing again."))
        write_json(self.state_path, dict(state, updated_at=_now_text(self._clock())))
        return state

    def _save(self) -> None:
        self.state["updated_at"] = _now_text(self._clock())
        write_json(self.state_path, self.state)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self.state.update(changes)
            self._save()
            self._lock.notify_all()

    def _exit_path(self, state: dict[str, Any] | None = None) -> Path:
        return self.evidence / (str((state or self.state).get("operation")) + ".system.exit")

    def _log_path(self, state: dict[str, Any] | None = None) -> Path:
        return self.evidence / (str((state or self.state).get("operation")) + ".system.log")

    def _unit_state(self, state: dict[str, Any]) -> str:
        """"running", "ended" or "unknown" for the install unit of ``state``.

        The unit's cgroup directory is a plain file check that needs no library,
        so it still answers while dpkg replaces the libraries ``systemctl`` uses.
        "ended" needs both: no cgroup and ``systemctl`` saying so.
        """
        unit = state.get("unit")
        if not isinstance(unit, str) or not unit.startswith(UNIT_PREFIX):
            return "ended"
        if (Path(self.paths["cgroups"]) / (unit + ".service")).is_dir():
            return "running"
        code, output = self._run(["systemctl", "is-active", unit + ".service"], 30)
        answer = output.strip().splitlines()[0] if output.strip() else ""
        if code == 0 and answer in ("active", "activating", "reloading", "deactivating"):
            return "running"
        if answer in ("inactive", "failed", "unknown"):
            return "ended"
        return "unknown"

    def _unit_ended(self, state: dict[str, Any]) -> bool:
        """The unit ended, confirmed ENDED_CONFIRMATIONS times, and wrote no exit code."""
        exit_path = self._exit_path(state)
        for attempt in range(ENDED_CONFIRMATIONS):
            if attempt:
                self._sleep(ENDED_INTERVAL)
            if exit_path.exists() or self._unit_state(state) != "ended":
                return False
        return not exit_path.exists()

    def packages_busy(self) -> bool:
        """A system operation runs, or anything holds the dpkg lock (also outside a recorded operation)."""
        return self.busy() or dpkg_busy(self.paths)

    def resume(self) -> None:
        """Continue watching an operation that was running when the updater restarted."""
        phase = self.state["phase"]
        if phase == "installing":
            self._start(self._watch_install)
        elif phase == "verifying":
            self._start(lambda: self._verify("installed"))
        elif phase in ("rebooting", "verifying_restart"):
            self._start(self._watch_reboot)

    def start_background(self) -> None:
        self.resume()
        threading.Thread(target=self._background, daemon=True, name="system-check").start()

    def _start(self, target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True, name="system-operation").start()

    # ------------------------------------------------------------------- check

    def restart_reasons(self) -> list[str]:
        """restart_reasons(), read again only when dpkg's status or the reboot flag changed."""
        key = (_mtime(Path(self.paths["dpkg_status"])), Path(self.paths["reboot_flag"]).exists(), boot_time(self.paths))
        cached = self._restart_cache
        if cached is None or cached[0] != key:
            cached = self._restart_cache = (key, restart_reasons(self.paths))
        return list(cached[1])

    def busy(self) -> bool:
        with self._lock:
            return self.state["phase"] in ACTIVE

    def _check_key(self) -> tuple[Any, ...]:
        return (index_time(self.paths), _mtime(Path(self.paths["dpkg_status"])))

    def recompute(self) -> None:
        """Recompute the cached check from the local lists; keeps the last result when it fails."""
        key = self._check_key()
        try:
            result = compute_check(self._run, self.paths, self._clock)
        except SystemRefused as error:
            with self._lock:
                self.check.update(state="failed", error=str(error), computed_at=self._clock())
            return
        with self._lock:
            self.check.update(state="ready", result=result, error=None, key=key, computed_at=self._clock())

    def _background(self) -> None:
        while not self._stop.wait(5 if self.check["computed_at"] is None else 60):
            if self.busy():
                continue
            stale = (self.check["computed_at"] is None or self._clock() - self.check["computed_at"] > RECHECK_SECONDS)
            if stale or self._check_key() != self.check["key"]:
                self.recompute()

    def manual_check(self) -> dict[str, Any]:
        with self._lock:
            if self.state["phase"] in ACTIVE:
                raise SystemRefused("A system update or restart is in progress")
            if self.check["state"] == "checking":
                return {"accepted": True}
            wait = self._retry_after()
            if wait:
                raise SystemRefused(f"The package lists were refreshed recently; try again in {wait} s")
            self.check.update(state="checking", manual_at=self._clock())
        self._start(self._refresh)
        return {"accepted": True}

    def _retry_after(self) -> int:
        last = self.check["manual_at"]
        return max(0, int(last + MANUAL_CHECK_SECONDS - self._clock())) if last is not None else 0

    def _refresh(self) -> None:
        code, _ = self._run(["apt-get", "update", "-q"], 600)
        if code:
            with self._lock:
                self.check.update(state="failed", error="The package lists could not be refreshed (apt-get update "
                                  "failed). The result below is the last known one.")
            return
        self.recompute()

    def check_view(self) -> dict[str, Any]:
        with self._lock:
            check = copy.deepcopy(self.check)
        result = check["result"] or {}
        restart = self.restart_reasons()
        return {"state": check["state"], "error": check["error"], "retry_after_seconds": self._retry_after(),
                **{key: result.get(key) for key in ("checked_at", "index_updated_at", "index_stale", "upgrades",
                                                    "new", "removals", "security", "packages", "removed",
                                                    "automatic")},
                "restart": {"needed": bool(restart), "reasons": restart}}

    # ---------------------------------------------------------------- requests

    def handle(self, request: dict[str, Any], actor: Callable[[], dict[str, str]]) -> dict[str, Any]:
        operation = request["op"]
        if operation == "system_status":
            return self.status(seen=request.get("seen") is True)
        if operation == "system_check":
            return self.manual_check()
        if operation == "system_review":
            return self.review(request.get("kind"), actor())
        if operation == "system_approve":
            return self.approve(request.get("operation"), request.get("digest"), actor())
        if operation == "system_cancel":
            return self.cancel(request.get("operation"))
        raise SystemRefused("Unknown request")

    def status(self, *, seen: bool = False) -> dict[str, Any]:
        """``seen``: asked for an administrator's page, which shows a pending review; its window starts now."""
        with self._lock:
            review = self.state.get("review")
            if seen and isinstance(review, dict) and review.get("expires_at") is None:
                review["expires_at"] = self._clock() + REVIEW_SECONDS
                self._save()
                self._lock.notify_all()
            state = copy.deepcopy(self.state)
        review = state.get("review")
        if isinstance(review, dict):
            remaining = (REVIEW_SECONDS if review.get("expires_at") is None
                         else max(0, int(review["expires_at"] - self._clock())))
            review = dict(review, expires_in_seconds=remaining)
            review.pop("unseen_until", None)
        return {"api_version": 1, "phase": state["phase"], "kind": state.get("kind"),
                "operation": state.get("operation"), "review": review, "result": state.get("result"),
                "error": state.get("error"), "busy": state["phase"] in ACTIVE,
                "updated_at": state.get("updated_at"), "check": self.check_view()}

    def _refusal(self) -> str | None:
        """Called with the gate held and without the own lock (other_busy takes the image updater's lock)."""
        if self.busy():
            return "A system update or restart is in progress"
        if self.other_busy():
            return "An image update or restore is in progress"
        if not self._deployment_clear():
            return "The deployment has an unfinished or reserved operation; resolve it first"
        if operator_busy(Path(self.paths["operator_lock"])):
            return "Another operator (for example an SSH deployment) is active"
        if dpkg_busy(self.paths):
            return "Packages are being installed right now (for example the automatic security updates)"
        return None

    def review(self, kind: object, actor: dict[str, str]) -> dict[str, Any]:
        if kind not in ("install", "restart"):
            raise SystemRefused("Unknown system action")
        with self.gate:
            refusal = self._refusal()
            if refusal:
                raise SystemRefused(refusal)
            if kind == "restart" and not self.restart_reasons():
                raise SystemRefused("No restart is needed")
            operation = str(uuid4())
            with self._lock:
                self.state = {"phase": "preparing", "kind": kind, "operation": operation, "actor": actor,
                              "started_at": _now_text(self._clock()), "review": None, "result": None,
                              "error": None}
                self._save()
        self._start(lambda: self._prepare(operation, kind))
        return {"accepted": True, "operation": operation}

    def _prepare(self, operation: str, kind: str) -> None:
        if kind == "install":
            try:
                plan = compute_check(self._run, self.paths, self._clock)
            except SystemRefused as error:
                self._set(phase="failed_before_install", error=str(error) + " Nothing was installed.")
                return
            with self._lock:
                self.check.update(state="ready", result=plan, error=None, key=self._check_key(),
                                  computed_at=self._clock())
            if not plan["upgrades"] and not plan["new"] and not plan["removals"]:
                self._set(phase="up_to_date", error=None)
                return
            summary = self._install_summary(plan)
            digest = plan["digest"]
        else:
            reasons = self.restart_reasons()
            summary = {"action": "restart", "reasons": reasons, "interruption": RESTART_INTERRUPTION}
            digest = "sha256:" + hashlib.sha256(f"{operation}/{boot_id(self.paths)}".encode()).hexdigest()
        if not self._await_approval(operation, digest, summary):
            return
        if kind == "install":
            self._install(operation, digest)
        else:
            self._restart(operation)

    def _install_summary(self, plan: dict[str, Any]) -> dict[str, Any]:
        names = {item["name"].split(":")[0] for item in plan["packages"]}
        restart = sorted(name for name in names if needs_restart(name))
        return {
            "action": "install", "upgrades": plan["upgrades"], "new": plan["new"], "removals": plan["removals"],
            "removed": plan["removed"][:10], "security": plan["security"],
            "index_updated_at": plan["index_updated_at"], "index_stale": plan["index_stale"],
            "docker": bool(names & DOCKER_NAMES),
            "restart_after": ("A restart is needed afterwards (" + ", ".join(restart[:4])
                              + (" …" if len(restart) > 4 else "") + ").") if restart else None,
            "interruption": INSTALL_INTERRUPTION, "undo": NO_UNDO, "config": CONFIG_NOTE,
        }

    def _review_deadline(self, review: dict[str, Any]) -> float:
        """Called with the own lock held: the confirmation deadline, or the unseen deadline before it was shown."""
        return review["expires_at"] if review.get("expires_at") is not None else review["unseen_until"]

    def _await_approval(self, operation: str, digest: str, summary: dict[str, Any]) -> bool:
        with self._lock:
            self._decision = None
            review = {"operation": operation, "digest": digest, "expires_at": None,
                      "unseen_until": self._clock() + UNSEEN_REVIEW_SECONDS, "summary": summary}
            self.state.update(phase="awaiting_approval", review=review)
            self._save()
            self._lock.notify_all()
            while self._decision is None and self._clock() < self._review_deadline(review):
                self._lock.wait(timeout=min(1.0, max(0.0, self._review_deadline(review) - self._clock())))
            decision = self._decision or {"approved": False, "reason": "expired"}
            if not decision["approved"]:
                self.state.update(phase="cancelled", review=None,
                                  error="The review expired." if decision["reason"] == "expired" else "Cancelled.")
                self._save()
                return False
            self.state.update(review=None, approved_by=decision["actor"], approved_at=_now_text(self._clock()))
            self._save()
            return True

    def approve(self, operation: object, digest: object, actor: dict[str, str]) -> dict[str, Any]:
        with self._lock:
            review = self.state.get("review")
            if (self.state["phase"] != "awaiting_approval" or not isinstance(review, dict)
                    or operation != review["operation"]):
                raise SystemRefused("There is no pending review for this operation")
            if digest != review["digest"]:
                raise SystemRefused("The confirmation does not match the pending review")
            if self._clock() >= self._review_deadline(review):
                raise SystemRefused("The review expired; start a new one")
            self._decision = {"approved": True, "actor": actor}
            self._lock.notify_all()
        return {"accepted": True, "operation": operation}

    def cancel(self, operation: object) -> dict[str, Any]:
        with self._lock:
            review = self.state.get("review")
            if not isinstance(review, dict) or operation != review["operation"]:
                raise SystemRefused("There is no pending review for this operation")
            self._decision = {"approved": False, "reason": "cancelled"}
            self._lock.notify_all()
        return {"accepted": True, "operation": operation}

    # ----------------------------------------------------------------- install

    def _install(self, operation: str, digest: str) -> None:
        refusal = ("An image update or restore is in progress" if self.other_busy()
                   else "The deployment has an unfinished operation" if not self._deployment_clear()
                   else "Packages are being installed right now" if dpkg_busy(self.paths) else None)
        if refusal:
            self._set(phase="failed_before_install", error=refusal + ". Nothing was installed.")
            return
        try:
            again = compute_check(self._run, self.paths, self._clock)
            before = services_snapshot(self._run)
        except SystemRefused as error:
            self._set(phase="failed_before_install", error=str(error) + " Nothing was installed.")
            return
        if again["digest"] != digest:
            self._set(phase="cancelled", error="The pending updates changed since the review; review them again.")
            return
        unit = UNIT_PREFIX + operation[:8]
        lock = Path(self.paths["operator_lock"])
        if operator_busy(lock):
            self._set(phase="failed_before_install", error="Another operator is active. Nothing was installed.")
            return
        exit_path, log_path = self.evidence / (operation + ".system.exit"), self.evidence / (operation + ".system.log")
        # Fixed arguments only; the operation is a UUID generated here.
        script = (f"flock -n -E {CONFLICT_EXIT} {lock} apt-get -y -q -o Dpkg::Options::=--force-confdef "
                  f"-o Dpkg::Options::=--force-confold full-upgrade; code=$?; "
                  f"echo $code > {exit_path}.tmp && mv {exit_path}.tmp {exit_path}")
        command = ["systemd-run", "--unit", unit, "--description", "MPDRussoundPi system update",
                   "--property", f"StandardOutput=append:{log_path}", "--property", f"StandardError=append:{log_path}",
                   "--setenv", "DEBIAN_FRONTEND=noninteractive", "--setenv", "APT_LISTCHANGES_FRONTEND=none",
                   "--setenv", "LC_ALL=C", "--setenv", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                   "/bin/sh", "-c", script]
        # Dispatch boundary: recorded before the unit may change anything.
        self._set(phase="installing", unit=unit, before=before, started_install_at=_now_text(self._clock()))
        code, output = self._run(command, 60)
        if code:
            logger.warning("systemd-run failed: %s", output[-500:])
            self._set(phase="failed_before_install", error="The installation could not be started. Nothing was installed.")
            return
        self._watch_install()

    def _watch_install(self) -> None:
        exit_path = self._exit_path()
        while not exit_path.exists():
            if self._unit_ended(self.state):
                self._set(phase="outcome_unknown", error=(
                    "The installation ended without recording its result. Check the Pi with "
                    "'sudo dpkg --audit' over SSH before installing again."))
                return
            self._sleep(2)
        try:
            code = int(exit_path.read_text().strip())
            # When apt ended, also if the updater only noticed it later (after its own restart).
            ended = _now_text(exit_path.stat().st_mtime)
        except (OSError, ValueError):
            code, ended = -1, _now_text(self._clock())
        kept = self._kept_configs()
        if code == CONFLICT_EXIT:
            self._set(phase="failed_before_install", error="Another operator was active. Nothing was installed.")
        elif code != 0:
            _, audit = self._run(["dpkg", "--audit"], 120)
            if audit.strip():
                self._set(phase="outcome_unknown", result={"kept_configs": kept, "finished_at": ended}, error=(
                    "The installation stopped part way and some packages are not fully set up. Finish it over SSH "
                    "with 'sudo dpkg --configure -a', then check again."))
            else:
                self._set(phase="install_failed", result={"kept_configs": kept, "finished_at": ended}, error=(
                    "The installation failed; the packages that were already updated stay installed. "
                    "The details are in the updater's log on the Pi."))
        else:
            self._set(phase="verifying", result={"kept_configs": kept, "finished_at": ended})
            self._verify("installed")
        self.recompute()

    def _kept_configs(self) -> list[str]:
        try:
            text = self._log_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return sorted({match.group(1) for match in map(CONFFILE.match, text.splitlines()) if match})[:20]

    def _verify(self, success: str) -> None:
        before = self.state.get("before") or {}
        deadline = self._clock() + VERIFY_SECONDS
        missing = missing_services(before, self._run)
        while missing and self._clock() < deadline:
            self._sleep(5)
            missing = missing_services(before, self._run)
        previous = self.state.get("result") or {}
        result = dict(previous, finished_at=previous.get("finished_at") or _now_text(self._clock()),
                      checks="passed" if not missing else "failed", missing=missing[:10],
                      restart_needed=bool(self.restart_reasons()))
        if missing:
            self._set(phase="checks_failed", result=result,
                      error="These did not come back: " + ", ".join(missing[:5]) + ".")
        else:
            self._set(phase=success, result=result, error=None)

    # ----------------------------------------------------------------- restart

    def _restart(self, operation: str) -> None:
        refusal = ("An image update or restore is in progress" if self.other_busy()
                   else "Packages are being installed right now" if dpkg_busy(self.paths) else None)
        if refusal:
            self._set(phase="restart_failed", error=refusal + ". The Pi was not restarted.")
            return
        try:
            before = services_snapshot(self._run)
        except SystemRefused as error:
            self._set(phase="restart_failed", error=str(error) + " The Pi was not restarted.")
            return
        self._set(phase="rebooting", before=before, boot_id=boot_id(self.paths),
                  reboot_requested_at=self._clock())
        self._sleep(2)  # let the confirmation reach the page first
        code, output = self._reboot()
        if code:
            logger.warning("systemctl reboot failed: %s", output[-300:])
            self._set(phase="restart_failed", error="The restart could not be started.")

    def _watch_reboot(self) -> None:
        recorded = self.state.get("boot_id")
        requested = self.state.get("reboot_requested_at") or self._clock()
        while boot_id(self.paths) == recorded:
            if self._clock() - requested > REBOOT_SECONDS:
                self._set(phase="restart_failed", error="The Pi did not restart.")
                return
            self._sleep(5)
        self._set(phase="verifying_restart")
        self._verify("restarted")
