import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tool.dummy_backend.dummy_backend import (
    DummyRussoundRequestHandler, DummyRussoundState, ThreadedDummyRussoundServer,
)


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "POSIX service signals")
class WebLifecycleTests(unittest.TestCase):
    def test_sigterm_with_open_event_stream_exits_cleanly(self):
        self.check_shutdown(signal.SIGTERM)

    def test_sigint_with_open_event_stream_exits_cleanly(self):
        self.check_shutdown(signal.SIGINT)

    def test_sigterm_drains_an_active_request_before_exit(self):
        self.check_shutdown(signal.SIGTERM, active_request=True)

    def check_shutdown(self, stop_signal, active_request=False):
        with tempfile.TemporaryDirectory(prefix="russound-lifecycle-") as directory:
            root = Path(directory)
            config = root / "config.json"
            backend = ThreadedDummyRussoundServer(
                ("127.0.0.1", 0), DummyRussoundRequestHandler, DummyRussoundState())
            thread = threading.Thread(target=backend.serve_forever, daemon=True)
            thread.start()

            def close_backend():
                backend.shutdown()
                backend.server_close()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

            self.addCleanup(close_backend)
            original_config = json.dumps({
                "backend": {"host": "127.0.0.1", "port": backend.server_address[1]},
                "controllers": [], "zones": [], "inputs": [],
            }).encode()
            config.write_bytes(original_config)
            state = root / "state.json"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("RUSSOUND_")}
            args = [sys.executable, "-B", "-m", "web.russound_server", "--host", "127.0.0.1",
                    "--port", str(port), "--config", str(config), "--state", str(state)]
            marker = root / "request-phase"
            if active_request:
                args = [sys.executable, "-B", "-c", """
from pathlib import Path
import sys, time
from web.russound_server import RussoundHTTPServer
server = RussoundHTTPServer(sys.argv[1], sys.argv[2])
@server.app.post('/test/drain')
def drain():
    marker = Path(sys.argv[4])
    marker.write_text('started')
    time.sleep(0.5)
    marker.write_text('committed')
    return {'ok': True}
server.run('127.0.0.1', int(sys.argv[3]))
""", str(config), str(state), str(port), str(marker)]
            process = subprocess.Popen(
                args,
                cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            response = None
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        connection.request("GET", "/" if active_request else "/api/events")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        if active_request:
                            response.read()
                            connection.close()
                            connection.request("POST", "/test/drain")
                            while not marker.exists() and time.monotonic() < deadline:
                                time.sleep(0.01)
                            self.assertTrue(marker.exists(), "Active request did not begin")
                        else:
                            self.assertTrue(response.readline().startswith(b"retry:"))
                        break
                    except (ConnectionRefusedError, ConnectionResetError):
                        connection.close()
                        self.assertIsNone(process.poll(), "Web process exited before readiness")
                        if time.monotonic() >= deadline:
                            self.fail("Web did not become ready")
                        time.sleep(0.05)
                started = time.monotonic()
                process.send_signal(stop_signal)
                output, _ = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, output.decode())
                self.assertLess(time.monotonic() - started, 8)
                self.assertNotIn(b"thread(s) still running", output)
                if active_request:
                    self.assertEqual(marker.read_text(), "committed")
                self.assertEqual(config.read_bytes(), original_config)
                self.assertNotIn(b"Traceback", output)
            finally:
                failed = sys.exc_info()[0] is not None
                if response is not None:
                    response.close()
                connection.close()
                if process.poll() is None:
                    process.kill()
                output, _ = process.communicate(timeout=5)
                if failed:
                    print(output.decode(), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
