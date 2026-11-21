"""Validation, assessment and display rows for the agent's host facts (``/v1/host``).

The status page has no login, so nothing the agent sends is forwarded as it
arrived: every section is rebuilt from an allowlist of fields with strict types
and bounds, and anything else is dropped. Fields the page must never show (MAC
addresses, serial number, machine id, boot id, host name, Wi-Fi network name,
channel, gateway, DNS) are not part of the contract and are not accepted.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable, cast

SECTIONS = ("system", "cpu", "memory", "temperature", "power", "disk", "network", "clock")
PROBE_STATUSES = frozenset({"ok", "error", "pending", "unsupported"})
THROTTLE_FLAGS = ("under_voltage", "frequency_capped", "throttled", "soft_temperature_limit")
MAX_ADDRESSES = 8
MAX_TEXT = 64
MAX_BYTES = 2**50

# Label thresholds. Proposals from the deployment overview plan, kept together
# so they can be tuned in one place.
WIFI_GOOD_DBM = -60
WIFI_FAIR_DBM = -70
MEMORY_LOW_PERCENT = 10.0
TEMPERATURE_HIGH_C = 70.0
TEMPERATURE_CRITICAL_C = 80.0

LEVEL_ORDER = {"unknown": 0, "ok": 1, "warning": 2, "problem": 3}
ERROR_MESSAGES = {
    "probe_failed": "The host probe failed.",
    "invalid_agent_response": "The agent returned invalid host data.",
}


class Invalid(ValueError):
    """A value is outside the contract."""


# ------------------------------------------------------------- primitives

def _integer(value: object, low: int = 0, high: int = MAX_BYTES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid("integer")
    return value


def _number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise Invalid("number")
    return float(value)


def _optional(check: Callable[[object], Any], value: object) -> Any:
    return None if value is None else check(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or not all(32 <= ord(c) < 127 for c in value):
        raise Invalid("text")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise Invalid("boolean")
    return value


def _only(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(cast(dict[str, Any], value)) != keys:
        raise Invalid("object")
    return cast(dict[str, Any], value)


# -------------------------------------------------------- section values

def _system(value: object) -> dict[str, Any]:
    fields = _only(value, {"model", "os", "kernel", "uptime_seconds", "cores"})
    return {
        "model": _optional(_text, fields["model"]), "os": _optional(_text, fields["os"]),
        "kernel": _optional(_text, fields["kernel"]),
        "uptime_seconds": _integer(fields["uptime_seconds"], 0, 10**9),
        "cores": _optional(lambda item: _integer(item, 1, 1024), fields["cores"]),
    }


def _cpu(value: object) -> dict[str, Any]:
    fields = _only(value, {"cores", "load", "busy_percent", "frequency_mhz"})
    load = fields["load"]
    if not isinstance(load, list) or len(cast(list[Any], load)) != 3:
        raise Invalid("load")
    return {
        "cores": _optional(lambda item: _integer(item, 1, 1024), fields["cores"]),
        "load": [round(_number(item, 0, 10**4), 2) for item in cast(list[Any], load)],
        "busy_percent": _optional(lambda item: round(_number(item, 0, 100), 1), fields["busy_percent"]),
        "frequency_mhz": _optional(lambda item: _integer(item, 0, 10**4), fields["frequency_mhz"]),
    }


def _memory(value: object) -> dict[str, Any]:
    fields = _only(value, {"total_bytes", "available_bytes", "swap_total_bytes", "swap_used_bytes"})
    result = {name: _integer(fields[name]) for name in fields}
    if result["available_bytes"] > result["total_bytes"] or result["swap_used_bytes"] > result["swap_total_bytes"]:
        raise Invalid("memory")
    return result


def _temperature(value: object) -> dict[str, Any]:
    return {"celsius": round(_number(_only(value, {"celsius"})["celsius"], -50, 150), 1)}


def _flags(value: object) -> dict[str, bool]:
    fields = _only(value, set(THROTTLE_FLAGS))
    return {name: _boolean(fields[name]) for name in THROTTLE_FLAGS}


def _power(value: object) -> dict[str, Any]:
    fields = _only(value, {"raw", "now", "since_boot"})
    raw = fields["raw"]
    if not isinstance(raw, str) or re.fullmatch(r"0x[0-9a-f]{1,8}", raw) is None:
        raise Invalid("raw")
    return {"raw": raw, "now": _flags(fields["now"]), "since_boot": _flags(fields["since_boot"])}


def _disk(value: object) -> dict[str, Any]:
    fields = _only(value, {"total_bytes", "free_bytes"})
    result = {name: _integer(fields[name]) for name in fields}
    if result["free_bytes"] > result["total_bytes"]:
        raise Invalid("disk")
    return result


def _address(value: object) -> dict[str, Any]:
    fields = _only(value, {"family", "address", "prefix"})
    family = _integer(fields["family"], 4, 6)
    if family not in (4, 6) or not isinstance(fields["address"], str):
        raise Invalid("family")
    parsed = ipaddress.ip_address(fields["address"])
    if parsed.version != family or parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
        raise Invalid("address")
    return {"family": family, "address": str(parsed), "prefix": _integer(fields["prefix"], 0, 128 if family == 6 else 32)}


def _wifi(value: object) -> dict[str, Any]:
    fields = _only(value, {"signal_dbm", "link_rate_mbps", "band"})
    band = fields["band"]
    if band not in (None, "2.4 GHz", "5 GHz", "6 GHz"):
        raise Invalid("band")
    return {
        "signal_dbm": _optional(lambda item: _integer(item, -127, 0), fields["signal_dbm"]),
        "link_rate_mbps": _optional(lambda item: round(_number(item, 0, 100000), 1), fields["link_rate_mbps"]),
        "band": band,
    }


def _network(value: object) -> dict[str, Any]:
    fields = _only(value, {"state", "interface_type", "addresses", "wifi"})
    if fields["state"] not in ("connected", "disconnected", "no_uplink") or fields["interface_type"] not in (
            None, "wifi", "ethernet", "other"):
        raise Invalid("network")
    addresses = fields["addresses"]
    if addresses is not None and (not isinstance(addresses, list) or len(cast(list[Any], addresses)) > MAX_ADDRESSES):
        raise Invalid("addresses")
    return {
        "state": fields["state"], "interface_type": fields["interface_type"],
        "addresses": None if addresses is None else [_address(item) for item in cast(list[Any], addresses)],
        "wifi": _optional(_wifi, fields["wifi"]),
    }


def _clock(value: object) -> dict[str, Any]:
    return {"synchronized": _boolean(_only(value, {"synchronized"})["synchronized"])}


VALUE_CHECKS: dict[str, Callable[[object], dict[str, Any]]] = {
    "system": _system, "cpu": _cpu, "memory": _memory, "temperature": _temperature,
    "power": _power, "disk": _disk, "network": _network, "clock": _clock,
}


def _failed(code: str = "invalid_agent_response") -> dict[str, Any]:
    return {"status": "error", "age_ms": None, "stale": True, "error": {"code": code, "retryable": True}, "value": None}


def _probe(name: str, probe: object) -> dict[str, Any]:
    """One section as a probe object; an invalid section degrades alone."""
    try:
        fields = cast(dict[str, Any], probe)
        if not isinstance(probe, dict) or fields.get("status") not in PROBE_STATUSES:
            raise Invalid("probe")
        status = fields["status"]
        age = _optional(lambda item: _integer(item, 0, 10**10), fields.get("age_ms"))
        error: dict[str, Any] | None = None
        if status == "error":
            code = cast(dict[str, Any], fields.get("error") or {}).get("code")
            error = {"code": code if code in ERROR_MESSAGES else "probe_failed", "retryable": True}
        value = VALUE_CHECKS[name](fields.get("value")) if status == "ok" else None
        return {"status": status, "age_ms": age if status == "ok" else None,
                "stale": True if status != "ok" else _boolean(fields.get("stale")), "error": error, "value": value}
    except (Invalid, ValueError, TypeError, KeyError, AttributeError):
        return _failed()


def validate_host_response(response: dict[str, Any]) -> dict[str, Any]:
    """Sanitized sections of a ``/v1/host`` response (identity is checked by the caller)."""
    return {name: _probe(name, response.get(name)) for name in SECTIONS}


# ------------------------------------------------------------ assessment

def _value(sections: dict[str, Any], name: str) -> dict[str, Any] | None:
    section = sections.get(name)
    return section["value"] if section and section["status"] == "ok" else None


def assess(sections: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Qualitative labels: ``{key: {level: ok|warning|problem|unknown, label}}``."""
    unknown = {"level": "unknown", "label": "unknown"}
    result: dict[str, dict[str, str]] = {}
    network = _value(sections, "network")
    result["network"] = unknown if network is None else (
        {"level": "ok", "label": "connected"} if network["state"] == "connected"
        else {"level": "problem", "label": "no uplink" if network["state"] == "no_uplink" else "disconnected"})
    wifi = network["wifi"] if network else None
    dbm = wifi["signal_dbm"] if wifi else None
    if dbm is not None:
        result["wifi"] = ({"level": "ok", "label": "good"} if dbm >= WIFI_GOOD_DBM
                          else {"level": "warning", "label": "fair" if dbm >= WIFI_FAIR_DBM else "weak"})
    memory = _value(sections, "memory")
    if memory is not None and memory["total_bytes"] > 0:
        free = 100.0 * memory["available_bytes"] / memory["total_bytes"]
        result["memory"] = {"level": "warning", "label": "low"} if free < MEMORY_LOW_PERCENT else {"level": "ok", "label": "ok"}
    else:
        result["memory"] = unknown
    temperature = _value(sections, "temperature")
    if temperature is not None:
        celsius = temperature["celsius"]
        result["temperature"] = (
            {"level": "problem", "label": "critical"} if celsius >= TEMPERATURE_CRITICAL_C
            else {"level": "warning", "label": "high"} if celsius >= TEMPERATURE_HIGH_C else {"level": "ok", "label": "ok"})
    power = _value(sections, "power")
    if power is not None:
        result["power"] = (
            {"level": "problem", "label": "problem now"} if any(power["now"].values())
            else {"level": "warning", "label": "occurred since boot"} if any(power["since_boot"].values())
            else {"level": "ok", "label": "ok"})
    clock = _value(sections, "clock")
    if clock is not None:
        result["clock"] = {"level": "ok", "label": "synchronized"} if clock["synchronized"] else {
            "level": "warning", "label": "not synchronized"}
    return result


def overall(assessment: dict[str, dict[str, str]]) -> str:
    levels = [item["level"] for item in assessment.values() if item["level"] != "unknown"]
    return max(levels, key=LEVEL_ORDER.__getitem__) if levels else "unknown"


# ----------------------------------------------------------- presentation

def _size(value: int) -> str:
    for unit, factor in (("GiB", 2**30), ("MiB", 2**20)):
        if value >= factor:
            return f"{value / factor:.1f} {unit}" if unit == "GiB" else f"{value / factor:.0f} {unit}"
    return f"{value // 1024} KiB"


def _duration(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return " ".join(part for part in (f"{days} d" if days else "", f"{hours} h" if hours or days else "", f"{minutes} min") if part)


def view(sections: dict[str, Any], assessment: dict[str, dict[str, str]]) -> dict[str, Any]:
    """One-line summary and detail rows, all plain text built from validated values."""
    summary: list[str] = []
    rows: list[dict[str, str]] = []

    def row(label: str, text: str, level: str = "") -> None:
        rows.append({"label": label, "value": text, "level": level})

    network, cpu, memory = _value(sections, "network"), _value(sections, "cpu"), _value(sections, "memory")
    temperature, power = _value(sections, "temperature"), _value(sections, "power")
    system, disk, clock = _value(sections, "system"), _value(sections, "disk"), _value(sections, "clock")
    wifi = network["wifi"] if network else None
    if wifi and wifi["signal_dbm"] is not None:
        summary.append(f"Wi-Fi {assessment['wifi']['label']} ({wifi['signal_dbm']} dBm)")
    elif network and network["state"] != "connected":
        summary.append(f"Network {assessment['network']['label']}")
    if cpu and cpu["busy_percent"] is not None:
        summary.append(f"CPU {cpu['busy_percent']:.0f} %")
    if memory and memory["total_bytes"]:
        summary.append(f"Memory {100 * (memory['total_bytes'] - memory['available_bytes']) / memory['total_bytes']:.0f} % used")
    if temperature:
        summary.append(f"{temperature['celsius']:.0f} °C")
    if power and assessment.get("power", {}).get("level") != "ok":
        summary.append(f"Power: {assessment['power']['label']}")

    if system:
        for label, key in (("Model", "model"), ("Operating system", "os"), ("Kernel", "kernel")):
            if system[key]:
                row(label, system[key])
        row("Uptime", _duration(system["uptime_seconds"]))
    if cpu:
        row("CPU load (1, 5, 15 min)", " / ".join(f"{item:.2f}" for item in cpu["load"]) + (f" on {cpu['cores']} cores" if cpu["cores"] else ""))
        if cpu["frequency_mhz"]:
            row("CPU frequency", f"{cpu['frequency_mhz']} MHz")
    if memory:
        row("Memory", f"{_size(memory['total_bytes'] - memory['available_bytes'])} used of {_size(memory['total_bytes'])}",
            assessment["memory"]["level"])
        if memory["swap_total_bytes"]:
            row("Swap", f"{_size(memory['swap_used_bytes'])} used of {_size(memory['swap_total_bytes'])}")
    if temperature:
        row("Temperature", f"{temperature['celsius']:.1f} °C", assessment["temperature"]["level"])
    if power:
        flags = [name.replace("_", " ") for name in THROTTLE_FLAGS if power["now"][name]]
        past = [name.replace("_", " ") for name in THROTTLE_FLAGS if power["since_boot"][name] and not power["now"][name]]
        row("Power and throttling", "; ".join(
            ([f"now: {', '.join(flags)}"] if flags else []) + ([f"since boot: {', '.join(past)}"] if past else [])) or "no issues",
            assessment["power"]["level"])
    if disk:
        row("Disk", f"{_size(disk['free_bytes'])} free of {_size(disk['total_bytes'])}")
    if network:
        kind = {"wifi": "Wi-Fi", "ethernet": "Ethernet", "other": "Other", None: "none"}[network["interface_type"]]
        row("Network", f"{kind}, {assessment['network']['label']}", assessment["network"]["level"])
        if network["addresses"] is None:
            row("Addresses", "unavailable")
        elif network["addresses"]:
            row("Addresses", ", ".join(f"{item['address']}/{item['prefix']}" for item in network["addresses"]))
        if wifi:
            parts = ([f"{wifi['signal_dbm']} dBm ({assessment['wifi']['label']})"] if wifi["signal_dbm"] is not None else []) + (
                [f"{wifi['link_rate_mbps']:g} Mbit/s"] if wifi["link_rate_mbps"] is not None else []) + (
                [wifi["band"]] if wifi["band"] else [])
            row("Wi-Fi link", ", ".join(parts) or "unavailable", assessment.get("wifi", {}).get("level", ""))
    if clock:
        row("Clock", assessment["clock"]["label"], assessment["clock"]["level"])
    return {"overall": overall(assessment), "summary": summary, "rows": rows}


def snapshot_view(sections: dict[str, Any]) -> dict[str, Any]:
    """Everything the page needs for one validated host."""
    assessment = assess(sections)
    return {"sections": sections, "assessment": assessment, "view": view(sections, assessment)}
