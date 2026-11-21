"""Component and audio sections of the Status page overview.

Pure functions over already available data: the playback client's cached
snapshots (validated), the Russound configuration and its last hardware poll,
and the process itself. Nothing here probes a host or the network. Every value
is plain text built from a fixed vocabulary or a validated, sanitized field, and
each row names its level so the page never relies on colour alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, cast

LEVEL_ORDER = {"unknown": 0, "ok": 1, "warning": 2, "problem": 3}
CERTIFICATE_WARNING_DAYS = 30
REVISION = re.compile(r"[a-f0-9]{40}\Z")
BUILD_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):\d{2}Z\Z")
IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{1,63}/mpdrussoundpi-[a-z0-9-]{1,32}\Z")
IMAGE_TAG = re.compile(r"(dev-[a-f0-9]{40}|\d+\.\d+\.\d+(?:-rc\.\d+)?)\Z")
BUILD_SOURCES = {"ci": "CI build (GitHub Actions)", "local": "Local build"}
FORMAT = re.compile(r"[0-9a-z:]{1,32}\Z")
SINK_NAME = re.compile(r"[\w.-]{1,96}\Z", re.ASCII)
MAX_LABEL = 120

FEATURE_LABELS = {
    "inventory": "inventory", "playback": "playback", "now_playing": "now playing",
    "audio_diagnostics": "audio diagnostics", "host_info": "host information", "audio_info": "audio hardware",
}
CONTROL_GROUPS = (
    ("transport", {"play", "pause", "stop"}), ("gain", {"set_gain"}),
    ("station selection", {"set_tunein_station", "select_favorite"}),
    ("favorites", {"add_favorite", "remove_favorite", "move_favorite"}),
)
SOURCE_LABELS = {"environment": "from the deployment environment", "saved": "from the saved configuration", "default": "default"}
STATE_LABELS = {"play": "Playing", "pause": "Paused", "stop": "Stopped"}


def _row(label: str, value: str, level: str = "") -> dict[str, str]:
    return {"label": label, "value": value, "level": level}


def _worst(levels: list[str]) -> str:
    known = [level for level in levels if level in LEVEL_ORDER and level != "unknown"]
    return max(known, key=LEVEL_ORDER.__getitem__) if known else "unknown"


def _clean(value: object, limit: int = MAX_LABEL) -> str | None:
    """Printable text only: labels from configuration or streams are not trusted."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    return cleaned[:limit] or None


def _age(then: object, now: datetime) -> str | None:
    if not isinstance(then, str):
        return None
    try:
        moment = datetime.fromisoformat(then.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = max(0, int((now - moment).total_seconds()))
    return "just now" if seconds < 4 else f"{seconds} s ago" if seconds < 120 else f"{seconds // 60} min ago"


def _duration(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return " ".join(part for part in (f"{days} d" if days else "", f"{hours} h" if hours or days else "", f"{rest // 60} min") if part)


def _section(identifier: str, title: str, level: str, summary: str, rows: list[dict[str, str]],
             groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"id": identifier, "title": title, "level": level, "summary": summary, "rows": rows, "groups": groups or []}


# ------------------------------------------------------------ web application

def image_facts(image: str | None, version: str | None) -> tuple[str, str, str] | None:
    """(reference, kind, Docker Hub link) of a published image, or None when it is not a valid one."""
    if not (isinstance(image, str) and isinstance(version, str) and IMAGE_NAME.fullmatch(image) and IMAGE_TAG.fullmatch(version)):
        return None
    kind = ("Development image" if version.startswith("dev-") else
            "Release candidate" if "-rc." in version else "Release")
    return f"{image}:{version}", kind, f"https://hub.docker.com/r/{image}/tags?name={version}"


def web_section(*, uptime_seconds: int, runtime: str, revision: str | None, clients: int,
                build_time: str | None = None, build_source: str | None = None,
                image: str | None = None, version: str | None = None) -> dict[str, Any]:
    reported = revision if isinstance(revision, str) and REVISION.fullmatch(revision) else None
    moment = BUILD_TIME.fullmatch(build_time) if isinstance(build_time, str) else None
    source = BUILD_SOURCES.get(build_source or "")
    published = image_facts(image, version) if build_source == "ci" else None
    runtime_text = {"container": "Container (detected)", "native": "Native process (detected)"}.get(runtime, "Unknown")
    built = f"{moment.group(1)}-{moment.group(2)}-{moment.group(3)} {moment.group(4)}:{moment.group(5)} UTC" if moment else None
    rows = [
        _row("Status", "Running", "ok"),
        _row("Runtime", runtime_text),
    ]
    if published is not None:
        rows.append(_row("Image", f"{published[0]} (reported by the build)"))
        rows.append(_row("Image kind", published[1] + " (published by CI)"))
        rows.append({**_row("Docker Hub", "Open this tag on Docker Hub"), "link": published[2]})
    elif build_source == "local":
        rows.append(_row("Image", "local build, not published to a registry"))
    else:
        rows.append(_row("Image", "not reported by this build"))
    rows += [
        _row("Build", (source or "not reported by this build") + (" (reported by the build)" if source else "")),
        _row("Revision", f"{reported[:7]} (latest commit, reported by the build)" if reported else "not reported by this build"),
        _row("Built", f"{built} (reported by the build)" if built else "not reported by this build"),
        _row("Uptime", _duration(uptime_seconds)),
        _row("Live clients", str(clients)),
    ]
    origin = (published[1] + (f" {version}" if published[1] != "Development image" else "")) if published else (
        "local build" if build_source == "local" else None)
    tail = " · ".join(part for part in (origin, reported[:7] if reported else None) if part)
    return _section("web", "Web application", "ok", f"Running · {_duration(uptime_seconds)}" + (f" · {tail}" if tail else ""), rows)


# --------------------------------------------------------------- playback agent

def _controls(operations: object) -> str:
    enabled = set(cast(list[str], operations)) if isinstance(operations, list) else set()
    groups = [name for name, members in CONTROL_GROUPS if enabled & members]
    return "Manual control enabled: " + ", ".join(groups) if groups else "Read-only"


def certificate_row(certificate: dict[str, Any]) -> dict[str, str] | None:
    status = certificate.get("status")
    if status == "ok" and isinstance(certificate.get("days_left"), int):
        days, expires = certificate["days_left"], str(certificate["expires_at"])[:10]
        if days < 0:
            return _row("TLS certificate", f"Expired on {expires}", "problem")
        level = "warning" if days < CERTIFICATE_WARNING_DAYS else "ok"
        return _row("TLS certificate", f"Valid until {expires} ({days} days)", level)
    if status == "unavailable":
        return _row("TLS certificate", "could not be checked", "unknown")
    return None


def agent_section(agent: dict[str, Any], extra: dict[str, Any], now: datetime) -> dict[str, Any]:
    identifier = str(agent.get("agent_id", "agent"))
    title = f"Playback agent ({identifier})"
    available = agent.get("status") == "ok"
    snapshot = agent.get("snapshot") if isinstance(agent.get("snapshot"), dict) else {}
    certificate = certificate_row(extra.get("certificate", {}))
    rows: list[dict[str, str]] = []
    if not available:
        error = agent.get("error") if isinstance(agent.get("error"), dict) else {}
        message = _clean(cast(dict[str, Any], error).get("message"), 80) or "Playback agent is unreachable."
        rows.append(_row("Connection", f"Unavailable: {message}", "problem"))
        last = _age(agent.get("last_success_at"), now)
        rows.append(_row("Last report", last or "never"))
    else:
        verified = (extra.get("certificate") or {}).get("status") == "ok"
        rows.append(_row("Connection", "Connected, TLS certificate verified" if verified else "Connected", "ok"))
        rows.append(_row("Last report", _age(agent.get("last_success_at"), now) or "unknown"))
        stale = bool(snapshot.get("stale"))
        rows.append(_row("Playback snapshot", "Stale" if stale else "Fresh", "warning" if stale else "ok"))
        outputs = snapshot.get("outputs")
        rows.append(_row("Outputs", str(len(cast(list[Any], outputs))) if isinstance(outputs, list) else "unknown"))
        rows.append(_row("Reported features", ", ".join(FEATURE_LABELS[name] for name in FEATURE_LABELS
                                                          if name in extra.get("read_features", [])) or "none"))
        rows.append(_row("Controls", _controls(agent.get("write_operations"))))
    if certificate is not None:
        rows.append(certificate)
    rows.append(_row("Agent version", "not reported"))
    level = "problem" if not available else _worst([row["level"] for row in rows if row["level"]] or ["ok"])
    summary = "Unavailable" if not available else "Connected" + (
        f" · {len(snapshot['outputs'])} outputs" if isinstance(snapshot.get("outputs"), list) else "")
    if available and certificate is not None and certificate["level"] in ("warning", "problem"):
        summary += " · certificate " + ("expired" if certificate["level"] == "problem" else "expires soon")
    return _section(f"agent:{identifier}", title, level, summary, rows)


def missing_agent_section() -> dict[str, Any]:
    return _section("agent", "Playback agent", "unknown", "Not configured",
                    [_row("Status", "No playback agent is configured, so there is no playback or audio information.")])


# ----------------------------------------------------- gateway and controller

def gateway_section(*, config: dict[str, Any] | None, effective_backend: dict[str, Any], kind: str,
                    observed_age: str | None) -> dict[str, Any]:
    if config is None:
        return _section("gateway", "Russound gateway and controller", "warning", "Setup required",
                        [_row("Status", "No valid configuration yet. Complete setup on the Configuration page.", "warning")])
    mode = effective_backend.get("mode", {}) if isinstance(effective_backend.get("mode"), dict) else {}
    host = effective_backend.get("host", {}) if isinstance(effective_backend.get("host"), dict) else {}
    port = effective_backend.get("port", {}) if isinstance(effective_backend.get("port"), dict) else {}
    serial = mode.get("value") == "serial"
    source = SOURCE_LABELS.get(str(mode.get("source")), "")
    rows = [_row("Transport", ("Serial adapter" if serial else "TCP gateway") + (f" ({source})" if source else ""))]
    if serial:
        rows.append(_row("Endpoint", "serial adapter (device path not shown)"))
    else:
        host_text, port_text = _clean(host.get("value"), 64), port.get("value")
        endpoint = f"{host_text}:{port_text}" if host_text and isinstance(port_text, int) else "not configured"
        host_source = SOURCE_LABELS.get(str(host.get("source")), "")
        rows.append(_row("Endpoint", endpoint + (f" ({host_source})" if host_source else "")))
    state = {
        "responding": ("Controller responding", "ok"),
        "silent": ("Gateway reachable, controller not responding", "problem"),
        "unreachable": ("Gateway unreachable", "problem"),
    }.get(kind, ("Not checked yet", "unknown"))
    rows.append(_row("Hardware link", state[0] + (f" (checked {observed_age})" if observed_age and kind != "unknown" else ""), state[1]))
    controllers = [item for item in config.get("controllers", []) if isinstance(item, dict)]
    zones = [item for item in config.get("zones", []) if isinstance(item, dict)]
    for item in sorted(controllers, key=lambda entry: int(entry.get("id", 0)) if isinstance(entry.get("id"), int) else 0):
        identifier = item.get("id")
        enabled = sum(1 for zone in zones if zone.get("controller") == identifier and zone.get("enabled", True) is not False)
        rows.append(_row(f"Controller {identifier}", f"{item.get('zone_count', '?')} zones configured, {enabled} enabled"))
    rows.append(_row("Controller model and firmware", "not reported"))
    summary = state[0]
    if controllers:
        summary += f" · {len(controllers)} controller{'s' if len(controllers) != 1 else ''}"
    return _section("gateway", "Russound gateway and controller", state[1], summary, rows)


# ------------------------------------------------------------------ audio system

def _probe(output: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    container = output.get(section)
    probe = cast(dict[str, Any], container).get(name) if name and isinstance(container, dict) else container
    return cast(dict[str, Any], probe) if isinstance(probe, dict) else {}


def _value(probe: dict[str, Any]) -> dict[str, Any]:
    value = probe.get("value") if probe.get("status") == "ok" else None
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _sink(value: object) -> str | None:
    return value if isinstance(value, str) and SINK_NAME.fullmatch(value) else None


def _channels(value: object) -> str:
    return " + ".join(cast(list[str], value)) if isinstance(value, list) and value else "unknown"


def output_group(output: dict[str, Any], sink: dict[str, Any] | None) -> dict[str, Any]:
    identifier = str(output.get("id", "output"))
    label = _clean(output.get("label"), 64) or identifier
    control = output.get("control") if isinstance(output.get("control"), dict) else {}
    desired = control.get("desired_state") if control.get("desired_state") in STATE_LABELS else None
    playback_probe = _probe(output, "playback", "")
    playback = _value(playback_probe)
    observed = playback.get("state") if playback.get("state") in STATE_LABELS else None
    rows: list[dict[str, str]] = []
    if playback_probe.get("status") == "ok" and observed is not None:
        mismatch = desired is not None and desired != observed
        text = STATE_LABELS[observed] + (f" (desired: {desired})" if mismatch else "")
        rows.append(_row("Playback", text, "warning" if mismatch else "ok"))
    else:
        rows.append(_row("Playback", "Unknown (playback status unavailable)", "warning"))
    selection = output.get("selection") if isinstance(output.get("selection"), dict) else {}
    station = _clean(cast(dict[str, Any], selection).get("label"))
    rows.append(_row("Station", station or "none selected"))
    stream = []
    audio_format, bitrate = playback.get("audio_format"), playback.get("bitrate_kbps")
    if isinstance(audio_format, str) and FORMAT.fullmatch(audio_format):
        stream.append(audio_format)
    if isinstance(bitrate, (int, float)) and not isinstance(bitrate, bool) and 0 < bitrate < 100000:
        stream.append(f"{bitrate:.0f} kbit/s")
    if stream:
        rows.append(_row("Stream", " · ".join(stream)))
    route_probe = _probe(output, "audio", "route")
    route = _value(route_probe)
    if route_probe.get("status") == "ok" and isinstance(route.get("correct"), bool):
        actual, expected = _sink(route.get("actual_sink")) or "unknown sink", _sink(route.get("expected_sink")) or "unknown sink"
        rows.append(_row("Audio route", f"Correct ({actual})" if route["correct"] else f"Wrong: expected {expected}, actual {actual}",
                         "ok" if route["correct"] else "problem"))
    elif route_probe.get("status") == "not_applicable":
        rows.append(_row("Audio route", "Not applicable (not playing)"))
    else:
        rows.append(_row("Audio route", "Not checked yet" if route_probe.get("status") == "pending" else "Check failed",
                         "unknown" if route_probe.get("status") == "pending" else "warning"))
    readiness_probe = _probe(output, "audio", "readiness")
    ready = _value(readiness_probe).get("ready")
    if readiness_probe.get("status") == "ok" and isinstance(ready, bool):
        rows.append(_row("Audio device", "Ready" if ready else "Not ready", "ok" if ready else "problem"))
    else:
        rows.append(_row("Audio device", "Not checked yet" if readiness_probe.get("status") == "pending" else "Check failed",
                         "unknown" if readiness_probe.get("status") == "pending" else "warning"))
    gain = output.get("gain") if isinstance(output.get("gain"), dict) else {}
    configured = cast(dict[str, Any], gain).get("configured_db")
    measured = _probe(output, "audio", "gain").get("status") == "ok"
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        rows.append(_row("Gain", f"{configured:.1f} dB configured" + ("" if measured else " (measurement unavailable)")))
    if playback_probe.get("status") == "ok":
        reachable, failed = playback.get("reachable"), playback.get("mpd_error") is not None
        rows.append(_row("MPD", "Reachable" if reachable is True else "Unreachable" if reachable is False else "Unknown",
                         "problem" if reachable is False or failed else "ok"))
        if failed:
            rows.append(_row("MPD error", "MPD reports an error for this output", "problem"))
    if sink is not None:
        rows.append(_row("Hardware", f"{sink['id']}: card channels {_channels(sink.get('card_channels'))} as stereo"
                                      + (" (remix on)" if sink.get("remix") else "")))
    else:
        rows.append(_row("Hardware", "not reported"))
    level = _worst([row["level"] for row in rows if row["level"]])
    route_text = next((row["value"] for row in rows if row["label"] == "Audio route"), "")
    summary = " · ".join(part for part in (
        STATE_LABELS.get(observed or "", "Unknown"), station or "no station",
        "route ok" if route_text.startswith("Correct") else "route wrong" if route_text.startswith("Wrong") else "route unknown") if part)
    return {"id": identifier, "title": label, "level": level, "summary": summary, "rows": rows}


def device_group(device: dict[str, Any], index: int, outputs: list[dict[str, str]], total: int) -> dict[str, Any]:
    usb = device.get("usb")
    rows = [
        _row("Model", f"USB {usb['vendor']}:{usb['product']}" if isinstance(usb, dict) else "not identified"),
        _row("Card profile", str(device.get("profile") or "unknown")),
    ]
    for sink in device.get("sinks", []):
        users = [item["id"] for item in outputs if item["sink"] == sink["id"]]
        text = f"{sink['id']}: card channels {_channels(sink.get('card_channels'))} → {_channels(sink.get('output_channels'))}"
        if sink.get("remix"):
            text += " (remix on)"
        if users:
            text += f", used by {', '.join(users)}"
        rows.append(_row(str(sink.get("description") or sink["id"]), text))
    return {"id": f"device:{index}", "title": "Sound card" + (f" {index}" if total > 1 else ""), "level": "ok",
            "summary": rows[0]["value"] + f" · {len(device.get('sinks', []))} outputs wired", "rows": rows}


def audio_section(agent: dict[str, Any], audio: dict[str, Any]) -> dict[str, Any]:
    identifier = str(agent.get("agent_id", "agent"))
    title = f"Audio system ({identifier})"
    if agent.get("status") != "ok":
        return _section(f"audio:{identifier}", title, "problem", "Agent unavailable",
                        [_row("Audio system", "Not reported while the playback agent is unavailable.", "problem")])
    snapshot = agent.get("snapshot") if isinstance(agent.get("snapshot"), dict) else {}
    outputs = [item for item in cast(list[Any], snapshot.get("outputs", [])) if isinstance(item, dict)]
    description = audio.get("data") if audio.get("status") == "ok" and isinstance(audio.get("data"), dict) else None
    sinks = {}
    mapping: list[dict[str, str]] = []
    if description:
        mapping = list(description.get("outputs", []))
        by_sink = {sink["id"]: sink for device in description.get("devices", []) for sink in device.get("sinks", [])}
        sinks = {item["id"]: by_sink.get(item["sink"]) for item in mapping}
    groups = [output_group(output, sinks.get(output.get("id"))) for output in outputs]
    rows: list[dict[str, str]] = []
    if description:
        groups += [device_group(device, number, mapping, len(description["devices"]))
                   for number, device in enumerate(description["devices"], 1)]
    else:
        reason = {"unsupported": "this agent does not report audio hardware (update the agent and the playback proxy)",
                  "pending": "waiting for the first report"}.get(str(audio.get("status")), "unavailable")
        rows.append(_row("Audio hardware", reason))
    levels = [group["level"] for group in groups if group["id"].startswith("device:") is False]
    level = _worst(levels) if levels else "unknown"
    playing = sum(1 for group in groups if group["summary"].startswith("Playing"))
    summary = f"{playing} of {len(outputs)} outputs playing" if outputs else "No outputs reported"
    return _section(f"audio:{identifier}", title, level, summary, rows, groups)


def compose(sections: list[dict[str, Any]], extra_levels: list[str] | None = None) -> dict[str, Any]:
    """``extra_levels`` are levels of cards shown in the same panel (the host cards) that are not sections."""
    levels = [section["level"] for section in sections] + list(extra_levels or [])
    return {"api_version": 1, "overall": _worst(levels), "sections": sections}


def now() -> datetime:
    return datetime.now(timezone.utc)
