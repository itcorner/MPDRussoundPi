import argparse
import logging
from pathlib import Path
import signal
from types import FrameType

from waitress.server import create_server

from lib.playbackagent import AgentObserver, create_app
from lib.mpdbackend import MPDBackend
from lib.playbackoperations import OperationManager


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve cached MPD observations and opt-in manual operations")
    parser.add_argument("json")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--operations-db", help="Explicitly enable manual commands with a private persistent SQLite ledger")
    parser.add_argument("--disable-tunein-selection", action="store_true",
                        help="Disable station selection from the TuneIn catalog (enabled with manual operations)")
    parser.add_argument("--enable-tunein-selection", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--allow-remote", action="store_true",
                        help="Permit non-loopback binding only behind provisioned secure transport")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost") and not args.allow_remote:
        parser.error("Remote binding requires --allow-remote and provisioned secure transport")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.enable_tunein_selection and args.disable_tunein_selection:
        parser.error("--enable-tunein-selection conflicts with --disable-tunein-selection")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("mpd").setLevel(logging.WARNING)
    backend = MPDBackend(args.json) if args.operations_db else None
    operations = OperationManager(
        backend, args.operations_db, tunein_selection_enabled=not args.disable_tunein_selection,
    ) if backend is not None else None
    observer = AgentObserver(args.json, backend=backend, controls_enabled=operations is not None)
    try:
        token = Path(args.token_file).read_text(encoding="ascii").strip()
        app = create_app(observer, args.agent_id, token, operations)
    except (OSError, ValueError):
        if operations is not None:
            operations.close()
        parser.error("Invalid agent identity or service token file")
    server = create_server(
        app, host=args.host, port=args.port, threads=4,
        max_request_header_size=8192, max_request_body_size=2048,
        channel_timeout=30, connection_limit=100,
    )

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        observer.start()
        if operations is not None:
            operations.start()
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        server.task_dispatcher.shutdown()
        if operations is not None:
            operations.close()
        observer.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
