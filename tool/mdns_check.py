#!/usr/bin/env python3
"""Keep the Pi's mDNS name answering: restart Avahi when it stopped, renamed itself or went silent.

Runs from mpdrussoundpi-mdns-check.timer (ansible/playbooks/deploy-mdns.yml).
See docs/documentation/mdns-hostname.md. Standard library only; talks to Avahi over D-Bus via busctl.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence

UNIT = "avahi-daemon.service"
AVAHI = ["org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server"]
# A restart within this many seconds is left alone, so a lasting fault cannot become a restart loop.
SETTLE_SECONDS = 300

Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess]


def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "timeout")


def busctl(runner: Runner, method: str, signature: str = "", *args: str, timeout: float = 6) -> list | None:
    argv = ["busctl", "--system", f"--timeout={int(timeout)}", "--json=short", "--", "call", *AVAHI, method]
    if signature:
        argv += [signature, *args]
    result = runner(argv, timeout + 2)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)["data"]
    except (ValueError, KeyError, TypeError):
        return None


def seconds_since_start(runner: Runner) -> float | None:
    result = runner(["systemctl", "show", UNIT, "-p", "ActiveEnterTimestampMonotonic", "--value"], 10)
    try:
        started = int(result.stdout.strip()) / 1_000_000
        with open("/proc/uptime", encoding="ascii") as uptime:
            now = float(uptime.read().split()[0])
    except (ValueError, OSError):
        return None
    return now - started if started > 0 else None


def diagnose(runner: Runner, name: str) -> str | None:
    """None when Avahi is healthy, otherwise why it needs a restart."""
    state = runner(["systemctl", "is-active", UNIT], 10).stdout.strip()
    if state != "active":
        return f"avahi-daemon is {state or 'unknown'}"
    fqdn = busctl(runner, "GetHostNameFqdn")
    if not fqdn:
        return "avahi-daemon does not answer on D-Bus"
    if fqdn[0] != name:
        return f"avahi-daemon publishes {fqdn[0]} instead of {name}"
    # interface -1 (any), protocol 0 (IPv4), address protocol 0 (IPv4), no flags.
    answer = busctl(runner, "ResolveHostName", "iisiu", "-1", "0", name, "0", "0")
    if not answer or len(answer) < 5 or not answer[4]:
        return f"avahi-daemon does not resolve {name}"
    return None


def check(runner: Runner, name: str, settle: float = SETTLE_SECONDS) -> dict:
    reason = diagnose(runner, name)
    if reason is None:
        return {"ok": True, "name": name}
    since = seconds_since_start(runner)
    if since is not None and since < settle:
        return {"ok": False, "name": name, "reason": reason, "action": "none",
                "detail": f"started {int(since)}s ago; waiting before another restart"}
    restarted = runner(["systemctl", "restart", UNIT], 60).returncode == 0
    return {"ok": False, "name": name, "reason": reason, "action": "restart" if restarted else "restart-failed"}


def main(argv: Sequence[str] | None = None, runner: Runner = run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default=f"{socket.gethostname()}.local", help="expected mDNS name")
    parser.add_argument("--settle", type=float, default=SETTLE_SECONDS)
    options = parser.parse_args(argv)
    outcome = check(runner, options.name, options.settle)
    print(json.dumps(outcome, sort_keys=True), flush=True)
    return 1 if outcome.get("action") == "restart-failed" else 0


if __name__ == "__main__":
    sys.exit(main())
