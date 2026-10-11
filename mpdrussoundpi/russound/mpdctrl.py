import argparse
import logging

import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
from lib.setuplock import setup_lock
from lib.streamgain import set_sink_gain, validate_gain_db
from lib.streamroute import inspect_route, recover_route
import lib.sink as sink


def stop(mpd_instances: list[MPDInstance]):
    for instance in mpd_instances:
        instance.stop()


def status(mpd_instances: list[MPDInstance]):
    for instance in mpd_instances:
        state = instance.get_stream_status()
        if state:
            print(f"Stream '{instance.stream.get_name()}' status: {state}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Manually control configured MPD streams')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--start', action='store_true', help='Start selected or all streams')
    action.add_argument('--stop', action='store_true', help='Stop selected or all streams')
    action.add_argument('--status', action='store_true', help='Show selected or all stream statuses')
    action.add_argument('--apply-gain', action='store_true', help='Apply configured gains without changing playback')
    action.add_argument('--check-route', action='store_true', help='Verify actual playback routes without changing them')
    action.add_argument('--recover-route', action='store_true', help='Recover and verify selected or all playback routes')
    parser.add_argument('--stream', metavar='NAME', help='Exact configured stream name; omit for all streams')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('json', help='Path to JSON configuration file')
    args = parser.parse_args(argv)

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.DEBUG if args.debug else logging.INFO,
    )
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = readconfig.read_and_parse_json(args.json)
    if not config:
        parser.error("Failed to read configuration")

    streams = config.get('streams', [])
    if args.stream is not None:
        streams = [entry for entry in streams if entry.get('name') == args.stream]
        if not streams:
            parser.error(f"Unknown stream name: {args.stream!r}")
        if len(streams) != 1:
            parser.error(f"Ambiguous stream name: {args.stream!r}")
    if not streams:
        parser.error("No streams configured")

    sink_dict: dict[str, sink.Sink] = {}
    for audio_device in config.get('outputmappers', {}).get('audio_devices', []):
        for sink_element in audio_device.get('sinks', []):
            sink_dict[sink_element.get('name')] = sink.config2sink(sink_element)

    gains: dict[str, tuple[str, float]] = {}
    if args.start or args.apply_gain or args.check_route or args.recover_route:
        used_sinks: set[str] = set()
        for entry in config.get('streams', []):
            try:
                gain = validate_gain_db(entry.get('gain_db', 0)) if args.start or args.apply_gain else 0.0
                sink_name = entry.get('audio_output', {}).get('sink')
                if (entry.get('audio_output', {}).get('type') != 'pulse'
                        or not isinstance(sink_name, str) or sink_name not in sink_dict):
                    raise ValueError("gain/route control requires a configured PulseAudio virtual sink")
                if sink_name in used_sinks:
                    raise ValueError(f"per-stream gain/route control requires separate sinks: {sink_name!r}")
                name = entry.get('name')
                if not isinstance(name, str) or not name:
                    raise ValueError("stream name must be a nonempty string")
                if name in gains:
                    raise ValueError(f"duplicate stream name: {entry['name']!r}")
                used_sinks.add(sink_name)
                gains[name] = (sink_name, gain)
            except (ValueError, KeyError, TypeError) as error:
                parser.error(f"Invalid gain/route configuration for {entry.get('name')!r}: {error}")

    if args.check_route:
        failed = False
        for entry in streams:
            sink_name, _ = gains[entry['name']]
            try:
                route = inspect_route(sink_name)
                print(f"{entry['name']}: client {route.client_index} -> {route.actual_sink}"
                      f" (expected {route.expected_sink})")
                if not route.correct:
                    logging.error("Incorrect route for %s", entry['name'])
                    failed = True
            except (OSError, RuntimeError) as error:
                logging.error("Route check failed for %s: %s", entry['name'], error)
                failed = True
        if failed:
            parser.exit(1, "One or more stream routes failed verification.\n")
        return

    if args.recover_route:
        with setup_lock('mpd_control.lock'):
            for entry in streams:
                sink_name, _ = gains[entry['name']]
                try:
                    route = recover_route(sink_name)
                except (OSError, RuntimeError) as error:
                    parser.exit(1, f"Route recovery failed for {entry['name']}: {error}\n")
                print(f"{entry['name']}: verified client {route.client_index} -> {route.actual_sink}")
        return

    instances: list[MPDInstance] = []
    for entry in streams:
        instance = config2mpd(entry, sink_dict, ignore_missing_sinks=True)
        if instance is None:
            parser.error(f"Invalid stream configuration: {entry.get('name')!r}")
        instances.append(instance)

    if args.status:
        status(instances)
    else:
        with setup_lock('mpd_control.lock'):
            if args.start or args.apply_gain:
                for entry, instance in zip(streams, instances):
                    sink_name, gain = gains[entry['name']]
                    if args.start:
                        instance.play()
                    try:
                        recover_route(sink_name)
                        set_sink_gain(sink_name, gain)
                    except (OSError, RuntimeError, TimeoutError) as error:
                        if args.start:
                            instance.stop()
                        parser.exit(1, f"Failed to recover route/apply gain for {entry['name']}: {error}\n")
            elif args.stop:
                stop(instances)


if __name__ == "__main__":
    main()
