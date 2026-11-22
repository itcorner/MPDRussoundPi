"""Local certificate authority and server certificate for the web UI's HTTPS proxy.

See docs/plans/general_open_web-ui-https.md. Standard library plus the ``openssl``
command only: Ansible copies this file to the Pi, where it runs with the system
Python, independent of the deployment tooling.

    python3 web_tls.py ensure --dir /etc/mpdrussoundpi-web-tls --dns musicpi.local --ip 192.168.1.105 ...
    python3 web_tls.py status --dir /etc/mpdrussoundpi-web-tls

``ensure`` creates the CA once and never replaces it. It issues a new server
certificate (with a new key) when none exists, when its names differ from the
requested ones, or when it expires within ``--renew-days``. It always refreshes
the public download directory. It prints a JSON report and exits 0 on success,
also when nothing changed.

Layout below ``--dir``::

    ca/ca.key        root:root 0400  - never leaves the Pi
    ca/ca.crt        public root certificate
    server.key       root:<key group> 0640
    server.crt       server certificate (leaf only)
    public/          what the HTTP listener serves at /ca, plus status.json for the Status page
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import grp
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import uuid

CA_DAYS = 3650
SERVER_DAYS = 397  # under Apple's 825-day limit for user-installed roots
RENEW_DAYS = 60
PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
PUBLIC_NAME = "mpdrussoundpi-ca"


class TlsError(RuntimeError):
    """Nothing usable was produced; existing files are unchanged."""


def openssl(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(["openssl", *args], input=input_text, capture_output=True, text=True, timeout=60)
    if result.returncode:
        lines = (result.stderr or "").strip().splitlines()
        raise TlsError(f"openssl {args[0]} failed: {lines[-1] if lines else result.returncode}")
    return result.stdout


def _ok(*args: str) -> bool:
    return subprocess.run(["openssl", *args], capture_output=True, timeout=60).returncode == 0


# ------------------------------------------------------------------ validation

def dns_name(value: str) -> str:
    if len(value) > 253 or not all(DNS_LABEL.fullmatch(label) for label in value.split(".")) \
            or re.fullmatch(r"[0-9.]+", value):
        raise TlsError(f"Not a DNS name: {value!r}")
    return value.lower()


def ip_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise TlsError(f"Not an IP address: {value!r}") from None


def ip_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        return ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise TlsError(f"Not a network in CIDR form: {value!r}") from None


def dns_permitted(name: str, subtree: str) -> bool:
    """RFC 5280 DNS subtree: the name itself or any name below it."""
    return name == subtree or name.endswith("." + subtree)


def check_within(dns: list[str], ips: list[str], permit_dns: list[str], permit_ip: list[str]) -> None:
    """Refuse to issue what the CA's name constraints would make every client reject."""
    for name in dns:
        if not any(dns_permitted(name, subtree) for subtree in permit_dns):
            raise TlsError(f"{name} is outside the CA's permitted names {permit_dns}")
    networks = [ip_network(item) for item in permit_ip]
    for address in ips:
        if not any(ipaddress.ip_address(address) in network for network in networks):
            raise TlsError(f"{address} is outside the CA's permitted ranges {permit_ip}")


# ------------------------------------------------------------------ files

def _write(path: Path, data: bytes | str, mode: int, gid: int | None = None) -> None:
    """Atomic replace, with the final mode and group set before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        os.fchmod(fd, mode)
        if gid is not None:
            os.fchown(fd, -1, gid)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data.encode() if isinstance(data, str) else data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _new_key() -> str:
    return openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256")


# ------------------------------------------------------------------ certificate facts

def sha256_fingerprint(pem_path: Path) -> str:
    out = openssl("x509", "-in", str(pem_path), "-noout", "-fingerprint", "-sha256")
    return out.strip().split("=", 1)[1]


def not_after(pem_path: Path) -> str:
    raw = openssl("x509", "-in", str(pem_path), "-noout", "-enddate").strip().split("=", 1)[1]
    moment = datetime.strptime(raw.replace("  ", " "), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def san(pem_path: Path) -> tuple[set[str], set[str]]:
    out = openssl("x509", "-in", str(pem_path), "-noout", "-ext", "subjectAltName")
    dns = {item[4:].lower() for item in re.findall(r"DNS:[^,\s]+", out)}
    ips = {str(ipaddress.ip_address(item[11:])) for item in re.findall(r"IP Address:[0-9A-Fa-f:.]+", out)}
    return dns, ips


def key_matches(cert: Path, key: Path) -> bool:
    return openssl("x509", "-in", str(cert), "-noout", "-pubkey") == openssl("pkey", "-in", str(key), "-pubout")


# ------------------------------------------------------------------ issuing

def create_ca(directory: Path, common_name: str, permit_dns: list[str], permit_ip: list[str]) -> None:
    constraints = ",".join(
        [f"permitted;DNS:{name}" for name in permit_dns]
        + [f"permitted;IP:{net.network_address}/{net.netmask}" for net in map(ip_network, permit_ip)])
    key = _new_key()
    with tempfile.TemporaryDirectory() as scratch:
        key_path = Path(scratch) / "ca.key"
        _write(key_path, key, 0o400)
        cert = openssl(
            "req", "-x509", "-new", "-key", str(key_path), "-sha256", "-days", str(CA_DAYS),
            "-subj", "/CN=" + common_name, "-set_serial", "0x" + secrets.token_hex(16),
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
            "-addext", "nameConstraints=critical," + constraints)
    ca = directory / "ca"
    ca.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ca, 0o700)
    # The key first: a certificate without its key would block every later run.
    _write(ca / "ca.key", key, 0o400)
    _write(ca / "ca.crt", cert, 0o644)


def issue_server(directory: Path, dns: list[str], ips: list[str], days: int, gid: int | None) -> None:
    ca = directory / "ca"
    key = _new_key()
    names = ",".join([f"DNS:{name}" for name in dns] + [f"IP:{address}" for address in ips])
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        key_path, csr_path, ext_path = scratch_path / "server.key", scratch_path / "server.csr", scratch_path / "ext"
        _write(key_path, key, 0o400)
        # A CN that cannot be read as a host name, so verifiers never check it against the constraints.
        _write(csr_path, openssl("req", "-new", "-key", str(key_path), "-subj", "/CN=MPDRussoundPi web UI"), 0o600)
        _write(ext_path, "\n".join([
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature",
            "extendedKeyUsage=serverAuth",
            "subjectKeyIdentifier=hash",
            "authorityKeyIdentifier=keyid",
            "subjectAltName=" + names, ""]), 0o600)
        cert = openssl("x509", "-req", "-in", str(csr_path), "-CA", str(ca / "ca.crt"), "-CAkey", str(ca / "ca.key"),
                       "-sha256", "-days", str(days), "-set_serial", "0x" + secrets.token_hex(16),
                       "-extfile", str(ext_path))
        cert_path = scratch_path / "server.crt"
        _write(cert_path, cert, 0o644)
        # Verify before anything is replaced: the chain, the constraints and the key.
        if not _ok("verify", "-x509_strict", "-purpose", "sslserver", "-CAfile", str(ca / "ca.crt"), str(cert_path)):
            raise TlsError("The new server certificate does not verify against the CA (outside its name constraints?)")
        if not key_matches(cert_path, key_path):
            raise TlsError("The new server certificate does not match its key")
    # Key before certificate; Caddy only reads them on reload, which follows the run.
    _write(directory / "server.key", key, 0o640, gid)
    _write(directory / "server.crt", cert, 0o644)


def renewal_reason(directory: Path, dns: list[str], ips: list[str], renew_days: int) -> str | None:
    cert, key = directory / "server.crt", directory / "server.key"
    if not cert.exists() or not key.exists():
        return "no server certificate"
    if san(cert) != (set(dns), set(ips)):
        return "names changed"
    if not _ok("x509", "-in", str(cert), "-noout", "-checkend", str(renew_days * 86400)):
        return f"expires within {renew_days} days"
    if not _ok("verify", "-purpose", "sslserver", "-CAfile", str(directory / "ca" / "ca.crt"), str(cert)):
        return "does not verify against the CA"
    if not key_matches(cert, key):
        return "certificate and key do not match"
    return None


# ------------------------------------------------------------------ public download

def mobileconfig(ca_pem: str, label: str, identifier: str) -> bytes:
    body = "".join(line for line in ca_pem.splitlines() if not line.startswith("-----"))
    content = base64.b64decode(body)
    seed = hashlib.sha256(content).hexdigest()
    payload_uuid = str(uuid.UUID(seed[:32]))
    profile_uuid = str(uuid.UUID(seed[32:]))
    return plistlib.dumps({
        "PayloadContent": [{
            "PayloadCertificateFileName": PUBLIC_NAME + ".crt",
            "PayloadContent": content,
            "PayloadDescription": "Trusts the HTTPS certificate of " + label,
            "PayloadDisplayName": label + " CA",
            "PayloadIdentifier": identifier + ".ca",
            "PayloadType": "com.apple.security.root",
            "PayloadUUID": payload_uuid,
            "PayloadVersion": 1,
        }],
        "PayloadDescription": "Installs the local certificate authority of " + label
                              + ". It is limited to this installation's names and private addresses.",
        "PayloadDisplayName": label + " HTTPS",
        "PayloadIdentifier": identifier,
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": profile_uuid,
        "PayloadVersion": 1,
    })


def ca_page(label: str, print_fingerprint: str, https_url: str) -> str:
    e = html.escape
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trust {e(label)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5;
background:#07131f;color:#f3f7ff}}a{{color:#5bb9ff}}code{{word-break:break-all;background:#10233a;padding:.1rem .3rem;
border-radius:4px}}h2{{font-size:1.05rem;margin-top:1.6rem}}.box{{background:#10233a;border-radius:12px;padding:.8rem 1rem}}</style>
</head><body>
<h1>Trust {e(label)}</h1>
<p>This installation uses its own certificate authority for HTTPS. Install it once on each device, then open
<a href="{e(https_url)}">{e(https_url)}</a>. The authority is limited to this installation's names and private
network addresses; it cannot vouch for any other website.</p>
<div class="box"><p><strong>Compare before trusting.</strong> This page arrives over plain HTTP, so check that the
SHA-256 fingerprint matches the one the installation printed (Ansible output, or
<code>sudo python3 /usr/local/lib/mpdrussoundpi/web_tls.py status</code> on the Pi):</p>
<p><code>{e(print_fingerprint)}</code></p></div>
<h2>iPhone and iPad</h2>
<ol><li>Open <a href="{PUBLIC_NAME}.mobileconfig">the profile</a> in Safari and allow the download.</li>
<li>Settings → Profile Downloaded → Install.</li>
<li>Settings → General → About → Certificate Trust Settings → turn on full trust for the {e(label)} CA.</li></ol>
<h2>Android</h2>
<ol><li>Download <a href="{PUBLIC_NAME}.crt">the certificate</a>.</li>
<li>Settings → Security → More security settings → Encryption &amp; credentials → Install a certificate → CA
certificate (names vary by manufacturer). Chrome trusts it afterwards; Firefox needs "Use third party CA
certificates" in its secret settings.</li></ol>
<h2>macOS</h2>
<ol><li>Download <a href="{PUBLIC_NAME}.crt">the certificate</a> and open it; it lands in Keychain Access.</li>
<li>Double-click it → Trust → "When using this certificate: Always Trust".</li></ol>
<h2>Windows</h2>
<ol><li>Download <a href="{PUBLIC_NAME}.crt">the certificate</a> and open it → Install Certificate → Current
User → "Trusted Root Certification Authorities".</li></ol>
</body></html>
"""


def publish(directory: Path, label: str, identifier: str, https_url: str) -> None:
    ca_pem = (directory / "ca" / "ca.crt").read_text()
    public = directory / "public"
    public.mkdir(mode=0o755, exist_ok=True)
    os.chmod(public, 0o755)
    _write(public / (PUBLIC_NAME + ".crt"), ca_pem, 0o644)
    _write(public / (PUBLIC_NAME + ".mobileconfig"), mobileconfig(ca_pem, label, identifier), 0o644)
    _write(public / "index.html", ca_page(label, sha256_fingerprint(directory / "ca" / "ca.crt"), https_url), 0o644)
    # Public facts for the web application's Status page (mounted read-only into the container).
    if (directory / "server.crt").exists():
        dns, ips = san(directory / "server.crt")
        _write(public / "status.json", json.dumps({
            "schema_version": 1, "server_not_after": not_after(directory / "server.crt"),
            "server_dns": sorted(dns), "server_ips": sorted(ips),
            "ca_sha256_fingerprint": sha256_fingerprint(directory / "ca" / "ca.crt")}, indent=2) + "\n", 0o644)


# ------------------------------------------------------------------ commands

def status(directory: Path) -> dict[str, object]:
    ca, cert = directory / "ca" / "ca.crt", directory / "server.crt"
    report: dict[str, object] = {"ca": None, "server": None}
    if ca.exists():
        report["ca"] = {"sha256_fingerprint": sha256_fingerprint(ca), "not_after": not_after(ca)}
    if cert.exists():
        dns, ips = san(cert)
        report["server"] = {"dns": sorted(dns), "ips": sorted(ips), "not_after": not_after(cert)}
    return report


def ensure(args: argparse.Namespace) -> dict[str, object]:
    directory = Path(args.dir)
    dns = sorted({dns_name(item) for item in args.dns})
    ips = sorted({ip_address(item) for item in args.ip})
    permit_dns = sorted({dns_name(item) for item in (args.permit_dns or dns)})
    permit_ip = [str(ip_network(item)) for item in (args.permit_ip or PRIVATE_RANGES)]
    if not dns and not ips:
        raise TlsError("Give at least one --dns or --ip for the server certificate")
    if not 1 <= args.days <= 825 or not 1 <= args.renew_days < args.days:
        raise TlsError("Server days must be 1–825 and renewal days below them")
    gid = grp.getgrnam(args.key_group).gr_gid if args.key_group else None
    directory.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"ca_created": False, "server_issued": False, "reason": None}
    ca_key, ca_cert = directory / "ca" / "ca.key", directory / "ca" / "ca.crt"
    if ca_key.exists() != ca_cert.exists():
        raise TlsError("Only half of the CA exists; restore it from the backup or remove ca/ deliberately")
    if not ca_cert.exists():
        check_within(dns, ips, permit_dns, permit_ip)
        create_ca(directory, f"{args.label} local CA {datetime.now(timezone.utc):%Y-%m-%d}", permit_dns, permit_ip)
        report["ca_created"] = True
    reason = renewal_reason(directory, dns, ips, args.renew_days)
    if reason:
        issue_server(directory, dns, ips, args.days, gid)
        report.update(server_issued=True, reason=reason)
    elif gid is not None and (directory / "server.key").stat().st_gid != gid:
        os.chown(directory / "server.key", -1, gid)
    publish(directory, args.label, args.identifier, args.https_url or f"https://{(args.dns or args.ip)[0].lower()}/")
    report.update(status(directory))
    if report["server_issued"] and args.reload_unit:
        subprocess.run(["systemctl", "reload-or-restart", args.reload_unit], check=False, timeout=60)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local CA and server certificate for the web UI.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("ensure", help="create or renew as needed (idempotent)")
    run.add_argument("--dir", required=True)
    run.add_argument("--dns", action="append", default=[], help="server name (repeat)")
    run.add_argument("--ip", action="append", default=[], help="server address (repeat)")
    run.add_argument("--permit-dns", action="append", help="CA name constraint (repeat; default: the --dns names)")
    run.add_argument("--permit-ip", action="append", help="CA address constraint in CIDR (repeat; default: private ranges)")
    run.add_argument("--days", type=int, default=SERVER_DAYS)
    run.add_argument("--renew-days", type=int, default=RENEW_DAYS)
    run.add_argument("--key-group", help="group that may read the server key (the proxy's)")
    run.add_argument("--label", default="MPDRussoundPi")
    run.add_argument("--identifier", default="net.itcorner.mpdrussoundpi.web-ca")
    run.add_argument("--https-url", help="address shown on the download page")
    run.add_argument("--reload-unit", help="systemd unit to reload when a new certificate was issued")
    show = sub.add_parser("status", help="fingerprint and expiry")
    show.add_argument("--dir", default="/etc/mpdrussoundpi-web-tls")
    args = parser.parse_args(argv)
    try:
        report = ensure(args) if args.command == "ensure" else status(Path(args.dir))
    except (TlsError, OSError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
