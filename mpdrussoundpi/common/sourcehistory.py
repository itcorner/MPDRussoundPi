"""Whether one published build contains another, from the project's public Git history.

docs/plans/general_new_release-channels.md: an installation on the stable channel
that runs a development build is offered a release only when that release contains
the installed build (its commit is a descendant of the installed build's commit).
The Pi has no copy of the history, so this asks GitHub's public compare API, read
only and without credentials. Only commit identifiers are sent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from mpdrussoundpi.common.imageregistry import SOURCE, NoRedirect

API = "https://api.github.com/repos/" + SOURCE.removeprefix("https://github.com/")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
MAX_RESPONSE = 2 * 1024 * 1024
TIMEOUT_SECONDS = 20


class HistoryError(ValueError):
    """The history could not be read; nothing is concluded."""


def _fetch(url: str) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "mpdrussoundpi-updater"})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        raise HistoryError(f"The project history could not be read: HTTP {error.code}") from None
    except (URLError, OSError) as error:
        raise HistoryError("The project history could not be read") from error
    if len(body) > MAX_RESPONSE:
        raise HistoryError("The project history response is too large")
    try:
        value = json.loads(body)
    except ValueError as error:
        raise HistoryError("The project history response is not JSON") from error
    if not isinstance(value, dict):
        raise HistoryError("The project history response is not an object")
    return value


def contains(release: str, installed: str, *, fetch: Callable[[str], dict[str, Any]] = _fetch) -> bool:
    """True when commit ``release`` contains commit ``installed`` (equal or a descendant)."""
    if not COMMIT.fullmatch(release) or not COMMIT.fullmatch(installed):
        raise HistoryError("Expected two full commit identifiers")
    if release == installed:
        return True
    status = fetch(f"{API}/compare/{installed}...{release}").get("status")
    if status in ("ahead", "identical"):
        return True
    if status in ("behind", "diverged"):
        return False
    raise HistoryError("The project history returned an unexpected comparison")
