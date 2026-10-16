import argparse
import logging

from lib.mpdbackend import BackendError, MPDBackend


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Control configured MPD streams and saved playback intentions")
    action = parser.add_mutually_exclusive_group(required=True)
    for option, help_text in (
        ("start", "Start/resume playback and persist playing intention"),
        ("pause", "Pause playback and persist paused intention"),
        ("stop", "Stop playback and persist stopped intention"),
        ("status", "Show observed playback state"),
        ("apply-gain", "Apply configured gains after recovering routes"),
        ("check-route", "Check actual playback routes without changing them"),
        ("recover-route", "Recover actual playback routes"),
        ("config-revision", "Show the current configuration SHA-256 revision"),
    ):
        action.add_argument(f"--{option}", action="store_true", help=help_text)
    action.add_argument("--set-gain", type=float, metavar="DB", help="Set persisted output calibration")
    action.add_argument("--set-label", metavar="LABEL", help="Edit display label without changing identity")
    parser.add_argument("--expected-revision", metavar="SHA256")
    parser.add_argument("--stream", metavar="NAME")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("json")
    args = parser.parse_args(argv)
    if (args.set_gain is not None or args.set_label is not None) and args.stream is None:
        parser.error("--set-gain/--set-label requires --stream")
    if args.set_label is not None and args.expected_revision is None:
        parser.error("--set-label requires --expected-revision")
    if args.expected_revision is not None and args.set_gain is None and args.set_label is None:
        parser.error("--expected-revision requires --set-gain or --set-label")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    try:
        backend = MPDBackend(args.json)
        if args.config_revision:
            print(backend.configuration_snapshot()[2])
        elif args.set_gain is not None:
            backend.set_gain(args.stream, args.set_gain, expected_revision=args.expected_revision)
        elif args.set_label is not None and args.expected_revision is not None:
            backend.set_label(args.stream, args.set_label, expected_revision=args.expected_revision)
        elif args.start:
            backend.start(args.stream)
        elif args.pause:
            backend.pause(args.stream)
        elif args.stop:
            backend.stop(args.stream)
        elif args.apply_gain:
            backend.apply_gain(args.stream)
        elif args.status:
            for status in backend.status(args.stream):
                print(f"{status.name}: {status.state}, configured gain {status.gain_db:g} dB")
        elif args.recover_route:
            for route in backend.recover_route(args.stream):
                print(f"Verified client {route.client_index} -> {route.actual_sink}")
        elif args.check_route:
            failed = False
            for stream in backend.list_streams():
                if args.stream is not None and stream.name != args.stream:
                    continue
                try:
                    route = backend.check_route(stream.name)
                    print(f"{stream.name}: {route.actual_sink} (expected {route.expected_sink})")
                    failed = failed or not route.correct
                except BackendError as error:
                    logging.error("%s", error)
                    failed = True
            if args.stream is not None and not any(s.name == args.stream for s in backend.list_streams()):
                raise ValueError(f"Unknown stream name: {args.stream!r}")
            if failed:
                raise BackendError("One or more routes failed verification")
    except ValueError as error:
        parser.error(str(error))
    except (BackendError, OSError) as error:
        parser.exit(1, f"MPD control failed: {error}\n")


if __name__ == "__main__":
    main()
