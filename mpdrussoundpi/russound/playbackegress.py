"""Install the MPD-only packet filter, or gate MPD startup on its availability."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time

from lib.publicnetwork import blocked_networks


SERVICE = "mpdrussoundpi-egress.service"
TABLE = "mpdrussoundpi_egress"
STATE_FILE = Path("/run/mpdrussoundpi-egress/scope.json")


def cgroup_path(uid: int) -> Path:
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/"
                f"user@{uid}.service/mpdstreams.slice")


def verify_scope(uid: int) -> None:
    state = json.loads(STATE_FILE.read_text())
    if state != {"uid": uid, "cgroup_inode": cgroup_path(uid).stat().st_ino}:
        raise RuntimeError("MPD cgroup changed; reload mpdrussoundpi-egress before starting MPD.")


def ruleset(uid: int, resolvers: list[str]) -> str:
    if uid < 1:
        raise ValueError("A non-root playback user UID is required.")
    path = f"user.slice/user-{uid}.slice/user@{uid}.service/mpdstreams.slice"
    lines = [
        f"add table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
    ]
    for version in (4, 6):
        elements = ", ".join(str(network) for network in blocked_networks(version))
        lines.extend([
            f"  set blocked{version} {{",
            f"    type ipv{version}_addr; flags interval;",
            f"    elements = {{ {elements} }}",
            "  }",
        ])
    lines.extend([
        "  chain streams {",
        "    ct direction reply return",
    ])
    for resolver in resolvers:
        address = ipaddress.ip_address(resolver)
        family = "ip" if address.version == 4 else "ip6"
        lines.append(f"    {family} daddr {address} udp dport 53 counter return")
    lines.extend([
        "    ip daddr @blocked4 counter reject",
        "    ip6 daddr @blocked6 counter reject",
        "  }",
        "  chain output {",
        "    type filter hook output priority -5; policy accept;",
        f'    socket cgroupv2 level 4 "{path}" jump streams',
        "  }",
        "}",
        "",
    ])
    return "\n".join(lines)


def wait_for_filter() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", SERVICE], check=False,
        )
        if result.returncode == 0:
            verify_scope(os.getuid())
            return
        time.sleep(0.5)
    raise RuntimeError("MPD egress enforcement is unavailable; refusing to start MPD.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--user")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--resolver", action="append", default=[])
    args = parser.parse_args()
    if args.wait:
        wait_for_filter()
        return
    if not args.user or not re.fullmatch(r"[a-z_][a-z0-9_-]*", args.user) or args.uid is None:
        parser.error("--user and --uid are required to install the filter")
    rules = ruleset(args.uid, args.resolver)
    subprocess.run([
        "/usr/bin/systemctl", "--user", f"--machine={args.user}@.host",
        "start", "mpdstreams.slice",
    ], check=True)
    cgroup = cgroup_path(args.uid)
    if not cgroup.is_dir():
        raise RuntimeError("MPD stream cgroup is absent; refusing to install an unscoped filter.")
    subprocess.run(["/usr/sbin/nft", "--check", "--file", "-"], input=rules, text=True, check=True)
    subprocess.run(["/usr/sbin/nft", "--file", "-"], input=rules, text=True, check=True)
    STATE_FILE.parent.mkdir(mode=0o755, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".new")
    temporary.write_text(json.dumps({"uid": args.uid, "cgroup_inode": cgroup.stat().st_ino}))
    temporary.chmod(0o644)
    temporary.replace(STATE_FILE)


if __name__ == "__main__":
    main()
