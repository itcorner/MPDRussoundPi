"""Public stream destinations shared by the HTTP client and MPD packet filter."""

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPMessage, HTTPResponse, HTTPSConnection
from typing import IO
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler, HTTPSHandler, HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener,
)

from .sourceurl import validate_source_url


BLOCKED_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/3",
    "::/3", "4000::/2", "8000::/1", "2001::/23", "2001:db8::/32",
    "2002::/16", "3fff::/20",
)
PUBLIC_EXCEPTIONS = (
    "192.0.0.9/32", "192.0.0.10/32", "2001:1::1/128", "2001:1::2/128",
    "2001:1::3/128", "2001:3::/32", "2001:4:112::/48",
    "2001:20::/28", "2001:30::/28",
)


class PublicNetworkError(ValueError):
    """A destination does not satisfy the public-stream policy."""


def blocked_networks(version: int) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = [ipaddress.ip_network(value) for value in BLOCKED_NETWORKS
                if ipaddress.ip_network(value).version == version]
    for value in PUBLIC_EXCEPTIONS:
        exception = ipaddress.ip_network(value)
        if exception.version != version:
            continue
        remaining: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for network in networks:
            if isinstance(network, ipaddress.IPv4Network) and isinstance(exception, ipaddress.IPv4Network) \
                    and exception.subnet_of(network):
                remaining.extend(network.address_exclude(exception))
            elif isinstance(network, ipaddress.IPv6Network) and isinstance(exception, ipaddress.IPv6Network) \
                    and exception.subnet_of(network):
                remaining.extend(network.address_exclude(exception))
            else:
                remaining.append(network)
        networks = remaining
    return tuple(networks)


_BLOCKED = {version: blocked_networks(version) for version in (4, 6)}


def is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not any(address in network for network in _BLOCKED[address.version])


@dataclass(frozen=True)
class PublicEndpoint:
    family: socket.AddressFamily
    kind: socket.SocketKind
    protocol: int
    destination: tuple[str, int] | tuple[str, int, int, int]


def resolve_public(host: str, port: int) -> list[PublicEndpoint]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_address(str(literal)):
        raise PublicNetworkError("Stream destination must resolve only to public internet addresses.")
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise PublicNetworkError("Stream destination DNS lookup failed.") from None
    if not answers or len(answers) > 32:
        raise PublicNetworkError("Stream destination DNS returned no usable public addresses.")
    endpoints: list[PublicEndpoint] = []
    for family, kind, protocol, _, address in answers:
        host_ip = address[0]
        if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(host_ip, str) \
                or not is_public_address(host_ip):
            raise PublicNetworkError("Stream destination must resolve only to public internet addresses.")
        destination = (host_ip, port, 0, 0) if family == socket.AF_INET6 else (host_ip, port)
        endpoints.append(PublicEndpoint(family, kind, protocol, destination))
    return endpoints


def validate_public_url(value: object) -> str:
    url = validate_source_url(value)
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or not parts.hostname:
        raise PublicNetworkError("Stream destination must be a public HTTP(S) URL without credentials.")
    resolve_public(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    return url


def _connect_public(
    address: tuple[str, int], timeout: float, source_address: tuple[str, int] | None = None,
) -> socket.socket:
    answers = resolve_public(*address)
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    for endpoint in answers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Public stream connection timed out.")
        connection: socket.socket | None = None
        try:
            connection = socket.socket(endpoint.family, endpoint.kind, endpoint.protocol)
            connection.settimeout(remaining)
            if source_address is not None:
                connection.bind(source_address)
            connection.connect(endpoint.destination)
            return connection
        except OSError as error:
            if connection is not None:
                connection.close()
            last_error = error
    raise OSError("Public stream connection failed.") from last_error


class _PublicHTTPConnection(HTTPConnection):
    def __init__(
        self, host: str, port: int | None = None, *, timeout: float = 3.0,
        source_address: tuple[str, int] | None = None, blocksize: int = 8192,
    ):
        super().__init__(host, port, timeout=timeout, source_address=source_address, blocksize=blocksize)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(HTTPSConnection):
    def __init__(
        self, host: str, port: int | None = None, *, timeout: float = 3.0,
        context: ssl.SSLContext | None = None,
        source_address: tuple[str, int] | None = None, blocksize: int = 8192,
    ):
        super().__init__(host, port, timeout=timeout, context=context,
                         source_address=source_address, blocksize=blocksize)
        self._create_connection = _connect_public


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, req: Request) -> HTTPResponse:
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(HTTPSHandler):
    def __init__(self) -> None:
        self.context = ssl.create_default_context()
        super().__init__(context=self.context)

    def https_open(self, req: Request) -> HTTPResponse:
        return self.do_open(_PublicHTTPSConnection, req, context=self.context)


class _PublicRedirect(HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str,
    ) -> Request | None:
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_opener() -> OpenerDirector:
    return build_opener(
        ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(), _PublicRedirect(),
    )
