import ipaddress
import json
import re

from ansible.errors import AnsibleFilterError


def deployment_bool(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        return value.lower() in ("true", "yes", "on", "1")
    raise AnsibleFilterError("Expected a boolean or true/false, yes/no, on/off, 1/0 string")


def deployment_port(value):
    if isinstance(value, int) and not isinstance(value, bool):
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
        port = int(value)
    else:
        raise AnsibleFilterError("Expected an integer TCP port from 1 to 65535")
    if not 1 <= port <= 65535:
        raise AnsibleFilterError("Expected an integer TCP port from 1 to 65535")
    return port


def deployment_addresses(value, network=False):
    if not isinstance(value, list) or not value:
        raise AnsibleFilterError("Expected a nonempty list of IP literals" + ("/CIDRs" if network else ""))
    for item in value:
        if not isinstance(item, str) or "%" in item:
            raise AnsibleFilterError("IP addresses must be literal strings without scope identifiers")
        try:
            if network:
                ipaddress.ip_network(item, strict=False)
            else:
                ipaddress.ip_address(item)
        except ValueError:
            raise AnsibleFilterError("Invalid literal IP address or CIDR") from None
    return value


def deployment_hostname(value):
    if not isinstance(value, str) or len(value) > 253:
        raise AnsibleFilterError("Expected a DNS hostname or IPv4 literal")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if (re.fullmatch(r"[0-9.]+", value) or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in value.split(".")
        )):
            raise AnsibleFilterError("Expected a DNS hostname or IPv4 literal") from None
    else:
        if address.version != 4:
            raise AnsibleFilterError("The proxy URL currently requires DNS or an IPv4 literal")
    return value


def deployment_agents(value):
    if not isinstance(value, list):
        raise AnsibleFilterError("Playback agents must be a list, not a JSON string or mapping")
    seen = set()
    from urllib.parse import urlsplit
    for entry in value:
        if not isinstance(entry, dict) or not {"agent_id", "url", "token_file"} <= set(entry) or set(entry) - {
            "agent_id", "url", "token_file", "tls_ca_file"
        }:
            raise AnsibleFilterError("Playback agent entries need agent_id, url, token_file and optional tls_ca_file")
        identity = entry["agent_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity) or identity in seen:
            raise AnsibleFilterError("Playback agent identities must be valid and unique")
        seen.add(identity)
        try:
            url = urlsplit(entry["url"])
            port = url.port
            try:
                loopback = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = False
            if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
                    or url.query or url.fragment
                    or (port is not None and not 1 <= port <= 65535)
                    or (url.scheme == "http" and not loopback)
                    or ("tls_ca_file" in entry and url.scheme != "https")):
                raise ValueError
            for key in ("token_file", "tls_ca_file"):
                if key in entry and (not isinstance(entry[key], str) or not entry[key].startswith("/")
                                     or any(char in entry[key] for char in "\r\n\0")):
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AnsibleFilterError("Invalid playback agent URL or protected absolute file reference") from None
    return "" if not value else json.dumps(value)


class FilterModule:
    def filters(self):
        return {
            "deployment_bool": deployment_bool,
            "deployment_port": deployment_port,
            "deployment_addresses": deployment_addresses,
            "deployment_hostname": deployment_hostname,
            "deployment_agents": deployment_agents,
        }
