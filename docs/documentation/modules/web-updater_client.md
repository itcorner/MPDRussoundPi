# `web/updater_client.py`

## Documentation

Client for the updater service's Unix socket, stage 3 of the
[web-triggered update plan](../../plans/general_new_user-triggered-image-updates.md).
The socket is mounted read-only into the web container at
`/run/mpdrussoundpi-updater/updater.sock` (`RUSSOUND_UPDATER_SOCKET` overrides
it). When the socket is absent, installing updates from the page is reported as
not set up: only single-host Docker installations with the updater service
support it.

`UpdaterClient.request` sends one JSON request per connection with a 10-second
timeout and raises `UpdaterUnavailable` (no service or no answer) or
`UpdaterRefused` (the updater's own, safe reason). `sanitize_status` reduces the
updater's status to the documented fields, cleans text and includes the review
(with the confirmation digest) only for administrators. The server's
`/api/admin/update*` routes add the administrator check, CSRF token and the
recent-password requirement before forwarding.

## Requirements

- Python 3.11+, standard library only.

## Test Cases

[Update page flow tests](../../../tests/test_update_page_flow.py) run the web
endpoints against the real updater socket server with a simulated transaction:
review, wrong and correct confirmation, success, cancel, refused restore,
login/CSRF/recent-password requirements, installations without the updater and
sanitizing.
