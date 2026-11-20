import argparse
import logging

if not __package__:
    # Run as a file: import the library by its canonical package name only
    # (docs/plans/general_new_package-layout.md).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from mpdrussoundpi.russound.lib.audioready import wait_for_audio


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
