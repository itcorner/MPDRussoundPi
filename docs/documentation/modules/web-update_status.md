# `web/update_status.py` and `mpdrussoundpi/common/imageregistry.py`

## Documentation

`imageregistry.py` is the project's only Docker Hub client: anonymous, read-only,
limited to the project's repositories, its tag forms (`dev`, `dev-<commit>`,
`latest`, `X.Y`, `X.Y.Z`, `X.Y.Z-rc.N`) and digests, with size-limited,
digest-checked responses and blob redirects only to Docker's download hosts.
`platform_config` returns the index, platform manifest and image configuration
(labels, creation time) of one tag for one platform. The deployment tooling
(`tool/deployment_images.py`) builds its stricter development-image validation on
it; the web application uses it for the update check.

`update_status.py` runs one background thread in the web process. It compares the
image's published identity (`RUSSOUND_BUILD_IMAGE`, `RUSSOUND_BUILD_VERSION`) with
the channel tag (`dev` for development images, `latest` for releases) about every
6 hours and on request (coalesced, at most once per 60 s), caches the result in
memory and never pulls, installs or restarts anything. A failed lookup keeps the
last result, marked stale, and is never reported as up to date.
`RUSSOUND_UPDATE_CHECK=off` turns it off. The Status page shows the result in the
"Software updates" block of the Administrator panel (rendered by `status.js` from
`GET /api/status/updates`) and as an arrow icon in the System overview header.

## Requirements

- Python 3.11+, standard library only. Outbound HTTPS to `auth.docker.io`,
  `registry-1.docker.io` and Docker's blob download hosts.

## Test Cases

[Update status tests](../../../tests/test_update_status.py) cover unsupported and
disabled builds (no registry access), the development and release comparisons,
lagging moving tags, failures that keep the last result, unexpected labels, the
allowed registry resources, rate limiting and staleness, the overview section and
the endpoints including the same-origin guard. The deployment tooling's use is
covered by [deployment image tests](../../../tests/test_deployment_images.py).
