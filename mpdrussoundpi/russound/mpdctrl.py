import argparse
import logging

import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
from lib.setuplock import setup_lock
import lib.sink as sink


def play(mpd_instances: list[MPDInstance]):
    for instance in mpd_instances:
        instance.play()


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
            if args.start:
                play(instances)
            else:
                stop(instances)


if __name__ == "__main__":
    main()
