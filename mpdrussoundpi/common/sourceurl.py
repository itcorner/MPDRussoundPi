from urllib.parse import SplitResult, urlsplit


MAX_SOURCE_URL_LENGTH = 1200


def validate_source_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_URL_LENGTH
        or not value.isprintable()
        or any(character.isspace() for character in value)
        or "\\" in value
        or "#" in value
    ):
        raise ValueError("Source must be a direct HTTP(S) URL no longer than 1200 characters")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Source must be a valid direct HTTP(S) URL") from None
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.netloc.endswith(":")
        or any(character.isspace() for character in parts.hostname)
        or "%" in parts.hostname
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Source must be a direct HTTP(S) URL without a fragment")
    return value


def source_display(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts: SplitResult = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    display_host = f"[{host}]" if ":" in host else host
    authority = f"{display_host}:{port}" if port is not None else display_host
    return f"{parts.scheme.lower()}://{authority}/..."
