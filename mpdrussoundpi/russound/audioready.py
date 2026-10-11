import argparse
import logging

from lib.audioready import wait_for_audio


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait for configured hardware profile and audio sinks")
    parser.add_argument("json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        wait_for_audio(args.json)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Audio readiness failed: {error}\n")


if __name__ == "__main__":
    main()
