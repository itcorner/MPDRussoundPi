"""Shared, read-only USB selection and downstream routing validation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal, cast


class HardwareUnavailable(RuntimeError):
    def __init__(self, message: str, *, code: Literal[
        "audio_unavailable", "hardware_missing", "hardware_ambiguous",
        "hardware_profile_unavailable", "hardware_output_unavailable", "downstream_unavailable",
    ] = "audio_unavailable") -> None:
        super().__init__(message)
        self.code = code


def usb_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Invalid USB identifier")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,4}", value):
        number = int(value, 16)
    else:
        raise ValueError("Invalid USB identifier")
    if not 0 <= number <= 0xffff:
        raise ValueError("Invalid USB identifier")
    return number


@dataclass(frozen=True)
class HardwareSelector:
    vendor: int
    product: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor", usb_id(self.vendor))
        object.__setattr__(self, "product", usb_id(self.product))

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> "HardwareSelector":
        if set(value) != {"vendor", "product"}:
            raise ValueError("Hardware selector requires vendor and product only")
        return cls(usb_id(value["vendor"]), usb_id(value["product"]))

    def matches(self, properties: Mapping[str, object]) -> bool:
        if properties.get("device.api") != "alsa" or properties.get("device.bus") != "usb":
            return False
        try:
            vendor = usb_id(properties.get("device.vendor.id"))
            product = usb_id(properties.get("device.product.id"))
        except ValueError:
            return False
        return vendor == self.vendor and product == self.product


def object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    result: dict[str, object] = {}
    for key, entry in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise ValueError("Object field name must be a string")
        result[key] = entry
    return result


def hardware_configuration(
    device: Mapping[str, object],
) -> tuple[HardwareSelector, dict[str, tuple[str, str]]] | None:
    if "hardware-selector" not in device:
        return None
    selector = HardwareSelector.parse(object_mapping(device["hardware-selector"]))
    if device.get("card-profile") != "analog-surround-51":
        raise ValueError("USB hardware selector requires analog-surround-51")
    sinks = device.get("sinks")
    if not isinstance(sinks, list):
        raise ValueError("USB hardware selector requires sinks")
    mappings: dict[str, tuple[str, str]] = {}
    for item in cast(list[object], sinks):
        sink = object_mapping(item)
        name = sink.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[\w-]+", name, re.ASCII) or name in mappings:
            raise ValueError("USB selector sink names must be safe and unique")
        if sink.get("channel_map", "[ FL FR ]") != "[ FL FR ]":
            raise ValueError("USB selector requires stereo FL/FR input")
        channels = sink.get("master_channel_map")
        if not isinstance(channels, str) or not re.fullmatch(
            r"\[\s*(?:FL\s+FR|FC\s+LFE|RL\s+RR)\s*\]", channels,
        ):
            raise ValueError("USB selector requires a channel mapping")
        pair = tuple(channels.strip("[] ").split())
        if pair not in (("FL", "FR"), ("FC", "LFE"), ("RL", "RR")):
            raise ValueError("Invalid USB selector channel pair")
        mappings[name] = (pair[0], pair[1])
    validate_mappings(mappings)
    return selector, mappings


def validate_hardware_devices(devices: Sequence[object]) -> None:
    configured = [object_mapping(device) for device in devices]
    selections = [hardware_configuration(device) for device in configured]
    if not any(selections):
        return
    models: set[HardwareSelector] = set()
    names: set[str] = set()
    sink_names: set[str] = set()
    for device, selection in zip(configured, selections):
        name = device.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name, re.ASCII) or name in names:
            raise ValueError("Audio device names must be safe and unique")
        names.add(name)
        sinks = device.get("sinks")
        if not isinstance(sinks, list):
            raise ValueError("Invalid configured sinks")
        for entry in cast(list[object], sinks):
            sink_name = object_mapping(entry).get("name")
            if not isinstance(sink_name, str) or sink_name in sink_names:
                raise ValueError("Sink names must be unique across audio devices")
            sink_names.add(sink_name)
        if selection:
            selector, _ = selection
            if selector in models:
                raise ValueError("Duplicate USB hardware selector")
            models.add(selector)


def validate_mappings(mappings: Mapping[str, tuple[str, str]]) -> None:
    if not mappings:
        raise ValueError("At least one downstream channel mapping is required")
    used: set[str] = set()
    for name, channels in mappings.items():
        if not name or channels not in (("FL", "FR"), ("FC", "LFE"), ("RL", "RR")):
            raise ValueError("Invalid downstream channel mapping")
        if used.intersection(channels):
            raise ValueError("Downstream channel mappings overlap")
        used.update(channels)


def unique_card(
    cards: Sequence[Mapping[str, object]], selector: HardwareSelector,
) -> Mapping[str, object]:
    matches = [card for card in cards if selector.matches(card)]
    if not matches:
        raise HardwareUnavailable("Selected USB audio hardware is missing", code="hardware_missing")
    if len(matches) != 1:
        raise HardwareUnavailable("Selected USB audio hardware is ambiguous", code="hardware_ambiguous")
    return matches[0]


def graph_properties(item: Mapping[str, object]) -> dict[str, object]:
    return object_mapping(object_mapping(item.get("info")).get("props"))


def graph_objects(graph: Sequence[Mapping[str, object]]) -> dict[int, Mapping[str, object]]:
    objects: dict[int, Mapping[str, object]] = {}
    for item in graph:
        identity = item.get("id")
        if isinstance(identity, bool) or not isinstance(identity, int) or identity < 0 or identity in objects:
            raise ValueError("Invalid or duplicate PipeWire identity")
        objects[identity] = item
    return objects


def resolve_hardware_card(
    graph: Sequence[Mapping[str, object]], selector: HardwareSelector,
) -> Mapping[str, object]:
    graph_objects(graph)
    devices = [item for item in graph if item.get("type") == "PipeWire:Interface:Device"]
    matches = [item for item in devices if selector.matches(graph_properties(item))]
    unique_card([graph_properties(item) for item in matches], selector)
    return matches[0]


def validate_downstream(
    graph: Sequence[Mapping[str, object]], selector: HardwareSelector,
    mappings: Mapping[str, tuple[str, str]],
) -> int:
    """Validate one pw-dump graph; no I/O, mutation or default-route inference."""
    validate_mappings(mappings)
    objects = graph_objects(graph)

    def info(item: Mapping[str, object]) -> dict[str, object]:
        return object_mapping(item.get("info"))

    def props(item: Mapping[str, object]) -> dict[str, object]:
        return graph_properties(item)

    card_id = resolve_hardware_card(graph, selector)["id"]
    outputs = [
        item for item in graph if item.get("type") == "PipeWire:Interface:Node"
        and props(item).get("device.id") == card_id
        and props(item).get("media.class") == "Audio/Sink"
        and props(item).get("api.alsa.pcm.stream") == "playback"
        and props(item).get("device.profile.name") == "analog-surround-51"
    ]
    if len(outputs) != 1:
        raise HardwareUnavailable("Unique 5.1 hardware output is unavailable", code="hardware_output_unavailable")
    target = outputs[0]
    if props(target).get("audio.channels") != 6:
        raise HardwareUnavailable("Hardware output is not six-channel", code="hardware_output_unavailable")
    ports = {
        identity: props(item) for identity, item in objects.items()
        if item.get("type") == "PipeWire:Interface:Port"
    }
    for name, channels in mappings.items():
        nodes = [item for item in graph if item.get("type") == "PipeWire:Interface:Node"
                 and props(item).get("node.name") == f"playback.{name}"]
        if len(nodes) != 1:
            raise HardwareUnavailable("Managed loopback playback node is missing or ambiguous",
                                      code="downstream_unavailable")
        links = [item for item in graph if item.get("type") == "PipeWire:Interface:Link"
                 and info(item).get("output-node-id") == nodes[0]["id"]]
        observed: list[str] = []
        for link in links:
            details = info(link)
            if details.get("state") not in ("active", "paused") or details.get("input-node-id") != target["id"]:
                raise HardwareUnavailable("Loopback downstream target is incorrect or inactive",
                                          code="downstream_unavailable")
            source_id, destination_id = details.get("output-port-id"), details.get("input-port-id")
            if (isinstance(source_id, bool) or not isinstance(source_id, int)
                    or isinstance(destination_id, bool) or not isinstance(destination_id, int)):
                raise ValueError("Invalid link port identity")
            source = ports.get(source_id)
            destination = ports.get(destination_id)
            if (source is None or destination is None
                    or source.get("node.id") != nodes[0]["id"]
                    or destination.get("node.id") != target["id"]
                    or source.get("port.direction") != "out"
                    or destination.get("port.direction") != "in"
                    or source.get("audio.channel") != destination.get("audio.channel")):
                raise HardwareUnavailable("Loopback downstream channel mapping is incorrect",
                                          code="downstream_unavailable")
            channel = destination.get("audio.channel")
            if not isinstance(channel, str):
                raise ValueError("Invalid channel identity")
            observed.append(channel)
        if sorted(observed) != sorted(channels):
            raise HardwareUnavailable("Loopback downstream links are missing or duplicated",
                                      code="downstream_unavailable")
    identity = target["id"]
    if not isinstance(identity, int):
        raise ValueError("Invalid hardware output identity")
    return identity
