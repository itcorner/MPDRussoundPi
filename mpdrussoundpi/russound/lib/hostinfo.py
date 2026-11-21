"""Read-only facts about the host the agent runs on, for ``GET /v1/host``.

Every section is a *probe object* in the same shape as the playback and audio
probes of the read contract, so clients treat age and staleness the same way.

Disclosure (see the deployment overview plan): operational facts and the
addresses of the uplink interface only. Never read or report MAC addresses,
serial number, machine id, boot id, host name, the Wi-Fi network name, its
channel, the gateway or DNS servers. No probe triggers a Wi-Fi scan, writes
anything or takes the playback mutation lock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any

SCHEMA_SECTIONS = ("system", "cpu", "memory", "temperature", "power", "disk", "network", "clock")
# Seconds between collections, per section. Cheap file reads are frequent;
# probes that start a process are not.
INTERVALS = {
    "system": 10.0, "cpu": 5.0, "memory": 5.0, "temperature": 5.0, "disk": 30.0,
    "network": 10.0, "power": 30.0, "clock": 30.0,
}
STALE_FACTOR = 3
COMMAND_TIMEOUT_SECONDS = 5.0
MAX_ADDRESSES = 8
INTERFACE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,15}\Z")
THROTTLE_FLAGS = (
    ("under_voltage", 0), ("frequency_capped", 1), ("throttled", 2), ("soft_temperature_limit", 3),
)
LOGGER = logging.getLogger("russound.hostinfo")

Runner = Callable[[list[str]], str]


class Unsupported(Exception):
    """The host has no such source (a missing sensor or tool), which is not an error."""


def default_runner(argv: list[str]) -> str:
    """Run one fixed command without a shell; raise on any failure."""
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=True,
        env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    return result.stdout


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: str, limit: int = 64) -> str | None:
    """Printable ASCII only, bounded; hardware strings are not trusted."""
    cleaned = "".join(character for character in value if 32 <= ord(character) < 127).strip()
    return cleaned[:limit] or None


# ---------------------------------------------------------------- parsers

def parse_loadavg(text: str) -> tuple[float, float, float]:
    fields = text.split()
    return float(fields[0]), float(fields[1]), float(fields[2])


def parse_meminfo(text: str) -> dict[str, int]:
    """Bytes for the four values the page needs."""
    kilobytes: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            kilobytes[name] = int(rest.split()[0]) * 1024
    if "MemTotal" not in kilobytes or "MemAvailable" not in kilobytes:
        raise ValueError("meminfo incomplete")
    kilobytes.setdefault("SwapTotal", 0)
    kilobytes.setdefault("SwapFree", 0)
    return kilobytes


def parse_cpu_times(text: str) -> tuple[int, int]:
    """(busy, total) jiffies from the aggregate ``cpu`` line of ``/proc/stat``."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            values = [int(field) for field in line.split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[:8])
            return total - idle, total
    raise ValueError("no cpu line")


def cpu_busy_percent(previous: tuple[int, int] | None, current: tuple[int, int]) -> float | None:
    if previous is None:
        return None
    busy = current[0] - previous[0]
    total = current[1] - previous[1]
    if total <= 0 or busy < 0:
        return None
    return round(min(100.0, 100.0 * busy / total), 1)


def parse_throttled(text: str) -> dict[str, Any]:
    """Decode ``vcgencmd get_throttled`` (for example ``throttled=0x50005``)."""
    match = re.search(r"throttled=0x([0-9a-fA-F]{1,8})\b", text)
    if match is None:
        raise ValueError("unexpected get_throttled output")
    raw = int(match.group(1), 16)
    return {
        "raw": f"0x{raw:x}",
        "now": {name: bool(raw >> bit & 1) for name, bit in THROTTLE_FLAGS},
        "since_boot": {name: bool(raw >> (bit + 16) & 1) for name, bit in THROTTLE_FLAGS},
    }


def parse_default_interface(route_text: str, ipv6_route_text: str = "") -> str | None:
    """Interface of the default route (lowest metric), IPv4 first, then IPv6."""
    best: tuple[int, str] | None = None
    for line in route_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == "00000000" and int(fields[3], 16) & 0x3 == 0x3:
            candidate = (int(fields[6]), fields[0])
            best = candidate if best is None or candidate < best else best
    if best is not None:
        return best[1]
    for line in ipv6_route_text.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[0] == "0" * 32 and fields[1] == "00" and int(fields[8], 16) & 0x1:
            candidate = (int(fields[5], 16), fields[9])
            best = candidate if best is None or candidate < best else best
    return best[1] if best is not None else None


def parse_wireless_dbm(text: str, interface: str) -> int | None:
    """Signal level in dBm for ``interface`` from ``/proc/net/wireless``."""
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == interface:
            fields = rest.split()
            if len(fields) >= 3:
                level = float(fields[2].rstrip("."))
                return int(level) if -127 <= level <= 0 else None
    return None


def parse_link(text: str) -> tuple[str | None, float | None]:
    """Band and link rate of the connected access point from terse ``nmcli`` output.

    Only the ``IN-USE``, ``FREQ`` and ``RATE`` columns are ever requested.
    """
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[0] == "*":
            band = rate = None
            frequency = re.match(r"(\d{4,5})\b", fields[1])
            if frequency is not None:
                mhz = int(frequency.group(1))
                band = "2.4 GHz" if mhz < 3000 else "5 GHz" if mhz < 5925 else "6 GHz"
            speed = re.match(r"(\d+(?:\.\d+)?)\b", fields[2])
            if speed is not None:
                rate = float(speed.group(1))
            return band, rate
    return None, None


def parse_addresses(text: str) -> list[dict[str, Any]]:
    """Global IPv4 and IPv6 addresses from ``ip -j addr show dev <interface>``."""
    addresses: list[dict[str, Any]] = []
    for entry in json.loads(text):
        for info in entry.get("addr_info", []):
            family = {"inet": 4, "inet6": 6}.get(info.get("family"))
            if family is None or info.get("scope") != "global":
                continue
            addresses.append({"family": family, "address": str(info["local"]), "prefix": int(info["prefixlen"])})
    return addresses[:MAX_ADDRESSES]


# -------------------------------------------------------------- collector

class HostCollector:
    """Background collector with cached, immutable-per-read snapshots."""

    def __init__(
        self, *, root: str | Path = "/", run: Runner = default_runner,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
        wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._root = Path(root)
        self._run = run
        self._statvfs = statvfs
        self._wall = wall
        self._monotonic = monotonic
        self._which = which
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}
        self._due = {name: 0.0 for name in SCHEMA_SECTIONS}
        self._cpu_previous: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sources: dict[str, Callable[[], dict[str, Any]]] = {
            "system": self._system, "cpu": self._cpu, "memory": self._memory,
            "temperature": self._temperature, "power": self._power, "disk": self._disk,
            "network": self._network, "clock": self._clock,
        }

    # -- lifecycle
    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("Host collector cannot be restarted")
        self._thread = threading.Thread(target=self._run_loop, name="host-collector", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=COMMAND_TIMEOUT_SECONDS * 2)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_due()
            self._stop.wait(1.0)

    # -- collection
    def refresh_due(self) -> None:
        now = self._monotonic()
        for name in SCHEMA_SECTIONS:
            if now >= self._due[name]:
                self.refresh(name)

    def refresh(self, name: str) -> None:
        """Collect one section now; a failure never escapes and never leaks details."""
        attempted_wall, attempted_mono = self._wall(), self._monotonic()
        status: str
        value: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        try:
            value = self._sources[name]()
            status = "ok"
        except Unsupported:
            status = "unsupported"
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the others
            LOGGER.debug("Host probe %s failed: %s", name, type(exc).__name__)
            status = "error"
            error = {"code": "probe_failed", "message": "The host probe failed.", "retryable": True}
        with self._lock:
            previous = self._state.get(name, {})
            entry: dict[str, Any] = {
                "status": status, "attempted_wall": attempted_wall, "value": value, "error": error,
                "observed_wall": previous.get("observed_wall"), "observed_mono": previous.get("observed_mono"),
            }
            if status == "ok":
                entry["observed_wall"], entry["observed_mono"] = attempted_wall, attempted_mono
            self._state[name] = entry
        self._due[name] = attempted_mono + INTERVALS[name]

    def snapshot(self) -> dict[str, Any]:
        """Probe objects for every section; readers never trigger collection."""
        now_mono, now_wall = self._monotonic(), self._wall()
        result: dict[str, Any] = {"snapshot_at": _iso(now_wall)}
        with self._lock:
            states = {name: dict(self._state[name]) for name in self._state}
        for name in SCHEMA_SECTIONS:
            entry = states.get(name)
            if entry is None:
                result[name] = {
                    "status": "pending", "attempted_at": None, "observed_at": None, "age_ms": None,
                    "stale": True, "error": None, "value": None, "last_known": None,
                }
                continue
            status = entry["status"]
            fresh = status == "ok"
            age_ms = int((now_mono - entry["observed_mono"]) * 1000) if fresh else None
            result[name] = {
                "status": status,
                "attempted_at": _iso(entry["attempted_wall"]),
                "observed_at": _iso(entry["observed_wall"]) if fresh else None,
                "age_ms": age_ms,
                "stale": not fresh or age_ms > STALE_FACTOR * INTERVALS[name] * 1000,
                "error": entry["error"],
                "value": entry["value"] if fresh else None,
                "last_known": None,
            }
        return result

    # -- sources
    def _read(self, path: str) -> str:
        try:
            return (self._root / path.lstrip("/")).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise Unsupported(path) from None

    def _optional(self, path: str) -> str | None:
        try:
            return self._read(path)
        except (Unsupported, OSError):
            return None

    def _cores(self) -> int | None:
        text = self._optional("/proc/cpuinfo")
        count = sum(1 for line in (text or "").splitlines() if line.startswith("processor"))
        return count or os.cpu_count()

    def _system(self) -> dict[str, Any]:
        model = self._optional("/proc/device-tree/model") or self._optional("/sys/class/dmi/id/product_name")
        release = self._optional("/etc/os-release") or ""
        pretty = next((line.split("=", 1)[1].strip().strip('"') for line in release.splitlines()
                       if line.startswith("PRETTY_NAME=")), None)
        kernel = self._optional("/proc/sys/kernel/osrelease")
        return {
            "model": _text(model.replace("\x00", "")) if model else None,
            "os": _text(pretty) if pretty else None,
            "kernel": _text(kernel) if kernel else None,
            "uptime_seconds": int(float(self._read("/proc/uptime").split()[0])),
            "cores": self._cores(),
        }

    def _cpu(self) -> dict[str, Any]:
        load = parse_loadavg(self._read("/proc/loadavg"))
        current = parse_cpu_times(self._read("/proc/stat"))
        busy = cpu_busy_percent(self._cpu_previous, current)
        self._cpu_previous = current
        frequency = self._optional("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        return {
            "cores": self._cores(), "load": [round(value, 2) for value in load], "busy_percent": busy,
            "frequency_mhz": int(frequency.strip()) // 1000 if frequency and frequency.strip().isdigit() else None,
        }

    def _memory(self) -> dict[str, Any]:
        values = parse_meminfo(self._read("/proc/meminfo"))
        return {
            "total_bytes": values["MemTotal"], "available_bytes": values["MemAvailable"],
            "swap_total_bytes": values["SwapTotal"], "swap_used_bytes": values["SwapTotal"] - values["SwapFree"],
        }

    def _temperature(self) -> dict[str, Any]:
        text = self._read("/sys/class/thermal/thermal_zone0/temp")
        return {"celsius": round(int(text.strip()) / 1000, 1)}

    def _disk(self) -> dict[str, Any]:
        usage = self._statvfs(str(self._root))
        return {"total_bytes": usage.f_frsize * usage.f_blocks, "free_bytes": usage.f_frsize * usage.f_bavail}

    def _power(self) -> dict[str, Any]:
        if self._which("vcgencmd") is None:
            raise Unsupported("vcgencmd")
        return parse_throttled(self._run(["vcgencmd", "get_throttled"]))

    def _clock(self) -> dict[str, Any]:
        if self._which("timedatectl") is None:
            raise Unsupported("timedatectl")
        answer = self._run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
        if answer not in ("yes", "no"):
            raise ValueError("unexpected timedatectl output")
        return {"synchronized": answer == "yes"}

    def _network(self) -> dict[str, Any]:
        interface = parse_default_interface(
            self._optional("/proc/net/route") or "", self._optional("/proc/net/ipv6_route") or "",
        )
        if interface is None:
            return {"state": "no_uplink", "interface_type": None, "addresses": [], "wifi": None}
        if INTERFACE_PATTERN.fullmatch(interface) is None:
            raise ValueError("unexpected interface name")
        base = f"/sys/class/net/{interface}"
        sysfs = self._root / base.lstrip("/")
        if (sysfs / "wireless").is_dir() or (sysfs / "phy80211").exists():
            kind = "wifi"
        elif (sysfs / "device").exists() and not (sysfs / "bridge").exists():
            kind = "ethernet"
        else:
            kind = "other"
        operstate = (self._optional(f"{base}/operstate") or "unknown").strip()
        try:
            addresses: list[dict[str, Any]] | None = parse_addresses(self._run(["ip", "-j", "addr", "show", "dev", interface]))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            addresses = None
        wifi: dict[str, Any] | None = None
        if kind == "wifi":
            dbm = parse_wireless_dbm(self._optional("/proc/net/wireless") or "", interface)
            band = rate = None
            if self._which("nmcli") is not None:
                try:
                    # Current link only: never rescan (a scan interrupts the radio and stutters streams)
                    # and never request the network name.
                    band, rate = parse_link(self._run([
                        "nmcli", "-t", "-f", "IN-USE,FREQ,RATE", "device", "wifi", "list",
                        "ifname", interface, "--rescan", "no",
                    ]))
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
            wifi = {"signal_dbm": dbm, "link_rate_mbps": rate, "band": band}
        return {
            "state": "connected" if operstate in ("up", "unknown") else "disconnected",
            "interface_type": kind, "addresses": addresses, "wifi": wifi,
        }
