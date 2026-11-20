import argparse
import logging
import re
import signal
from types import FrameType

from waitress.server import create_server

if not __package__:
    # Run as a file: import the library by its canonical package name only
    # (see the package layout plan in docs/plans/).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from mpdrussoundpi.russound.lib.playbackagent import AgentObserver, create_app
from mpdrussoundpi.russound.lib.mpdbackend import MPDBackend
from mpdrussoundpi.russound.lib.playbackoperations import OperationManager
from mpdrussoundpi.common.servicecredentials import read_service_token
from mpdrussoundpi.common.runtimelogging import configure_console


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
    parser.add_argument("--debug", action="store_true", help="Enable diagnostic logging")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost") and not args.allow_remote:
        parser.error("Remote binding requires --allow-remote and provisioned secure transport")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.enable_tunein_selection and args.disable_tunein_selection:
        parser.error("--enable-tunein-selection conflicts with --disable-tunein-selection")
    try:
        token = read_service_token(args.token_file)
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", args.agent_id) is None:
            raise ValueError("Invalid agent identity")
    except ValueError:
        parser.error("Invalid agent identity or service token file")
    configure_console(args.debug)
    logging.getLogger("mpd").setLevel(logging.WARNING)
    backend = MPDBackend(args.json) if args.operations_db else None
    operations = OperationManager(
        backend, args.operations_db, tunein_selection_enabled=not args.disable_tunein_selection,
    ) if backend is not None else None
    observer = AgentObserver(args.json, backend=backend, controls_enabled=operations is not None)
    try:
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
