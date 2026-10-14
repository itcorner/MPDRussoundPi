import argparse
import logging
from pathlib import Path
import signal
from types import FrameType

from waitress.server import create_server

from lib.playbackagent import AgentObserver, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve cached read-only MPD observations")
    parser.add_argument("json")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--allow-remote", action="store_true",
                        help="Permit non-loopback binding only behind provisioned secure transport")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost") and not args.allow_remote:
        parser.error("Remote binding requires --allow-remote and provisioned secure transport")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("mpd").setLevel(logging.WARNING)
    observer = AgentObserver(args.json)
    try:
        token = Path(args.token_file).read_text(encoding="ascii").strip()
        app = create_app(observer, args.agent_id, token)
    except (OSError, ValueError):
        parser.error("Invalid agent identity or service token file")
    server = create_server(
        app, host=args.host, port=args.port, threads=4,
        max_request_header_size=8192, max_request_body_size=1024,
        channel_timeout=30, connection_limit=100,
    )

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        observer.start()
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        server.task_dispatcher.shutdown()
        observer.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
