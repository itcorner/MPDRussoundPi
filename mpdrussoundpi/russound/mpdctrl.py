import argparse
import logging

from lib.mpdbackend import BackendError, MPDBackend


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Control configured MPD outputs and saved playback intentions")
    action = parser.add_mutually_exclusive_group(required=True)
    for option, help_text in (
        ("start", "Start/resume playback and persist playing intention"),
        ("pause", "Pause playback and persist paused intention"),
        ("stop", "Stop playback and persist stopped intention"),
        ("status", "Show observed playback state"),
        ("apply-gain", "Apply saved gains after recovering routes"),
        ("check-route", "Check actual playback routes without changing them"),
        ("recover-route", "Recover actual playback routes"),
        ("config-revision", "Show the current configuration SHA-256 revision"),
    ):
        action.add_argument(f"--{option}", action="store_true", help=help_text)
    action.add_argument("--set-gain", type=float, metavar="DB", help="Set saved output gain in playback.json")
    parser.add_argument("--output", metavar="ID", help="Output ID from config.json (default: all outputs)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("json")
    args = parser.parse_args(argv)
    if args.set_gain is not None and args.output is None:
        parser.error("--set-gain requires --output")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    try:
        backend = MPDBackend(args.json)
        if args.config_revision:
            print(backend.configuration_snapshot()[2])
        elif args.set_gain is not None:
            backend.set_gain(args.output, args.set_gain)
        elif args.start:
            backend.start(args.output)
        elif args.pause:
            backend.pause(args.output)
        elif args.stop:
            backend.stop(args.output)
        elif args.apply_gain:
            backend.apply_gain(args.output)
        elif args.status:
            for status in backend.status(args.output):
                print(f"{status.id}: {status.state}, saved gain {status.gain_db:g} dB")
        elif args.recover_route:
            for route in backend.recover_route(args.output):
                print(f"Verified client {route.client_index} -> {route.actual_sink}")
        elif args.check_route:
            outputs = backend.list_outputs()
            if args.output is not None and not any(output.id == args.output for output in outputs):
                raise ValueError(f"Unknown output ID: {args.output!r}")
            failed = False
            for output in outputs:
                if args.output is not None and output.id != args.output:
                    continue
                try:
                    route = backend.check_route(output.id)
                    print(f"{output.id}: {route.actual_sink} (expected {route.expected_sink})")
                    failed = failed or not route.correct
                except BackendError as error:
                    logging.error("%s", error)
                    failed = True
            if failed:
                raise BackendError("One or more routes failed verification")
    except ValueError as error:
        parser.error(str(error))
    except (BackendError, OSError) as error:
        parser.exit(1, f"MPD control failed: {error}\n")


if __name__ == "__main__":
    main()
