import argparse
import logging
import time
import subprocess

from lib.mpdbackend import BackendError, MPDBackend


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile saved MPD playback intentions")
    parser.add_argument("json")
    parser.add_argument("--initialize", action="store_true", help="Persist default runtime state for new outputs and exit")
    parser.add_argument("--once", action="store_true", help="Reconcile every output once; fail if any output fails")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("mpd").setLevel(logging.WARNING)
    backend = MPDBackend(args.json)
    if args.initialize:
        backend.initialize_intentions()
        return
    backend.initialize_intentions()
    failures: dict[str, tuple[float, float]] = {}
    while True:
        now = time.monotonic()
        failed = False
        for output in backend.list_outputs():
            next_attempt, delay = failures.get(output.id, (0.0, 1.0))
            if now < next_attempt:
                continue
            try:
                unit = f"mpd_{output.id}.service"
                subprocess.run(["systemctl", "--user", "start", unit], check=True,
                               capture_output=True, text=True, timeout=45)
                backend.reconcile(output.id)
            except (BackendError, OSError, ValueError, subprocess.SubprocessError) as error:
                failed = True
                if isinstance(error, subprocess.CalledProcessError):
                    logging.error("MPD service activation error: %s", error.stderr)
                logging.error("Reconciliation failed for %s: %s; retry in %.0fs", output.id, error, delay)
                failures[output.id] = (time.monotonic() + delay, min(delay * 2, 30.0))
            else:
                if output.id in failures:
                    logging.info("Recovered %s", output.id)
                    del failures[output.id]
        if args.once:
            if failed:
                parser.exit(1, "One or more outputs failed reconciliation\n")
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
