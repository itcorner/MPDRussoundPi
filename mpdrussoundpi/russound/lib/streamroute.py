from dataclasses import dataclass
import json
import logging
import subprocess
import time
from typing import TypeAlias, Union


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


class RouteUnavailable(RuntimeError):
    """A required live sink or playback client has not appeared."""


@dataclass(frozen=True)
class StreamRoute:
    client_index: int
    actual_sink: str
    expected_sink: str

    @property
    def correct(self) -> bool:
        return self.actual_sink == self.expected_sink


def _pactl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["pactl", *arguments], check=True, capture_output=True, text=True, timeout=2,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"pactl {' '.join(arguments)} failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"pactl {' '.join(arguments)} timed out") from error
    return result.stdout


def _objects(kind: str) -> list[dict[str, JSONValue]]:
    try:
        value: JSONValue = json.loads(_pactl("--format=json", "list", kind))
    except ValueError as error:
        raise RuntimeError(f"Invalid pactl JSON for {kind}: {error}") from error
    if not isinstance(value, list):
        raise RuntimeError(f"Expected pactl {kind} JSON array")
    objects: list[dict[str, JSONValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid pactl {kind} entry")
        objects.append(item)
    return objects


def inspect_route(sink_name: str) -> StreamRoute:
    expected = f"sink.{sink_name}"
    sinks: dict[int, str] = {}
    for item in _objects("sinks"):
        index, name = item.get("index"), item.get("name")
        if isinstance(index, bool) or not isinstance(index, int) or not isinstance(name, str):
            raise RuntimeError("Invalid pactl sink identity")
        if index in sinks or name in sinks.values():
            raise RuntimeError("Ambiguous pactl sink identity")
        sinks[index] = name
    if expected not in sinks.values():
        raise RouteUnavailable(f"Expected virtual sink {expected} is missing")
    clients: list[dict[str, JSONValue]] = []
    for item in _objects("sink-inputs"):
        props = item.get("properties")
        if not isinstance(props, dict):
            raise RuntimeError("Invalid pactl sink-input properties")
        if (props.get("application.name") == "Music Player Daemon"
                and props.get("application.process.binary") == "mpd"
                and props.get("media.name") == f"pw_{sink_name}"):
            clients.append(item)
    if not clients:
        raise RouteUnavailable(f"No active MPD playback client for pw_{sink_name}; start playback first")
    if len(clients) != 1:
        raise RuntimeError(f"Ambiguous MPD playback clients for pw_{sink_name}; refusing to move any")
    index, sink_index = clients[0].get("index"), clients[0].get("sink")
    if (isinstance(index, bool) or not isinstance(index, int)
            or isinstance(sink_index, bool) or not isinstance(sink_index, int)):
        raise RuntimeError(f"Invalid MPD playback identity for pw_{sink_name}")
    if sink_index not in sinks:
        raise RouteUnavailable(f"Playback sink for pw_{sink_name} disappeared during inspection")
    return StreamRoute(index, sinks[sink_index], expected)


def recover_route(sink_name: str, timeout: float = 5) -> StreamRoute:
    deadline = time.monotonic() + timeout
    stable_client: int | None = None
    last_problem = "route did not settle"
    while True:
        try:
            route = inspect_route(sink_name)
        except RouteUnavailable as error:
            stable_client = None
            last_problem = str(error)
        else:
            if route.correct:
                if stable_client == route.client_index:
                    return route
                stable_client = route.client_index
                last_problem = "playback client changed before route verification settled"
            else:
                stable_client = None
                last_problem = f"{route.actual_sink} instead of {route.expected_sink}"
                logging.warning(
                    "Recovering pw_%s client %s: %s -> %s",
                    sink_name, route.client_index, route.actual_sink, route.expected_sink,
                )
                _pactl("move-sink-input", str(route.client_index), route.expected_sink)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Cannot recover route for pw_{sink_name}: {last_problem}")
        time.sleep(0.2)
