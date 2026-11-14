# `web/russound_server.py`

## Documentation

Hosts the Flask application, static pages, token-protected API routes, SSE events, session tracking, backend polling, status payloads, and request validation.

The web application is served by Waitress in both normal and debug logging
modes. `--debug` / `RUSSOUND_DEBUG` enable verbose logging only; they do not
enable Flask's development server or interactive debugger.

When run on the main thread, the server handles SIGTERM and SIGINT, including
as container PID 1. Shutdown rejects new requests, closes SSE streams, drains
HTTP workers, stops the backend watcher and playback pollers, and releases the
shared gateway connection. Previous signal handlers are restored afterward.
Embedded worker-thread callers retain ownership of process signal handling.
A stopped server instance cannot be restarted; create a new instance.

SSE shutdown checks run at most 0.5 seconds apart without increasing the
normal 15-second ping frequency. The watcher is allowed 10 seconds to finish;
each playback poller is allowed 8 seconds, after stop has been requested for
all pollers. Pollers do not issue another request after the active request
completes. Waitress also bounds its worker drain. Unstopped HTTP workers,
backend watcher or playback pollers raise an explicit shutdown error; a
timeout is not a successful stop. New Compose profiles allow 30 seconds before
Docker's final forced termination. This is not a deadline guarantee for
arbitrarily blocked OS/network calls or a substitute for checking actual
container/process state before backup.

## Requirements

- Python 3.11+.
- Flask and the repository controller/backend modules.
- API clients must provide the configured token through the supported header or events query parameter.

## Test Cases

[Server tests](../../../tests/test_russound_server.py) cover session cookies, token authorization, SSE delimiters, event broadcasts, polling configuration, unsolicited updates, route parsing, validation helpers, client status, history, and logging configuration.
[Lifecycle tests](../../../tests/test_web_lifecycle.py) run actual Waitress
subprocesses with open event streams and an in-flight write, send SIGTERM or
SIGINT, and assert clean exit, bounded shutdown and request completion.
The [container recovery test](../../../tests/test_container_recovery.py) also
checks current web source as Linux PID 1, with real dummy routing and an open
SSE stream, before taking its stopped-writer backup.

## Blind Spots

Not every API endpoint is exercised through Flask. Arbitrarily stuck native
calls, physical serial behavior and target power-loss durability are not
certified by the lifecycle tests.
