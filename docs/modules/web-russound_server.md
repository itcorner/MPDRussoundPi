# `web/russound_server.py`

## Documentation

Hosts the Flask application, static pages, token-protected API routes, SSE events, session tracking, backend polling, status payloads, and request validation.

## Requirements

- Python 3.11+.
- Flask and the repository controller/backend modules.
- API clients must provide the configured token through the supported header or events query parameter.

## Test Cases

[Server tests](../../tests/test_russound_server.py) cover session cookies, token authorization, SSE delimiters, event broadcasts, polling configuration, unsolicited updates, route parsing, validation helpers, client status, history, and logging configuration.

## Blind Spots

Not every API endpoint is exercised through Flask, including many successful and failed mutation requests; malformed JSON, invalid route bodies, SSE disconnects, and long-running watcher shutdown behavior remain gaps.
